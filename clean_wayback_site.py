#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
clean_wayback_site.py
======================

Cleans a Wayback-Machine-saved (or similarly bloated CMS export) static site:

  - un-wraps every "https://web.archive.org/web/<timestamp>/<original-url>" (and the
    scheme-relative "//web.archive.org/..." form) back to the original URL, everywhere
    (href/src/srcset/style/meta content/kept inline CSS & JS).
  - deletes the Wayback Machine toolbar markup, its scripts (athena.js, wombat.js,
    bundle-playback.js, ruffle.js, saved_resource, __wm.* calls, archive_analytics) and
    its CSS (banner-styles.css, iconochive.css).
  - removes ALL html comments (wayback markers, IE-conditional cruft, etc).
  - removes <a> tags that point to external, non-library domains - including social
    networks. Same-domain links are kept and rewritten as relative paths.
  - strips <script> tags down to the ones that plausibly implement UI behaviour
    (burger menu, toggle, slider/carousel, accordion, tabs, modal, dropdown...).
    Analytics/CMS-loader/tracker/font-loader/archive/minified-bundle scripts are dropped.
  - drops external font-loading <link>/<style> (Typekit/Adobe Fonts/etc.) and injects a
    Google Fonts <link> in their place.
  - strips CMS/JS-hook clutter (data-*, on*) from every element, EXCEPT the data-*
    attributes that are commonly used by menu/slider/toggle widgets (data-toggle,
    data-target, data-slide, data-bs-*, data-slick-*, data-swiper-*, ...).
  - promotes data-src -> src on images/sources missing a real src.
  - extracts the surviving inline <head><style> blocks (after cleaning) into a
    standalone .css file and links it instead.
  - moves now-orphaned local wayback asset files (athena.js, wombat.js, ...) into a
    "_wayback_removed" sub-folder instead of deleting them outright.
  - KEEPS all contact info by default (mailto:/tel: links, email/phone/address text) -
    cleanup never touches it. Opt in to stripping/redacting it with --strip-contact-info.
  - removes <iframe> embeds pointing to external domains (maps, video, widgets).
  - flags (does NOT auto-delete - too risky to guess boundaries) possible physical
    addresses, adult/casino/gambling keywords, mojibake and leaked raw HTML-as-text.
  - checks for a LOCAL favicon (a link pointing off-site, e.g. a leftover CMS CDN
    default icon, does not count) and, if none exists, auto-generates a simple flat
    monogram .ico (first letter of --brand-text/domain) - the site never ships with
    no favicon at all, even with no assets to work from. --favicon still overrides.
  - strips the <html> tag down to just lang/class, dropping itemscope/itemtype/
    xmlns:*/id/style and any other accumulated cruft.
  - checks for local robots.txt/sitemap.xml, and a canonical that still points
    somewhere other than the detected site domain.
  - detects logo elements (image or text) and can swap them in one shot across every
    matched element (header + mobile nav, etc.) via --logo-image / --brand-text - see
    "Logo/brand replacement" below.
  - neuters internal <a href> pointing at pages missing from this export to "#"
    instead of shipping dead menu links (can't fabricate the missing pages).
  - injects a global `img { object-fit: cover }` rule so images forced into a
    fixed-size slot crop to fill instead of stretching.
  - moves any local asset file the final HTML no longer references anywhere
    (unused duplicates, leftover CMS bundles, ...) into _unused_removed/.
  - writes the standard .htaccess (gzip compression, .js.gz serving, font/webp mime
    types) next to the site if one isn't already there - see DEFAULT_HTACCESS.
  - prints a final PBN-restoration-checklist-style verdict (BLOCKER / MEDIUM / MINOR),
    matching the "Чек-лист быстрого восстановления ПБН до 800$" categories.
  - re-downloads missing local <img>/<source> assets straight from web.archive.org,
    using the archived URL still sitting in data-image/data-src at that point in the
    pipeline (must run on the ORIGINAL, uncleaned export - a second pass on an
    already-cleaned file has nothing left to recover from). Disable with
    --no-image-recovery.

Usage
-----
    python clean_wayback_site.py <path-to-site-folder-or-html-file> [options]

Options
-------
    --fonts "Family1:wght@400;700,Family2"   Google Fonts families to link in
                                              (fonts.google.com css2 API family params).
                                              Defaults to a generic placeholder; the
                                              report prints the original font names found
                                              so you can pick real replacements.
    --logo-image PATH                        Replace every detected logo <img>/<svg>
                                              with this file (copied into the assets
                                              folder). Works across repeated runs on many
                                              site folders - point it at the same file.
    --brand-text "New Name"                  Replace every detected text-logo/site-title
                                              element, <title>, and og:site_name/
                                              og:title/twitter:title with this string.
    --favicon PATH                           Copy this file in and set/replace the
                                              favicon <link>.
    --dry-run                                Only print the report, write nothing.
    --no-backup                              Skip the automatic ".bak" backup files.
    --strip-contact-info                     Opt in to removing mailto:/tel: and
                                              redacting email/phone/address (kept by default).
    --check-url URL                          Standalone live-site check (no folder
                                              needed): HTTPS, redirect, status code,
                                              robots.txt/sitemap.xml reachability.

Logo/brand replacement
-----------------------
The script can't know what a "correct" logo looks like, so by default it only
DETECTS and REPORTS every element that looks like a logo (image with logo/brand-ish
class/id/alt, or a text site-title element) so you immediately see what to touch on
each site. Pass --logo-image and/or --brand-text to actually swap them - the same
file/string gets applied to every matched element (desktop nav, mobile nav, etc. all
at once), so across "дохуя сайтов" you can just point one flag at one prepared logo
file per site and re-run.

The script edits matched files IN PLACE (after writing a "<file>.bak" backup unless
--no-backup is passed) and prints/writes a "<file>.cleanup-report.txt" summary.

Tuning
------
All domain/keyword lists live in the CONFIG section right below the imports - edit them
per-site if the default heuristics keep/drop the wrong thing.
"""

import argparse
import functools
import json
import os
import random
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import SplitResult, urljoin, urlsplit, urlunsplit

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")


def safe_urlsplit(url):
    """`urlsplit` that never raises. Archived pages carry genuinely malformed hrefs - leftover
    template/BBCode junk like `http://[t vi=/ ]/en/home[/t]`, an unbalanced `[` in the host - and
    the stdlib answers those with ValueError("Invalid IPv6 URL"). ONE such link anywhere killed the
    entire cleanup mid-run (bambooship.vn). Junk must degrade, not crash: report no scheme and no
    host, so every caller treats it as a non-external, unusable reference and the link-cleaning
    steps neutralize it like any other dead link. (Nothing to do with the machine's real IPv6.)"""
    try:
        return urlsplit(url)
    except ValueError:
        return SplitResult("", "", url or "", "", "")


def safe_urljoin(base, url):
    """`urljoin` that never raises - it parses through the same stdlib path as `safe_urlsplit`."""
    try:
        return urljoin(base, url)
    except ValueError:
        return url or ""

# --------------------------------------------------------------------------------------
# CONFIG - tune per-site if needed
# --------------------------------------------------------------------------------------

LIBRARY_DOMAINS = {
    "fonts.googleapis.com", "fonts.gstatic.com", "ajax.googleapis.com",
    "cdnjs.cloudflare.com", "cdn.jsdelivr.net", "unpkg.com", "code.jquery.com",
    "stackpath.bootstrapcdn.com", "maxcdn.bootstrapcdn.com", "use.fontawesome.com",
    "kit.fontawesome.com", "cdn.datatables.net", "polyfill.io",
}

FONT_SERVICE_DOMAINS = {
    "use.typekit.net", "p.typekit.net", "fonts.com", "fast.fonts.net",
    "cloud.typography.com", "typekit.net",
}

# Font detection: generic CSS keywords and classic OS/browser default-stack names are
# never "the site's font" - if that's all a page declares, there's nothing worth
# keeping and the cleanup falls back to a random preset instead.
GENERIC_FONT_KEYWORDS = {
    "sans-serif", "serif", "monospace", "cursive", "fantasy", "system-ui",
    "-apple-system", "blinkmacsystemfont", "ui-sans-serif", "ui-serif",
    "ui-monospace", "ui-rounded", "emoji", "math", "fangsong",
    "inherit", "initial", "unset", "revert",
}
SYSTEM_FONT_NAMES = {
    "arial", "helvetica", "helvetica neue", "segoe ui", "tahoma", "verdana",
    "times new roman", "times", "georgia", "courier new", "courier",
    "trebuchet ms", "lucida sans unicode", "lucida grande", "impact",
    "comic sans ms", "ms sans serif", "consolas", "monaco",
    # More OS stacks old themes declare. These are NOT on Google Fonts, so treating one as
    # "the site's font" makes the download 404/403 and the page render in a fallback anyway.
    "lucida sans", "lucida console", "lucida", "book antiqua", "palatino",
    "palatino linotype", "garamond", "bookman old style", "century gothic",
    "franklin gothic medium", "arial black", "arial narrow", "gill sans",
    "candara", "calibri", "cambria", "constantia", "corbel", "optima",
    "geneva", "sans-serif", "serif",
}
# Icon fonts (Bootstrap glyphicons, Font Awesome, Material Icons, ...) turn up in a
# font-family: declaration same as a real webfont would, but aren't a body-text
# typeface choice at all and don't exist on Google Fonts under that name - loading
# "Glyphicons Halflings" from the css2 API 404s/gets ORB-blocked. Filtered by an
# explicit name AND a substring hint (icon fonts overwhelmingly have "icon" in the
# name somewhere) so unlisted ones are still caught.
ICON_FONT_NAMES = {
    "glyphicons halflings", "fontawesome", "font awesome", "font awesome 5 free",
    "font awesome 5 brands", "font awesome 6 free", "font awesome 6 brands",
    "ionicons", "material icons", "material icons outlined", "material symbols",
    "icomoon", "simple-line-icons", "themify", "flaticon", "linearicons",
    "et-line", "pe-icon-7-stroke", "elegant-icons", "et-icons",
}
ICON_FONT_HINTS = ("icon", "glyphicon", "awesome")

# Third-party CDNs that serve web fonts / framework assets - these never lived at the site's own
# domain, and the cleaner re-injects Google Fonts (and self-hosts icon fonts) fresh anyway, so
# hitting web.archive.org to "recover" one of their files is pure wasted time: each is a slow,
# always-failing CDX round-trip, and a Google-Fonts CSS references DOZENS of them (every Inter/
# Roboto subset) - the single biggest stall when cleaning, and it multiplies under parallel
# cleanups (archive.org throttles the burst, every request then hangs to its full timeout).
_FONT_CDN_HOSTS = {
    "fonts.gstatic.com", "fonts.googleapis.com", "gstatic.com", "googleapis.com",
    "use.typekit.net", "typekit.net", "p.typekit.net", "use.fontawesome.com", "fontawesome.com",
    "cdnjs.cloudflare.com", "maxcdn.bootstrapcdn.com", "stackpath.bootstrapcdn.com",
    "netdna.bootstrapcdn.com", "cdn.jsdelivr.net", "fonts.bunny.net",
}

SOCIAL_DOMAINS = {
    "facebook.com", "fb.com", "twitter.com", "x.com", "instagram.com",
    "youtube.com", "youtu.be", "pinterest.com", "pinterest.fi", "linkedin.com",
    "tiktok.com", "vk.com", "vkontakte.ru", "ok.ru", "t.me", "telegram.me",
    "whatsapp.com", "threads.net", "snapchat.com", "tumblr.com", "reddit.com",
    "flickr.com", "soundcloud.com", "behance.net", "dribbble.com",
}

WAYBACK_JS_NAME_HINTS = (
    "athena.js", "wombat.js", "bundle-playback.js", "ruffle.js", "saved_resource",
)

SCRIPT_DROP_KEYWORDS = (
    "__wm", "wombat", "archive_analytics", "archive.org", "rewriteurl",
    "wm-ipp", "rufflePlayer".lower(),
    "typekit", "squarespace_rollups", "squarespace_context", "static.sqsp",
    "squarespace.load", "squarespace.afterbodyload", "sqs-type",
    "imageloader-bootstrapper",
    "google-analytics", "googletagmanager", "gtag(", "ga('create'",
    "fbevents", "connect.facebook.net", "hotjar", "matomo", "mixpanel",
    "segment.com", "doubleclick", "clarity.ms", "metrika", "plausible.io",
    "-min.", ".min.",
)

SCRIPT_KEEP_KEYWORDS = (
    "burger", "hamburger", "toggle", "nav-menu", "mobile-nav", "mobile-menu",
    "menu-open", "menu-close", "slider", "swiper", "slick", "carousel",
    "accordion", "dropdown", "offcanvas", "collapse", "tab-", "lightbox",
    "modal",
)

# jQuery/Bootstrap bundles are foundational UI libraries, not CMS/analytics cruft -
# the generic "-min."/".min." entry in SCRIPT_DROP_KEYWORDS (aimed at minified
# analytics bundles) would otherwise catch "bootstrap.min.js" too, and a plain
# "jquery.js" has no drop/keep keyword at all so it fell through to "ambiguous" ->
# dropped. Both are extremely common as the actual thing driving data-toggle=
# "collapse" navbar burgers / dropdowns in old templates - dropping them silently
# breaks the mobile menu. Matched by filename stem, so kept unconditionally.
FOUNDATIONAL_JS_STEMS = {
    "jquery", "jquery.min", "jquery.slim", "jquery.slim.min",
    "bootstrap", "bootstrap.min", "bootstrap.bundle", "bootstrap.bundle.min",
}

DATA_ATTR_KEEP_HINTS = (
    "toggle", "target", "dismiss", "slide", "ride", "interval", "menu", "nav",
    "collapse", "tab", "accordion", "carousel", "swiper", "slick", "lightbox",
    "fancybox", "modal", "offcanvas", "dropdown",
)

WAYBACK_TOOLBAR_CSS_NAMES = {"banner-styles.css", "iconochive.css"}

# Font handling: presets the cleanup picks a random one of, and the fixed set of
# weights every font is linked at (kept to 3 - light/regular/bold covers a page's
# hierarchy without bloating the Google Fonts request).
FONT_WEIGHTS = "400;500;700"
# Only a UI suggestion for the studio's font field. NOTHING in the cleaner may fall back to
# this: an un-asked-for font silently re-brands a restored site (see inject_google_fonts).
DEFAULT_GOOGLE_FONTS = f"Jost:wght@{FONT_WEIGHTS}"

# Standard .htaccess for every restored PBN site - gzip compression, serve
# pre-gzipped .js.gz when available, correct headers/mime types for fonts and webp.
DEFAULT_HTACCESS = """RewriteEngine On
RewriteBase /

# Redirect www to non-www
# RewriteCond %{HTTP_HOST} ^www\\.(.*)$ [NC]
# RewriteRule ^(.*)$ https://%1/$1 [R=301,L]

# Redirect non-www to www
# RewriteCond %{HTTP_HOST} !^www\\. [NC]
# RewriteRule ^(.*)$ https://www.%{HTTP_HOST}/$1 [R=301,L]

# Clean URLs: /about -> about.html
RewriteCond %{REQUEST_FILENAME} !-d
RewriteCond %{REQUEST_FILENAME}\\.html -f
RewriteRule ^(.*?)/?$ $1.html [L]

# Remove .html from URL
RewriteCond %{THE_REQUEST} \\s/+([^\\s]+?)\\.html[\\s?] [NC]
RewriteRule ^ %1 [R=301,L]

# Strip query string from /
RewriteCond %{REQUEST_URI} ^/$
RewriteCond %{QUERY_STRING} .+
RewriteRule ^ https://%{HTTP_HOST}/? [R=301,L,NE]

# Non-existent path with query string -> /
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteCond %{QUERY_STRING} .+
RewriteRule ^ https://%{HTTP_HOST}/? [R=301,L,NE]

# Non-existent path -> /
RewriteCond %{REQUEST_FILENAME} !-f
RewriteCond %{REQUEST_FILENAME} !-d
RewriteRule ^ / [R=301,L]

# ----------------------------------------------------------
# 5) Gzip compression (HTML, CSS, JS, fonts, etc.)
# ----------------------------------------------------------
<IfModule mod_deflate.c>
    # DeflateCompressionLevel 9
    AddEncoding gzip .gz
    AddType application/javascript .gz
    SetEnvIfNoCase Request_URI \\.gz$ no-gzip

    AddOutputFilterByType DEFLATE text/html text/plain text/xml text/css
    AddOutputFilterByType DEFLATE application/javascript application/x-javascript
    AddOutputFilterByType DEFLATE image/svg+xml
    AddOutputFilterByType DEFLATE font/woff font/woff2 font/ttf font/otf
</IfModule>

# ----------------------------------------------------------
# 6) Rewrite .js -> .js.gz when available
# ----------------------------------------------------------
<IfModule mod_rewrite.c>
    RewriteEngine On
    RewriteCond %{HTTP:Accept-encoding} gzip
    RewriteCond %{REQUEST_FILENAME}.gz -f
    RewriteRule ^(.*)\\.js$ $1\\.js\\.gz [QSA,L]
</IfModule>

# ----------------------------------------------------------
# 7) Correct headers for .js.gz
# ----------------------------------------------------------
<IfModule mod_headers.c>
    <FilesMatch "\\.js\\.gz$">
        Header set Content-Encoding gzip
        Header set Content-Type "application/javascript"
    </FilesMatch>
</IfModule>

# ----------------------------------------------------------
# 8) Font and webp mime types
# ----------------------------------------------------------
AddType font/woff2 .woff2
AddType font/woff .woff
AddType font/ttf .ttf
AddType font/otf .otf
AddType image/webp .webp

# Block bots
RewriteCond %{HTTP_USER_AGENT} SemrushBot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} MJ12bot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} Riddler
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} aiHitBot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} trovitBot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} Detectify
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} BLEXBot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} LinkpadBot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} dotbot
RewriteRule (.*) - [F,L]
RewriteCond %{HTTP_USER_AGENT} FlipboardProxy
RewriteRule (.*) - [F,L]
"""

_CHARSET_META_RE = re.compile(rb'charset\s*=\s*["\']?\s*([a-zA-Z0-9_-]+)', re.IGNORECASE)
_CHARSET_ALIASES = {"iso-8859-1": "cp1252", "latin1": "cp1252", "latin-1": "cp1252"}


def read_text_safe(path):
    """Read an HTML/CSS file respecting its OWN declared encoding instead of always
    assuming UTF-8 - a windows-1252 export (extremely common on older European sites:
    German/Finnish/etc. umlauts) decoded as UTF-8 with errors='ignore' doesn't mojibake,
    it silently DELETES every non-ASCII character (ö/ä/ü/ß/é/...) with no visible sign
    anything went wrong. Tries: BOM -> the file's own <meta charset>/@charset -> utf-8
    -> windows-1252, and only replaces (never silently drops) as an absolute last resort."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        return raw.decode("utf-8-sig")

    candidates = []
    m = _CHARSET_META_RE.search(raw[:4096])
    if m:
        declared = m.group(1).decode("ascii", errors="ignore").lower()
        candidates.append(_CHARSET_ALIASES.get(declared, declared))
    candidates += ["utf-8", "cp1252"]

    seen = set()
    for enc in candidates:
        if enc in seen:
            continue
        seen.add(enc)
        try:
            return raw.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def normalize_charset_meta(soup):
    """Output is always written as UTF-8 - force any declared charset (HTML5 <meta
    charset> or the old <meta http-equiv=Content-Type content=...charset=...> form)
    to say so too, or a stale windows-1252/etc. declaration left over from the
    source page would make browsers misdecode perfectly-valid UTF-8 bytes."""
    head = soup.find("head")
    if not head:
        return
    meta_charset = head.find("meta", attrs={"charset": True})
    if meta_charset:
        meta_charset["charset"] = "utf-8"
    for meta in head.find_all("meta", attrs={"http-equiv": re.compile("^content-type$", re.I)}):
        content = meta.get("content", "")
        if "charset=" in content.lower():
            meta["content"] = re.sub(r"charset=[^;]+", "charset=utf-8", content, flags=re.IGNORECASE)


# Обёртка снимается и со ссылок НЕ-http: архив заворачивает `tel:`/`mailto:` точно так же, и без
# этого телефон остаётся href="https://web.archive.org/web/2024.../tel:19003007" — клик уводит на
# archive.org вместо звонка. Схемы перечислены явно: «любое слово с двоеточием» съело бы обычные
# пути, где двоеточие встречается внутри имени файла.
_WB_LINK_SCHEMES = "tel|mailto|sms|callto|whatsapp|viber|skype|facetime|geo|bitcoin"
WAYBACK_PREFIX_RE = re.compile(
    r"(?:(?:https?:)?//web\.archive\.org)?/web/\d{1,14}[a-zA-Z_]*/"
    r"(?=https?://|//|(?:" + _WB_LINK_SCHEMES + r"):)"
)
FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;{}]+)", re.IGNORECASE)
# @import of an old font service - kept CSS text gets this stripped out (rather than
# the whole block dropped, the way an @font-face hit does) since it's typically one
# harmless standalone line pulling in a font we're about to replace anyway.
FONT_IMPORT_RE = re.compile(
    r"@import\s+(?:url\(\s*)?['\"]?(?:https?:)?//"
    r"(?:fonts\.googleapis\.com|fonts\.gstatic\.com|use\.typekit\.net|p\.typekit\.net|"
    r"fonts\.com|fast\.fonts\.net|cloud\.typography\.com)[^;]*;?",
    re.IGNORECASE,
)
PRECLEAN_STRAY_TAG_RE = re.compile(r'<div id="yui3-css-stamp"[^>]*>\s*</div>', re.IGNORECASE)

# The Wayback Machine appends its own notice + a "playback timings" debug block to
# the END of every CSS/JS file it serves through the live player - "FILE ARCHIVED
# ON... AND RETRIEVED FROM THE INTERNET ARCHIVE ON...", "JAVASCRIPT APPENDED BY
# WAYBACK MACHINE, COPYRIGHT INTERNET ARCHIVE...", then a second block with
# "playback timings (ms): capture_cache.get: ..." etc. Neither is part of the real
# site - they're artifacts of viewing/saving THROUGH web.archive.org, and unlike a
# URL wrapper they're not something unwayback() (which only touches URL text)
# touches at all. Matched by the /* ... */ (or <!-- ... -->) block that CONTAINS the
# distinctive phrase, not by exact wording, so minor formatting differences between
# captures still get caught.
#   BUG HISTORY: the first version of these used a plain ".*?" between the comment
#   opener and the trigger phrase - `.` (even non-greedy, even with DOTALL) matches
#   a literal "*/" as ordinary characters, so if the file had ANY earlier, unrelated
#   /* ... */ comment before the wayback notice, the match spanned from THAT much
#   earlier "/*" all the way to the wayback notice's closing "*/", deleting every
#   real rule in between - gutted bootstrap.min.css/theme.css/etc down to a few
#   bytes on a real site before this was caught. Fixed with "(?:(?!\*/).)*?" - "any
#   character, as long as we're not standing at the start of */" - which keeps the
#   match inside the ONE unbroken comment that actually contains the phrase.
WAYBACK_ARCHIVE_NOTICE_RE = re.compile(
    r"/\*(?:(?!\*/).)*?FILE ARCHIVED ON(?:(?!\*/).)*?\*/"
    r"|<!--(?:(?!-->).)*?FILE ARCHIVED ON(?:(?!-->).)*?-->",
    re.DOTALL | re.IGNORECASE,
)
WAYBACK_TIMINGS_COMMENT_RE = re.compile(
    r"/\*(?:(?!\*/).)*?playback timings \(ms\):(?:(?!\*/).)*?\*/"
    r"|<!--(?:(?!-->).)*?playback timings \(ms\):(?:(?!-->).)*?-->",
    re.DOTALL | re.IGNORECASE,
)


def strip_wayback_appended_comments(text):
    """Remove the Wayback-injected archive-notice and playback-timings comment
    blocks (see WAYBACK_ARCHIVE_NOTICE_RE above) from arbitrary text - safe to run
    on any file, a no-op if neither phrase is present."""
    text = WAYBACK_ARCHIVE_NOTICE_RE.sub("", text)
    text = WAYBACK_TIMINGS_COMMENT_RE.sub("", text)
    return text

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
# Same, but with the local part and domain captured, for rewriting an e-mail's domain in place.
_EMAIL_LOCAL_DOMAIN_RE = re.compile(r"([A-Za-z0-9._%+\-]+)@([A-Za-z0-9.\-]+\.[A-Za-z]{2,})")
# Phone-ish text: a run of digits/spaces/dashes/parens/leading "+" (deliberately NOT
# dots - dotted digit groups are almost always dates like 17.3.2019, especially on
# European/Finnish sites). Verified further in _phone_sub (min digit count, needs
# either a separator or enough digits to not be a bare year/id).
PHONE_RE = re.compile(r"(?<![\w@.])\+?\(?\d[\d\s\-()]{4,16}\d(?![\w@.])")
CONTACT_TEXT_ATTRS = ("title", "alt", "content", "placeholder", "aria-label", "value")

# PBN restoration checklist extras --------------------------------------------------

ADULT_KEYWORDS = (
    "porn", "xxx", "hardcore", "escort", " sex ", "sex.", "sexy", "nude", "camgirl",
    "onlyfans", "casino", "gambling", "bet365", "poker", "slot machine", "jackpot",
    "виагра", "порно", "казино", "ставки на спорт", "букмекер",
)

ADDRESS_KEYWORDS = (
    " street", " st.", " avenue", " ave.", " road ", " blvd", "suite ", "floor,",
    "katu ", "kuja ", "tie ", " puistotie", "улица", "ул.", "пр-т", "проспект",
    "переулок", "корпус", "офис ",
)
ADDRESS_POSTAL_RE = re.compile(r"\b\d{4,6}\b")

# Common UTF-8-decoded-as-Latin-1 mojibake pairs (Ã©, Â©, â€™, ...) and the
# unicode replacement character left behind by a broken decode.
MOJIBAKE_RE = re.compile("[ÃÂâ][-¿]{1,2}|�")
# Matched against already-parsed text nodes, so HTML entities like &lt;/&gt; have
# already been decoded to literal </> by BeautifulSoup - match the literal form.
RAW_HTML_LEAK_RE = re.compile(r"</?[a-zA-Z][a-zA-Z0-9]{0,20}(?:\s[^<>]{0,80})?>")

LOGO_HINT_TOKENS = ("logo", "brand", "site-title", "sitetitle")
# Even if they also match a LOGO_HINT_TOKEN (e.g. class="logo-subtitle"), these are
# taglines/descriptions, not the brand name itself - never a text-logo replace target.
TEXT_LOGO_EXCLUDE_TOKENS = ("subtitle", "tagline", "slogan", "description", "desc")

IFRAME_LIBRARY_DOMAINS = set()  # every external iframe is removed - PBN checklist item 24
FAVICON_RELS = {"icon", "shortcut icon", "apple-touch-icon"}

# --------------------------------------------------------------------------------------
# dependency bootstrap
# --------------------------------------------------------------------------------------


def _ensure(pkg, import_name=None):
    import_name = import_name or pkg
    try:
        return __import__(import_name)
    except ImportError:
        print(f"[setup] installing missing dependency: {pkg} ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", pkg])
        import importlib

        importlib.invalidate_caches()
        return __import__(import_name)


bs4 = _ensure("beautifulsoup4", "bs4")
try:
    _ensure("lxml")
    from bs4 import BeautifulSoup as _Probe

    _Probe("<p>x</p>", "lxml")
    PARSER = "lxml"
except Exception:
    PARSER = "html.parser"

from bs4 import BeautifulSoup, Comment  # noqa: E402


# --------------------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------------------


def unwayback(text):
    """Strip the web.archive.org time-travel wrapper, restoring the original URL."""
    if not text:
        return text
    return WAYBACK_PREFIX_RE.sub("", text)


def domain_of(url):
    try:
        netloc = safe_urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def is_external(url):
    parts = safe_urlsplit(url)
    return bool(parts.scheme) or url.startswith("//")


def matches_suffix(domain, domain_set):
    return any(domain == d or domain.endswith("." + d) for d in domain_set)


def _bare_domain(domain):
    """domain_of() always strips a leading 'www.' off any URL it looks at, but
    site_domain (when it comes from the export folder's own name, e.g.
    'www.example.com') often still has it - so a same-site 'www.example.com' link
    compared against domain_of()'s bare 'example.com' output never matched anything
    and got misclassified as external. Strip it here too before any such comparison."""
    domain = (domain or "").lower()
    return domain[4:] if domain.startswith("www.") else domain


def looks_hashed(stem):
    compact = stem.replace("-", "").replace("_", "")
    return len(compact) >= 24 and compact.isalnum()


def to_relative(url, site_domain):
    """Turn a same-site absolute URL into a root-relative path for a clean static export."""
    parts = safe_urlsplit(url)
    path = parts.path or "/"
    rebuilt = urlunsplit(("", "", path, parts.query, parts.fragment))
    return rebuilt or "/"


class Report:
    def __init__(self):
        self.audit_warnings = []
        self.removed_scripts = []
        self.ambiguous_scripts_dropped = []
        self.removed_links_css = []
        self.removed_cms_meta = []
        self.kept_a_external = []
        self.kept_a_social = []
        self.kept_a_internal = 0
        self.font_families_found = set()
        self.moved_assets = []
        self.removed_unused_assets = []
        self.custom_css_written = None
        self.removed_contact_links = []
        self.redacted_emails = []
        self.redacted_phones = []
        self.redacted_addresses = []
        self.redacted_domain_mentions = []
        self.emails_domain_normalized = []
        self.removed_iframes = []
        self.flagged_adult = []
        self.flagged_garbage_text = []
        self.logo_candidates = []
        self.logo_replaced = 0
        self.brand_text_replaced = 0
        self.brand_body_replacements = 0
        self.removed_owner_traces = []
        self.owner_trace_year_updates = 0
        self.icon_font_selfhosted = False
        self.glyphicons_rewritten = 0
        self.custom_icons_rewritten = 0  # theme's own icon font (icon-twitter...) -> Font Awesome
        self.localized_media = []
        self.detached_media = []
        self.external_media_localized = []   # third-party image downloaded into the project
        self.external_media_removed = []     # third-party image unreachable/tracker -> element gone
        self.external_embeds_removed = []    # <object>/<embed> to youtube/flash/etc (ads+trackers)
        self.external_hrefs_stripped = []    # external <a> on the page -> href dropped
        self.inline_scripts_externalized = None  # inline JS moved to a deferred .js file
        self.jquery_selfhosted = None            # page used jQuery with none loaded -> added
        self.favicon_present = False
        self.favicon_set = False
        self.favicon_auto_generated = False
        self.favicon_was_external = None
        self.robots_present = None
        self.sitemap_present = None
        self.robots_created = False
        self.sitemap_created = False
        self.htaccess_present = None
        self.htaccess_created = False
        self.canonical_old = None
        self.canonical_new = None
        self.html_lang = None
        self.head_meta_deduped = 0
        self.recovered_images = []
        self.failed_image_recovery = []
        self.noindex_removed = None
        self.external_redirect_found = None
        self.broken_internal_targets = []
        self.lazy_loading_added = 0
        # AI (Haiku) semantic pass - only populated when semantics.available()
        self.semantic_title = None
        self.semantic_description = None
        self.semantic_tags_applied = []  # e.g. "div.top-bar -> header"
        self.landmarks_repaired = []     # bogus nav/header wrapping the whole page -> div
        self.sliders_fixed = None        # sliders left in a sane static state
        self.reveal_unhidden = 0         # scroll-reveal blocks made visible (their JS is gone)
        self.headings_normalized = []  # e.g. "h4 -> h3"
        self.base_tag_removed = None  # the killed <base href="..."> if any
        self.sri_stripped = 0  # integrity/crossorigin attrs removed (they'd block local assets)
        self.qa_fixed = []  # QA self-check auto-fixes (e.g. "стиль был HTML, снят")
        self.qa_warnings = []  # QA self-check issues that need a human eye

    def render(self):
        lines = ["=== cleanup report ===", ""]
        # Losses vs the archive go FIRST - they are the only findings that mean "this clean is
        # bad", and burying them under a thousand "removed 40 scripts" lines is how they got
        # missed before.
        if self.audit_warnings:
            lines.append("!!! СВЕРКА С АРХИВОМ — ЧТО ПОТЕРЯЛОСЬ:")
            for w in self.audit_warnings:
                lines.append(f"  !!! {w}")
            lines.append("")
        lines.append(f"scripts removed: {len(self.removed_scripts)}")
        for s in self.removed_scripts[:50]:
            lines.append(f"  - {s}")
        if self.ambiguous_scripts_dropped:
            lines.append("")
            lines.append(
                f"unclassified local scripts dropped by default ({len(self.ambiguous_scripts_dropped)}) "
                f"- review if any of these were actually needed:"
            )
            for s in self.ambiguous_scripts_dropped:
                lines.append(f"  ? {s}")
        lines.append("")
        lines.append(f"stylesheet/font links removed: {len(self.removed_links_css)}")
        for s in self.removed_links_css:
            lines.append(f"  - {s}")
        lines.append(f"CMS boilerplate (feeds/oembed/RSD/wlwmanifest/generator/...) removed: {len(self.removed_cms_meta)}")
        for s in self.removed_cms_meta:
            lines.append(f"  - {s}")
        lines.append("")
        lines.append(f"social <a> links kept (wayback wrapper unwrapped only): {len(self.kept_a_social)}")
        for s in self.kept_a_social:
            lines.append(f"  - {s}")
        lines.append(f"other external <a> links kept (wayback wrapper unwrapped only): {len(self.kept_a_external)}")
        for s in self.kept_a_external[:50]:
            lines.append(f"  - {s}")
        lines.append(f"internal <a> links kept (rewritten relative): {self.kept_a_internal}")
        lines.append("")
        lines.append(f"mailto:/tel: links removed: {len(self.removed_contact_links)}")
        for s in self.removed_contact_links:
            lines.append(f"  - {s}")
        if self.redacted_emails:
            lines.append(f"email addresses redacted from text/attrs: {len(self.redacted_emails)}")
            for s in sorted(set(self.redacted_emails)):
                lines.append(f"  - {s}")
        if self.inline_scripts_externalized:
            lines.append(f"inline JS externalized: {self.inline_scripts_externalized}")
        if self.jquery_selfhosted:
            lines.append(f"jQuery self-hosted locally: {self.jquery_selfhosted}")
        if self.external_media_localized:
            lines.append(f"third-party images pulled local: {len(self.external_media_localized)}")
            for s in self.external_media_localized:
                lines.append(f"  - {s}")
        if self.external_media_removed:
            lines.append(f"third-party images removed (tracker/unreachable): {len(self.external_media_removed)}")
            for s in self.external_media_removed:
                lines.append(f"  - {s}")
        if self.external_embeds_removed:
            lines.append(f"external <object>/<embed> removed (flash/youtube -> ads): {len(self.external_embeds_removed)}")
            for s in self.external_embeds_removed:
                lines.append(f"  - {s}")
        if self.external_hrefs_stripped:
            lines.append(f"external link hrefs stripped: {len(self.external_hrefs_stripped)}")
            for s in sorted(set(self.external_hrefs_stripped))[:30]:
                lines.append(f"  - {s}")
        if self.emails_domain_normalized:
            lines.append(f"email domains normalized to the site domain: {len(self.emails_domain_normalized)}")
            for s in self.emails_domain_normalized:
                lines.append(f"  - {s}")
        if self.redacted_phones:
            lines.append(f"phone-like numbers redacted from text/attrs: {len(self.redacted_phones)}")
            for s in sorted(set(self.redacted_phones)):
                lines.append(f"  - {s}")
        if self.redacted_addresses:
            lines.append(f"physical addresses redacted from text/attrs: {len(self.redacted_addresses)}")
            for s in self.redacted_addresses:
                lines.append(f"  - {s}")
        if self.redacted_domain_mentions:
            lines.append(f"old-domain text mentions redacted: {len(self.redacted_domain_mentions)}")
            for s in sorted(set(self.redacted_domain_mentions)):
                lines.append(f"  - {s}")
        lines.append("")
        if self.font_families_found:
            lines.append("original font-family names detected (pick real Google Fonts replacements):")
            for f in sorted(self.font_families_found):
                lines.append(f"  - {f}")
            lines.append("")
        if self.moved_assets:
            lines.append(f"orphaned wayback asset files moved to _wayback_removed/: {len(self.moved_assets)}")
            for f in self.moved_assets:
                lines.append(f"  - {f}")
            lines.append("")
        if self.removed_unused_assets:
            lines.append(f"unused local asset files moved to _unused_removed/: {len(self.removed_unused_assets)}")
            for f in self.removed_unused_assets:
                lines.append(f"  - {f}")
            lines.append("")
        if self.custom_css_written:
            lines.append(f"extracted inline <style> content into: {self.custom_css_written}")
        lines.append("")
        lines.append(f"missing local images re-downloaded from web.archive.org: {len(self.recovered_images)}")
        for s in self.recovered_images[:100]:
            lines.append(f"  - {s}")
        if self.failed_image_recovery:
            lines.append(f"images that could NOT be recovered ({len(self.failed_image_recovery)}):")
            for s in self.failed_image_recovery:
                lines.append(f"  ! {s}")
        lines.append(f"<img> tags given loading=\"lazy\": {self.lazy_loading_added}")
        lines.append("")
        lines.append(f"external <iframe> embeds removed: {len(self.removed_iframes)}")
        for s in self.removed_iframes:
            lines.append(f"  - {s}")
        lines.append("")
        lines.append("--- logo / brand ---")
        if self.logo_candidates:
            lines.append(f"logo/brand elements detected: {len(self.logo_candidates)}")
            for s in self.logo_candidates:
                lines.append(f"  - {s}")
        else:
            lines.append("no logo/brand elements auto-detected - check header manually")
        if self.logo_replaced:
            lines.append(f"logo elements replaced with --logo-image: {self.logo_replaced}")
        if self.brand_text_replaced:
            lines.append(f"text-logo/title/meta elements replaced with --brand-text: {self.brand_text_replaced}")
        if self.brand_body_replacements:
            lines.append(f"old brand name swapped in body text/attrs (--brand-old): {self.brand_body_replacements}")
        if self.removed_owner_traces:
            lines.append(f"owner traces removed (verification/generator/GTM meta): {len(self.removed_owner_traces)}")
            for t in self.removed_owner_traces:
                lines.append(f"  - {t}")
        if self.owner_trace_year_updates:
            lines.append(f"© year bumped to current: {self.owner_trace_year_updates}")
        if self.icon_font_selfhosted:
            lines.append("icons: self-hosted a working Font Awesome locally (broken icon webfont replaced)")
        if self.sliders_fixed:
            lines.append(f"static slider fix: {self.sliders_fixed}")
        if self.reveal_unhidden:
            lines.append(f"scroll-reveal blocks un-hidden (their animation JS was stripped): {self.reveal_unhidden}")
        if self.glyphicons_rewritten:
            lines.append(f"icons: Bootstrap glyphicons remapped to Font Awesome: {self.glyphicons_rewritten}")
        if self.custom_icons_rewritten:
            lines.append(f"icons: theme icon-font classes remapped to Font Awesome: {self.custom_icons_rewritten}")
        if self.localized_media:
            lines.append(f"media: same-domain absolute refs made local: {len(self.localized_media)}")
            for s in self.localized_media:
                lines.append(f"  - {s}")
        if self.detached_media:
            lines.append(f"media: dead/unrecoverable refs detached: {len(self.detached_media)}")
            for s in self.detached_media:
                lines.append(f"  - {s}")
        lines.append("")
        lines.append("--- flagged for manual review (not auto-removed) ---")
        if self.flagged_adult:
            lines.append(f"possible adult/casino/gambling keywords: {len(self.flagged_adult)}")
            for s in self.flagged_adult:
                lines.append(f"  - {s}")
        if self.flagged_garbage_text:
            lines.append(f"possible mojibake / leaked raw HTML in text: {len(self.flagged_garbage_text)}")
            for s in self.flagged_garbage_text:
                lines.append(f"  - {s}")
        if not (self.flagged_adult or self.flagged_garbage_text):
            lines.append("nothing flagged")
        lines.append("")
        lines.append("--- site hygiene ---")
        favicon_note = ""
        if self.favicon_auto_generated:
            favicon_note = " (auto-generated monogram - no local favicon existed)"
        elif self.favicon_set:
            favicon_note = " (just set via --favicon)"
        lines.append(f"favicon present locally: {'yes' if self.favicon_present else 'no'}{favicon_note}")
        if self.favicon_was_external:
            lines.append(f"  (old favicon pointed off-site, replaced: {self.favicon_was_external})")
        if self.robots_present is not None:
            lines.append(
                f"robots.txt found next to site: {'yes' if self.robots_present else 'no'}"
                + (" (just generated)" if self.robots_created else "")
            )
        if self.sitemap_present is not None:
            lines.append(
                f"sitemap.xml found next to site: {'yes' if self.sitemap_present else 'no'}"
                + (" (just generated)" if self.sitemap_created else "")
            )
        if self.htaccess_present is not None:
            lines.append(
                f".htaccess found next to site: {'yes' if self.htaccess_present else 'no'}"
                + (" (just generated - standard gzip/mime template)" if self.htaccess_created else "")
            )
        if self.canonical_new:
            if self.canonical_old:
                lines.append(f"canonical переписан: {self.canonical_old}  ->  {self.canonical_new}")
            else:
                lines.append(f"canonical добавлен: {self.canonical_new}")
        if self.html_lang:
            lines.append(f"<html lang> выставлен: {self.html_lang}")
        if self.head_meta_deduped:
            lines.append(f"дублей meta в <head> убрано: {self.head_meta_deduped}")
        if self.noindex_removed:
            lines.append(f"removed a noindex robots meta tag: {self.noindex_removed!r}")
        if self.external_redirect_found:
            lines.append(f"removed a redirect to an external domain: {self.external_redirect_found}")
        if self.broken_internal_targets:
            lines.append(
                f"internal links to pages missing from this export, neutered to '#' "
                f"({len(self.broken_internal_targets)}):"
            )
            for s in sorted(set(self.broken_internal_targets)):
                lines.append(f"  - {s}")
        if self.semantic_title or self.semantic_description:
            lines.append("AI-семантика (Haiku):")
            if self.semantic_title:
                lines.append(f"  title:       {self.semantic_title}")
            if self.semantic_description:
                lines.append(f"  description: {self.semantic_description}")
        if self.semantic_tags_applied:
            lines.append(f"AI-семантика тегов ({len(self.semantic_tags_applied)}):")
            for s in self.semantic_tags_applied:
                lines.append(f"  - {s}")
        if self.headings_normalized:
            lines.append(f"уровни заголовков выровнены (без пропусков) ({len(self.headings_normalized)}):")
            for s in self.headings_normalized[:20]:
                lines.append(f"  - {s}")
        if self.base_tag_removed:
            lines.append(f"убран <base href> (ломал все относительные ссылки/стили): {self.base_tag_removed}")
        if self.sri_stripped:
            lines.append(f"снято integrity/crossorigin (блокировали локальные стили/скрипты): {self.sri_stripped}")
        if self.qa_fixed:
            lines.append(f"QA-автофиксы ({len(self.qa_fixed)}):")
            for s in self.qa_fixed:
                lines.append(f"  - {s}")
        if self.qa_warnings:
            lines.append(f"QA-предупреждения — проверь глазами ({len(self.qa_warnings)}):")
            for s in self.qa_warnings:
                lines.append(f"  ! {s}")
        lines.append("")
        lines.append(self.verdict())
        return "\n".join(lines)

    def verdict(self):
        """PBN-restoration-checklist-style BLOCKER/MEDIUM/MINOR verdict."""
        blockers = []
        if self.removed_contact_links or self.redacted_emails or self.redacted_phones or self.redacted_addresses:
            blockers.append("контакты/адреса (телефон/email/физический адрес) были на странице (уже вычищены - проверь визуально)")
        if self.redacted_domain_mentions:
            blockers.append("упоминания старого домена в тексте были на странице (уже вычищены - проверь визуально)")
        if self.flagged_adult:
            blockers.append("возможен adult/casino контент - требует ручной проверки")
        if not self.favicon_present:
            blockers.append("нет favicon")
        if self.failed_image_recovery:
            blockers.append(f"есть {len(self.failed_image_recovery)} битых изображений, которые не удалось восстановить")
        if self.external_redirect_found:
            blockers.append(f"был редирект на внешний домен ({self.external_redirect_found}) - уже удалён, проверь визуально")
        medium = []
        if self.flagged_garbage_text:
            medium.append("похоже на кракозябры / протёкший HTML в тексте")
        if self.robots_present is False or self.sitemap_present is False:
            medium.append("отсутствует robots.txt и/или sitemap.xml")
        if not self.logo_candidates:
            medium.append("логотип не найден автоматически - проверь шапку вручную")
        if self.noindex_removed:
            medium.append("была noindex-метка на странице - уже удалена, проверь что индексация не заблокирована ещё где-то")
        if self.broken_internal_targets:
            medium.append(
                f"{len(set(self.broken_internal_targets))} ссылок в меню вели на несуществующие страницы "
                f"(уже заменены на '#' - проверь, не стоит ли вообще убрать эти пункты меню)"
            )
        lines = ["=== verdict (по чек-листу ПБН) ==="]
        lines.append(f"BLOCKER: {'; '.join(blockers) if blockers else 'нет'}")
        lines.append(f"MEDIUM: {'; '.join(medium) if medium else 'нет'}")
        return "\n".join(lines)


# --------------------------------------------------------------------------------------
# core cleaning
# --------------------------------------------------------------------------------------


_DOMAIN_FOLDER_RE = re.compile(
    r"^(?:www\.)?[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)+$", re.I
)


def _domain_from_folder_name(html_path):
    """The site export folder is conventionally named after the domain it's being
    restored to (e.g. 'www.mikatoronen.com') - that's a much more reliable signal
    than sniffing a possibly-stale canonical/og:url out of the archived content, so
    it takes priority whenever the folder name actually looks like a domain."""
    name = html_path.parent.name.strip().lower()
    return name if _DOMAIN_FOLDER_RE.match(name) else None


def get_site_domain(soup):
    canonical = soup.find("link", rel="canonical")
    if canonical and canonical.get("href"):
        d = domain_of(unwayback(canonical["href"]))
        if d:
            return d
    og_url = soup.find("meta", property="og:url")
    if og_url and og_url.get("content"):
        d = domain_of(unwayback(og_url["content"]))
        if d:
            return d
    from collections import Counter

    counts = Counter()
    for a in soup.find_all("a", href=True):
        url = unwayback(a["href"])
        if is_external(url):
            d = domain_of(url)
            if d:
                counts[d] += 1
    if counts:
        return counts.most_common(1)[0][0]
    return ""


RECOVERY_FETCH_TIMEOUT = 8  # seconds - was 30, then 10. Measured: a healthy web.archive.org
# asset answers in ~1.5s, but a THROTTLED one still legitimately takes 5-8s, so anything under
# 8 starts throwing away assets that would have arrived. 8 trims the dead-request wait without
# losing real recoveries. The actual speed-ups are elsewhere and cost nothing: _FETCH_FAILED
# (never re-ask for a known-dead URL) and fetching CSS-referenced images at DOWNLOAD time, which
# is what took an asset-heavy page from 30+ minutes to ~3.


def recover_missing_local_images(soup, html_path, report, dry_run=False):
    """PBN checklist item 9/10: no missing key images. Wayback/Archivarix exports very
    often reference local image files that were never actually saved (Squarespace's
    responsive srcset only captures some sizes). The wayback-archived absolute URL is
    still sitting in data-image/data-src/src at this point in the pipeline (must run
    BEFORE unwayback_all_attrs/clean_data_and_event_attrs strip it) - use it to
    re-download the exact bytes that were live at capture time. Downloads run
    concurrently (a page can easily have 20-30 of these) and print progress as each
    one finishes, since a page with several genuinely-unrecoverable images used to
    make the whole cleanup look hung for minutes with zero feedback."""
    import urllib.error
    import urllib.request
    from concurrent.futures import ThreadPoolExecutor, as_completed

    def _fetch(url):
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (image-recovery-bot)"})
        with urllib.request.urlopen(req, timeout=RECOVERY_FETCH_TIMEOUT) as resp:
            return resp.read()

    jobs = []  # (tag_src_label, local_path, candidate_url)
    for tag in soup.find_all(["img", "source"]):
        src = tag.get("src") or tag.get("data-src")
        if not src or is_external(src):
            continue
        local_path = (html_path.parent / src).resolve()
        if local_path.is_file():
            continue
        candidate = None
        for attr in ("data-image", "data-src", "src"):
            val = tag.get(attr)
            if val and "web.archive.org" in val:
                candidate = val
                break
        if not candidate:
            report.failed_image_recovery.append(f"{src}: no archived URL found on this tag")
            continue
        # Without an "id_"/"im_" flag on the timestamp segment, web.archive.org serves
        # its own HTML wrapper page (toolbar + rewritten links) instead of raw bytes -
        # force the raw/identity variant so we actually get the image, not a webpage.
        candidate = re.sub(r"(/web/\d{1,14})(?![a-zA-Z_])/", r"\1id_/", candidate, count=1)
        if dry_run:
            report.recovered_images.append(f"{src} (would fetch from {candidate})")
            continue
        jobs.append((src, local_path, candidate))

    if not jobs:
        return

    unique_candidates = sorted({c for _, _, c in jobs})
    print(f"[image-recovery] {len(jobs)} missing image(s), {len(unique_candidates)} unique URL(s) to fetch...")
    results = {}  # candidate -> bytes or Exception
    done_count = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        future_to_candidate = {pool.submit(_fetch, c): c for c in unique_candidates}
        for future in as_completed(future_to_candidate):
            candidate = future_to_candidate[future]
            done_count += 1
            try:
                results[candidate] = future.result()
                print(f"[image-recovery] {done_count}/{len(unique_candidates)} ok: {candidate}")
            except (urllib.error.URLError, OSError, Exception) as e:  # noqa: BLE001
                results[candidate] = e
                print(f"[image-recovery] {done_count}/{len(unique_candidates)} FAILED: {candidate} ({e})")

    for src, local_path, candidate in jobs:
        outcome = results[candidate]
        if isinstance(outcome, Exception):
            report.failed_image_recovery.append(f"{src}: {outcome}")
            continue
        data = outcome
        if not _looks_like_image_bytes(data):
            report.failed_image_recovery.append(f"{src}: downloaded content isn't a valid image (got {candidate})")
            continue
        local_path.parent.mkdir(parents=True, exist_ok=True)
        local_path.write_bytes(data)
        report.recovered_images.append(src)


def _looks_like_image_bytes(data):
    if not data or len(data) < 16:
        return False
    magic = (
        (b"\xff\xd8\xff",),  # JPEG
        (b"\x89PNG\r\n\x1a\n",),  # PNG
        (b"GIF87a",),
        (b"GIF89a",),
        (b"RIFF",),  # WEBP (RIFF....WEBP)
        (b"BM",),  # BMP
        (b"<svg", b"<?xml"),  # SVG
    )
    head = data[:16]
    return any(head.startswith(sig) for group in magic for sig in group)


def classify_script(tag):
    src = tag.get("src") or ""
    text = tag.string or tag.get_text() or ""
    combined = f"{src} {text}".lower()

    if src.startswith(("chrome-extension:", "moz-extension:")):
        return "drop"
    if any(hint in src.lower() for hint in WAYBACK_JS_NAME_HINTS):
        return "drop"
    if tag.get("type") == "application/ld+json":
        return "drop"
    if is_external(src) and domain_of(src) not in LIBRARY_DOMAINS:
        return "drop"
    if src and Path(safe_urlsplit(src).path).stem.lower() in FOUNDATIONAL_JS_STEMS:
        return "keep"
    if any(k in combined for k in SCRIPT_DROP_KEYWORDS):
        return "drop"
    if any(k in combined for k in SCRIPT_KEEP_KEYWORDS):
        return "keep"
    if src:
        stem = Path(safe_urlsplit(src).path).stem
        return "drop" if looks_hashed(stem) else "ambiguous"
    return "drop" if not text.strip() else "ambiguous"


def clean_scripts(soup, report):
    for tag in soup.find_all("script"):
        verdict = classify_script(tag)
        label = tag.get("src") or "(inline script)"
        if verdict == "drop":
            report.removed_scripts.append(label)
            tag.decompose()
        elif verdict == "ambiguous":
            report.ambiguous_scripts_dropped.append(label)
            tag.decompose()
        # 'keep' -> leave tag in place


def clean_stylesheet_links(soup, report, site_domain=None):
    bare = _bare_domain(site_domain) if site_domain else ""
    for tag in soup.find_all("link"):
        href = tag.get("href", "")
        rel = tag.get("rel") or []
        rel = " ".join(rel) if isinstance(rel, list) else str(rel)
        if "stylesheet" not in rel:
            continue
        clean_href = unwayback(href)
        fname = Path(safe_urlsplit(clean_href).path).name
        if fname in WAYBACK_TOOLBAR_CSS_NAMES:
            report.removed_links_css.append(href)
            tag.decompose()
            continue
        if is_external(clean_href):
            d = domain_of(clean_href)
            # A same-domain absolute link (e.g. "http://site/css/landing-page.css") is the
            # site's OWN theme CSS, not an external service - keep it (localize_media_refs
            # rewrites it to the local copy later). Only genuinely external, non-library
            # (or font-service) stylesheets get dropped here.
            same_site = bool(bare) and matches_suffix(d, {bare})
            if not same_site and (matches_suffix(d, FONT_SERVICE_DOMAINS) or d not in LIBRARY_DOMAINS):
                report.removed_links_css.append(href)
                tag.decompose()
                continue
        tag["href"] = clean_href


CMS_JUNK_LINK_RELS = {"https://api.w.org/", "edituri", "wlwmanifest", "shortlink", "profile"}


def strip_cms_meta_links(soup, report):
    """CMS (WordPress and friends) head boilerplate that's meaningless - or actively
    bad, like advertising the exact CMS/version to anyone scanning - once exported as
    a static site: REST API discovery, RSD/EditURI, Windows Live Writer manifest,
    shortlink, oEmbed/RSS/Atom feed links (all point back at the live original site
    anyway), the GMPG XFN profile link, the wp.org dns-prefetch, and the <meta
    name=generator> tag."""
    head = soup.find("head")
    if not head:
        return
    for link in list(head.find_all("link")):
        rel = link.get("rel")
        rel_str = " ".join(rel).lower() if isinstance(rel, list) else str(rel or "").lower()
        href = (link.get("href") or "").lower()
        type_ = (link.get("type") or "").lower()
        is_junk = (
            rel_str in CMS_JUNK_LINK_RELS
            or (rel_str == "dns-prefetch" and "s.w.org" in href)
            or (rel_str == "alternate" and (
                "rss+xml" in type_ or "atom+xml" in type_ or "oembed" in type_ or type_ == "application/json"
            ))
            or "wp-json" in href or "xmlrpc.php" in href or "wlwmanifest.xml" in href
        )
        if is_junk:
            report.removed_cms_meta.append(f"<link rel={rel_str!r}> {link.get('href', '')}")
            link.decompose()

    for meta in list(head.find_all("meta", attrs={"name": re.compile("^generator$", re.I)})):
        report.removed_cms_meta.append(f"<meta name=generator> {meta.get('content', '')}")
        meta.decompose()

    strip_url_meta_tags(soup, report)


_META_URL_RE = re.compile(r"^(?:https?:)?//|^https?://", re.IGNORECASE)


def strip_url_meta_tags(soup, report):
    """Any <meta content="..."> whose value is itself a URL - og:url, og:image,
    og:image:secure_url, twitter:url, twitter:image, ... - gets removed outright,
    even if it points at the site's own (new) domain: these just leak the scraped
    source path (e.g. the original live wp-content upload URL, as seen in a
    dangling og:image) and aren't worth selectively rewriting one by one."""
    head = soup.find("head")
    if not head:
        return
    for meta in list(head.find_all("meta")):
        content = meta.get("content", "")
        if content and _META_URL_RE.match(content.strip()):
            label = meta.get("property") or meta.get("name") or meta.get("itemprop") or "?"
            report.removed_cms_meta.append(f"<meta {label}> {content}")
            meta.decompose()


def strip_head_to_essentials(soup, report):
    """Оставить в <head> из СЕО/соц-мета только НЕОБХОДИМОЕ (правило владельца, 2026-07-22):
    `<title>`, `<meta name=description>`, `<link rel=canonical>` и ОДНУ иконку. Всё прочее
    соц/СЕО — удалить: `<meta property=…>` (og:*, twitter:*, fb:*, article:*), `<meta name=…>`
    вроде og:site_name/twitter:card/twitter:description/keywords/msapplication-*/robots,
    `<link rel=alternate hreflang>` и ЛИШНИЕ иконки (apple-touch-icon, mask-icon, дубли).

    ТЕХНИЧЕСКОЕ не трогаем — без него ломается страница: `<meta charset>`, `<meta name=viewport>`,
    `<meta http-equiv=…>`, `<link rel=stylesheet>`, `preconnect`/`preload`/`dns-prefetch` (шрифты),
    `<style>`, `<script>`, `<base>`, `<title>`.
    """
    head = soup.find("head")
    if not head:
        return 0
    removed = 0
    for meta in list(head.find_all("meta")):
        if meta.has_attr("charset") or meta.has_attr("http-equiv"):
            continue
        name = (meta.get("name") or "").strip().lower()
        prop = (meta.get("property") or "").strip().lower()
        itemprop = (meta.get("itemprop") or "").strip().lower()
        if not prop and not itemprop and name in ("description", "viewport"):
            continue
        if name or prop or itemprop:   # любой соц/СЕО-мета — прочь
            report.removed_cms_meta.append(f"<meta {prop or name or itemprop}> (соц/СЕО-мусор)")
            meta.decompose()
            removed += 1
    icon_kept = False
    for link in list(head.find_all("link")):
        rel = link.get("rel")
        rel_str = " ".join(rel).lower() if isinstance(rel, list) else str(rel or "").lower()
        if ("stylesheet" in rel_str or "canonical" in rel_str
                or "preconnect" in rel_str or "preload" in rel_str
                or "modulepreload" in rel_str or "dns-prefetch" in rel_str):
            continue
        if "icon" in rel_str:
            plain_icon = rel_str in ("icon", "shortcut icon", "shortcut", "icon shortcut")
            if plain_icon and not icon_kept:
                icon_kept = True
                continue
            report.removed_cms_meta.append(f"<link rel={rel_str!r}> (лишняя иконка)")
            link.decompose()
            removed += 1
            continue
        if "alternate" in rel_str or link.get("hreflang"):
            report.removed_cms_meta.append(f"<link rel={rel_str!r} hreflang> {link.get('href','')}")
            link.decompose()
            removed += 1
            continue
        # прочие технические rel (manifest/amphtml/…) не трогаем, чтобы не сломать неизвестное
    return removed


_FONT_FACE_RULE_RE = re.compile(r"@font-face\s*\{[^{}]*\}", re.I)
_TYPEKIT_RULE_RE = re.compile(r"@import[^;]*typekit[^;]*;", re.I)


def _strip_font_loading_rules(css):
    """Drop only the webfont loading that CANNOT work offline - @font-face rules whose src points at
    a font CDN, and typekit @imports - KEEPING everything else, layout CSS and local fonts alike.

    Two bugs shaped this. First, dropping the whole <style> on any @font-face nuked CMS/Blogger
    skins that mix dozens of font rules into ~90KB of theme CSS (page rendered unstyled). Then the
    replacement stripped EVERY @font-face on the premise "we inject our own font anyway" - which
    stopped being true once the rule became "keep the font the archive had". On firsttalk.in that
    deleted the self-hosted Inter/Open Sans faces while their 11 .woff files sat right there in the
    folder, so the page then looked font-less and got flattened to Arial. A local @font-face is the
    archived font: keep it."""
    def _drop_if_external(m):
        rule = m.group(0)
        srcs = re.findall(r"url\(\s*['\"]?([^'\")]+)", rule, re.I)
        host_of = lambda u: safe_urlsplit(u).netloc.lower().lstrip("www.")
        if srcs and all(host_of(u) and any(host_of(u).endswith(h) for h in _FONT_CDN_HOSTS) for u in srcs):
            return ""  # every source is a dead CDN - the rule can never load anything locally
        return rule

    css = _FONT_FACE_RULE_RE.sub(_drop_if_external, css)
    css = _TYPEKIT_RULE_RE.sub("", css)
    return css


def clean_head_styles(soup, report, html_path):
    """Extract the site's OWN surviving inline <head><style> blocks into a standalone
    -custom.css. Must skip any <style> this tool injected itself (data-site-studio-
    font/-img) - those are regenerated fresh by their own step later in THIS SAME
    run, so sweeping them up here on a repeat cleanup pass would (a) overwrite
    -custom.css with just that leftover marker content, permanently losing whatever
    real site CSS was extracted from it the first time, and (b) still get a fresh
    copy re-added by inject_google_fonts/inject_image_object_fit_style right after -
    net result, real layout CSS silently destroyed on every re-run past the first."""
    head = soup.find("head")
    if not head:
        return
    kept_css_chunks = []
    for style_tag in head.find_all("style"):
        if style_tag.get("data-site-studio-font") or style_tag.get("data-site-studio-img"):
            continue
        css_text = FONT_IMPORT_RE.sub("", unwayback(style_tag.get_text()))
        for m in FONT_FAMILY_RE.finditer(css_text):
            report.font_families_found.add(m.group(1).strip().strip('"\''))
        # Strip only the font-loading rules, keep the layout. (Was: drop the whole block on any
        # @font-face - which destroyed CMS skins where fonts and 90KB of theme CSS share one block.)
        if "@font-face" in css_text or "use.typekit.net" in css_text:
            css_text = _strip_font_loading_rules(css_text)
        if re.search(r"green-outline|red-outline", css_text):
            style_tag.decompose()
            continue
        if css_text.strip():
            kept_css_chunks.append(css_text.strip())
        style_tag.decompose()

    if kept_css_chunks:
        css_path = html_path.with_name(f"{html_path.stem}-custom.css")
        css_path.write_text("\n\n".join(kept_css_chunks), encoding="utf-8")
        report.custom_css_written = css_path.name
        # A repeat run with fresh content to extract shouldn't pile up a second
        # <link> alongside the one a prior run already added for the same file.
        if not head.find("link", href=css_path.name):
            link_tag = soup.new_tag("link", rel="stylesheet", type="text/css", href=css_path.name)
            head.append(link_tag)


def normalize_font_family(family_param):
    """Google Fonts' css2 API is case-sensitive on the family name - 'family=jost'
    400s, 'family=Jost' works. Title-case only names typed fully lowercase (a
    careless-typing signal); leave anything with existing capitalization alone so a
    correctly-cased multi-word/acronym name (e.g. 'IBM Plex Mono') isn't mangled."""
    parts = []
    for f in family_param.split(","):
        f = f.strip()
        if not f:
            continue
        name, sep, rest = f.partition(":")
        if name and name == name.lower():
            name = name.title()
        parts.append(name + sep + rest)
    return ",".join(parts)


def font_param_from_name(name):
    """'Jost' -> 'Jost:wght@400;500;700' (the standard 3 weights). If the input already
    carries ':wght@...' it's kept (just case-normalized); empty -> empty.

    Empty must NOT become a default family: "no font asked for" means "keep the fonts the
    archived page already had", and returning Jost here would re-brand the site behind the
    caller's back."""
    name = (name or "").strip()
    if not name:
        return ""
    if ":" in name:
        return normalize_font_family(name)
    base = name.split(":")[0].strip()
    return normalize_font_family(f"{base}:wght@{FONT_WEIGHTS}")


def _local_stylesheet_paths(soup, html_path):
    paths = []
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href", "")
        if not href or is_external(href):
            continue
        css_path = (html_path.parent / href).resolve()
        if css_path.is_file() and css_path.suffix.lower() == ".css":
            paths.append(css_path)
    return paths


def _collect_font_family_tokens(css_text, out):
    for m in FONT_FAMILY_RE.finditer(css_text):
        first = m.group(1).split(",")[0].strip().strip("'\"")
        if first:
            out.append(first)


def detect_site_font(soup, html_path):
    """Look at the page's OWN css (surviving inline <head><style> blocks, plus every
    locally linked stylesheet - the theme's main CSS is almost always there, not just
    in <head>) for a font-family it already declares, so the cleanup keeps whatever
    typeface the site actually shipped with instead of dropping in an unrelated random
    preset. Only the first (non-fallback) name of each declaration is considered, and
    generic CSS keywords / classic OS-default stack fonts (Arial, Helvetica, Segoe UI,
    ...) are skipped - those aren't a deliberate brand choice, so if that's all a page
    has this returns None and the caller should fall back to a random preset."""
    tokens = []
    head = soup.find("head")
    if head:
        for style_tag in head.find_all("style"):
            # Skip OUR OWN injected font/image style from a previous run - otherwise a re-clean
            # detects the preset we last dropped in (e.g. Montserrat) instead of the site's real
            # font (Lato), and the wrong typeface sticks forever.
            if style_tag.has_attr("data-site-studio-font") or style_tag.has_attr("data-site-studio-img"):
                continue
            _collect_font_family_tokens(unwayback(style_tag.get_text()), tokens)
    scanned = set()
    for css_path in _local_stylesheet_paths(soup, html_path):
        _collect_font_family_tokens(read_text_safe(css_path), tokens)
        scanned.add(css_path)
    # Also scan every .css physically in the site folder. The theme's main CSS (where the real
    # brand font lives, e.g. landing-page.css -> "Lato") is often still linked by an ABSOLUTE
    # same-domain URL at this point (localize_media_refs rewrites those to local paths later),
    # so _local_stylesheet_paths - which skips is_external hrefs - would miss it. The file is on
    # disk regardless, so read it straight off disk.
    site_root = html_path.parent
    for css_path in sorted(site_root.rglob("*.css")):
        if css_path in scanned or css_path.name.endswith("-fonts.css"):
            continue  # -fonts.css is our OWN injected font CSS - never re-detect from it
        if any(part in QUARANTINE_DIR_NAMES for part in css_path.relative_to(site_root).parts):
            continue
        _collect_font_family_tokens(read_text_safe(css_path), tokens)

    # Rank candidates by HOW OFTEN the theme declares them, not by document order. The real
    # brand font is stated over and over (body, headings, buttons); a one-off like
    # "SFMono-Regular" in a code block would otherwise win just for appearing first.
    counts, first_seen = {}, {}
    for i, name in enumerate(tokens):
        key = name.lower()
        if key in GENERIC_FONT_KEYWORDS or key in SYSTEM_FONT_NAMES or key in ICON_FONT_NAMES:
            continue
        if any(hint in key for hint in ICON_FONT_HINTS):
            continue
        counts[key] = counts.get(key, 0) + 1
        first_seen.setdefault(key, (i, name))
    # A blocklist can't enumerate every OS-default/icon-font name a theme might use - ask Google
    # Fonts itself whether the family exists there (and get its canonical spelling) before
    # committing to it (avoids repeating the Glyphicons/Menlo mistake for the next unlisted one).
    for key in sorted(counts, key=lambda k: (-counts[k], first_seen[k][0])):
        canonical = _google_font_canonical(first_seen[key][1])
        if canonical:
            return font_param_from_name(canonical)
    return None


def _google_font_serves(name):
    """True if Google Fonts' css2 API actually serves this EXACT family spelling - a made-up/
    system/icon-font name 400s or comes back without any @font-face rule."""
    import urllib.error
    import urllib.parse
    import urllib.request

    try:
        url = f"https://fonts.googleapis.com/css2?family={urllib.parse.quote(name)}&display=swap"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200 and b"@font-face" in resp.read()
    except (urllib.error.URLError, OSError):
        return False


_GF_METADATA_URL = "https://fonts.google.com/metadata/fonts"


@functools.lru_cache(maxsize=1)
def _google_font_index():
    """{lowercased family -> canonical family} for EVERY Google Font, fetched once (~1900 names,
    <1s). Empty dict if unreachable - the caller then falls back to probing spellings."""
    import urllib.request

    try:
        req = urllib.request.Request(_GF_METADATA_URL, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            txt = resp.read().decode("utf-8", "replace")
        if txt.startswith(")]}'"):
            txt = txt.split("\n", 1)[1]  # strip Google's XSSI guard prefix
        data = json.loads(txt)
        return {f["family"].lower(): f["family"]
                for f in data.get("familyMetadataList", []) if f.get("family")}
    except Exception:  # noqa: BLE001 - offline/blocked/shape change: fall back to probing
        return {}


@functools.lru_cache(maxsize=512)
def _google_font_canonical(name):
    """The EXACT Google Fonts spelling of `name`, or None if Google doesn't have that family.

    CSS family names are case-INSENSITIVE, but the Google Fonts API is not: a theme that writes
    `font-family: poppins` (perfectly valid CSS) made the old existence probe return HTTP 400, so
    EVERY candidate "didn't exist" and the cleaner silently dropped in a random preset - the
    restored site lost its real typeface (danvanhaiphong: poppins -> Jost). Resolve through the
    canonical family list instead, which also fixes spellings no title-casing would guess
    ("pt sans" -> "PT Sans", "ibm plex sans" -> "IBM Plex Sans")."""
    n = (name or "").strip().strip("\"'")
    if not n:
        return None
    idx = _google_font_index()
    if idx:
        return idx.get(n.lower())
    # List unavailable (offline): probe a few plausible spellings directly.
    tried = []
    for cand in (n, n.title(),
                 " ".join(w.upper() if len(w) <= 3 else w.capitalize() for w in n.split())):
        if cand in tried:
            continue
        tried.append(cand)
        if _google_font_serves(cand):
            return cand
    return None


def resolve_font_input(raw, soup=None, html_path=None):
    """UI/CLI helper: empty -> use the font the ARCHIVED PAGE actually used; a bare name ('Jost')
    -> that name at the 3 standard weights; an explicit 'Family:wght@...' -> kept as typed.

    Returns "" when the page declares no Google-available brand font. That is deliberate: the
    restored page must look like the snapshot (a site set in Arial/Verdana keeps Arial/Verdana),
    so we do NOT drop in a random preset any more - that silently re-branded every plain site
    (danvanhaiphong lost Poppins to Jost, sanjhapunjab's system stack became Jost).
    inject_google_fonts() turns "" into "leave the site's own fonts alone"."""
    raw = (raw or "").strip()
    if raw:
        return font_param_from_name(raw)
    if soup is not None and html_path is not None:
        detected = detect_site_font(soup, html_path)
        if detected:
            return detected
    return ""


def _primary_font_family(fonts_param):
    """"Jost:wght@400;700,Inter" -> "Jost" - the family name typed in, GF API params
    stripped off, first family only (that's the one meant to actually apply site-wide)."""
    first = fonts_param.split(",")[0].strip()
    return first.split(":")[0].strip()


_FONT_FACE_URL_RE = re.compile(r"url\(([^)]+)\)\s*format\(\s*['\"]?([\w-]+)['\"]?\s*\)", re.IGNORECASE)
_FONT_FORMAT_EXTS = {"woff2": ".woff2", "woff": ".woff", "truetype": ".ttf", "opentype": ".otf"}


def _fetch_google_font_css(fonts_param):
    """Fetch the Google Fonts css2 API response for `fonts_param` with a modern-
    desktop-browser User-Agent, so it serves woff2 (the smallest/most broadly
    supported format) rather than a legacy fallback."""
    import urllib.request

    families = "&".join(f"family={fam.strip()}" for fam in fonts_param.split(",") if fam.strip())
    url = f"https://fonts.googleapis.com/css2?{families}&display=swap"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        },
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8")


def download_google_font_locally(fonts_param, dest_dir, html_root):
    """Download every font FILE the Google Fonts css2 API response for `fonts_param`
    references - one per weight x unicode-range subset, e.g. Latin/Cyrillic/Greek
    variants each get their own file - into `dest_dir`, and return the @font-face CSS
    text with every url() rewritten to the local file's path (relative to
    `html_root`, so it can be dropped straight into an inline <style> in the HTML
    document). Fully self-hosted: once this returns, nothing at request time ever
    touches fonts.googleapis.com/fonts.gstatic.com again. Raises on total failure
    (network down, Google 4xx, ...) - the caller decides the fallback."""
    css_text = _fetch_google_font_css(fonts_param)
    dest_dir.mkdir(parents=True, exist_ok=True)
    rel_dir = dest_dir.relative_to(html_root).as_posix()
    primary_slug = re.sub(r"[^a-z0-9]+", "-", _primary_font_family(fonts_param).lower()).strip("-") or "font"
    counter = 0

    def _sub(m):
        nonlocal counter
        url = m.group(1).strip("'\" ")
        fmt = m.group(2).lower()
        ext = _FONT_FORMAT_EXTS.get(fmt, ".woff2")
        # One unreachable weight must not abort the whole font (or the run): keep the original
        # url() for that face and carry on with the ones that did download.
        try:
            data = _fetch_url_bytes(url)
        except Exception:  # noqa: BLE001
            return m.group(0)
        if not data:
            return m.group(0)
        fname = f"{primary_slug}-{counter}{ext}"
        counter += 1
        (dest_dir / fname).write_bytes(data)
        return f"url('{rel_dir}/{fname}') format('{fmt}')"

    new_css, n = _FONT_FACE_URL_RE.subn(_sub, css_text)
    if n == 0:
        raise ValueError("Google Fonts response had no @font-face url() to download")
    return new_css


# Icon-font elements (Font Awesome / glyphicons) must be excluded from any global
# "* { font-family: ... !important }" reset - both the one this tool injects AND the site
# theme's own - or the icon's element gets the body font forced onto it, its glyph codepoint
# doesn't exist in that font, and every icon renders as a blank tofu box. The glyph is drawn
# in the ::before pseudo, so the universal ::before/::after resets have to be shielded too
# (the :not() goes on the element part, before the pseudo: `*:not(.fas)::before`).
_ICON_EXCLUDE_SEL = (
    ":not(.fa):not(.fas):not(.far):not(.fab):not(.fal):not(.fad)"
    ":not(.fa-solid):not(.fa-brands):not(.fa-regular):not(.glyphicon)"
    ':not([class^="fa-"]):not([class*=" fa-"])'
)
_FONT_RESET_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")


def _shield_sel_part(ps):
    """A universal selector a font reset hits every element/pseudo through (`*`, `*::before`,
    `*::after`) -> the same with icon exclusions inserted after the `*`; else None."""
    if ps == "*":
        return "*" + _ICON_EXCLUDE_SEL
    if ps in ("*::before", "*::after", "*:before", "*:after"):
        return "*" + _ICON_EXCLUDE_SEL + ps[1:]
    return None


def _shield_icons_from_font_resets(css_text):
    """Add icon exclusions to any universal `* { font-family: ... !important }` reset in
    `css_text` - not just the one this tool injects, but the site theme's own bare reset,
    which otherwise blanks every Font Awesome / glyphicon icon. Idempotent, returns the
    (possibly unchanged) text."""
    def repl(m):
        selector, body = m.group(1), m.group(2)
        low = body.lower()
        if "font-family" not in low or "!important" not in low:
            return m.group(0)
        shielded, changed = [], False
        for p in (part.strip() for part in selector.split(",")):
            sh = _shield_sel_part(p) if _ICON_EXCLUDE_SEL not in p else None
            if sh is not None:
                shielded.append(sh)
                changed = True
            else:
                shielded.append(p)
        return (",".join(shielded) + "{" + body + "}") if changed else m.group(0)

    return _FONT_RESET_RULE_RE.sub(repl, css_text)



def _strip_dead_font_links(soup, head, html_path):
    """Drop every OLD font connection left in <head> - the theme's own Google Fonts/Typekit
    <link>, leftover preconnect hints, and the LOCAL MIRROR wayback often saves of the Google
    Fonts CSS response (a file named just "css", so an href substring check never catches it).
    Without this the page double-loads two font services, or keeps hitting a dead one."""
    for link in list(head.find_all("link")):
        href = link.get("href") or ""
        rel = link.get("rel") or []
        rel = " ".join(rel) if isinstance(rel, list) else str(rel)
        if not ("stylesheet" in rel or "preconnect" in rel or "dns-prefetch" in rel):
            continue
        is_font_host = any(
            host in href.lower() for host in ("fonts.googleapis.com", "fonts.gstatic.com", *FONT_SERVICE_DOMAINS)
        )
        local_mirror_path = None
        if not is_font_host and html_path is not None and href and not is_external(href):
            local_path = (html_path.parent / href).resolve()
            if local_path.is_file() and local_path.stat().st_size < UNWAYBACK_SWEEP_MAX_BYTES:
                sniff = read_text_safe(local_path)[:2000]
                is_font_host = "@font-face" in sniff and (
                    "fonts.gstatic.com" in sniff or "fonts.googleapis.com" in sniff
                )
                if is_font_host:
                    local_mirror_path = local_path
        if is_font_host:
            link.decompose()
            if local_mirror_path is not None:
                # Quarantine the mirror file right away instead of leaving it for the
                # later "unused assets" text-search sweep to rediscover - a short,
                # generic filename like the "css" a saved Google Fonts response gets
                # saved as is too easy to false-positive-match as "still referenced"
                # inside some unrelated comment/URL elsewhere on the page.
                assets_dir = html_path.with_name(html_path.stem + "_files")
                try:
                    rel = local_mirror_path.relative_to(assets_dir)
                    dest = assets_dir / "_wayback_removed" / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(local_mirror_path), str(dest))
                except (ValueError, OSError):
                    pass  # outside assets_dir, or a move hiccup - leave it for the later sweep


def inject_google_fonts(soup, fonts_param, html_path=None):
    fonts_param = normalize_font_family(fonts_param)
    head = soup.find("head")
    if not head:
        return

    for tag in head.find_all(attrs={"data-site-studio-font": True}):
        tag.decompose()

    # No Google-available brand font on this page -> KEEP WHAT THE ARCHIVE HAD. Overriding a site
    # that was set in Arial/Verdana with some preset would change the very look we're restoring.
    # Only when the page declares no font at all do we state a plain Arial stack, so it isn't left
    # to the browser's default serif.
    if not fonts_param:
        _strip_dead_font_links(soup, head, html_path)
        # "Does this page state a font?" must look at the site's LOCAL STYLESHEETS too, not just at
        # the HTML. By this point clean_head_styles has already moved every inline <style> out into
        # <stem>-custom.css, so a site whose whole typography lives in CSS looks font-less here and
        # would be flattened to Arial - which is how firsttalk.in lost its self-hosted Inter/Open
        # Sans even though the .woff2 files were sitting in its own folder.
        declares_font = bool(FONT_FAMILY_RE.search(str(soup)))
        if not declares_font and html_path is not None:
            for css_path in _local_stylesheet_paths(soup, html_path):
                try:
                    if FONT_FAMILY_RE.search(css_path.read_text(encoding="utf-8", errors="replace")):
                        declares_font = True
                        break
                except OSError:
                    continue
        if not declares_font and html_path is not None:
            css_body = "* { font-family: Arial, Helvetica, sans-serif; }\n"
            fonts_css_path = html_path.with_name(f"{html_path.stem}-fonts.css")
            fonts_css_path.write_text(css_body, encoding="utf-8")
            link = soup.new_tag("link", rel="stylesheet", href=fonts_css_path.name)
            link["data-site-studio-font"] = "local"
            head.append(link)
        return

    _strip_dead_font_links(soup, head, html_path)

    # Self-hosted, not CDN: download the actual font FILES from Google Fonts into the
    # project and rewrite @font-face to local paths, instead of a live <link> to
    # fonts.googleapis.com that hits Google on every single page load. Falls back to
    # the live CDN link only if the download itself fails (offline, blocked, ...) or
    # no html_path was given to know where to save files - some connection is better
    # than the page silently rendering in a fallback font with no explanation.
    primary = _primary_font_family(fonts_param)

    def _try_selfhost(param):
        if html_path is None:
            return None
        try:
            dest_dir = html_path.with_name(html_path.stem + "_files") / "fonts"
            return download_google_font_locally(param, dest_dir, html_path.parent)
        except Exception:  # noqa: BLE001 - offline/blocked/not-a-Google-font
            return None

    local_css = _try_selfhost(fonts_param)
    if not local_css:
        # The requested family isn't downloadable (not on Google Fonts, or the fetch failed).
        # NEVER fall back to a live <link> to fonts.googleapis.com: that leaks an external
        # request on every page load and, for a non-Google family, just 403s and renders
        # nothing.
        #
        # And never fall back to a PRESET either. That used to drop in Jost/Montserrat and
        # push a `* { font-family: Jost !important }` override, silently RE-BRANDING a site
        # whose own font was perfectly fine - one flaky font download was enough to lose
        # danvanhaiphong's Poppins. The rule is: keep whatever the archive used. If we cannot
        # self-host, we touch nothing and the site's own CSS keeps rendering its own fonts.
        return

    parts = []
    if local_css:
        parts.append(local_css.strip())
    # else: no external link at all - the font-family rule below still applies with its
    # generic sans-serif fallback, and the page stays free of third-party requests.
    # Loading the font isn't enough - the site's own CSS still references whatever
    # font-family the original theme used, so nothing would actually render in the
    # new font without a global override. Exclude icon-font elements (see _ICON_EXCLUDE_SEL)
    # so this !important doesn't clobber their font-family and blank every icon.
    if primary:
        parts.append(f"*{_ICON_EXCLUDE_SEL} {{ font-family: '{primary}', sans-serif !important; }}")
    if not parts:
        return
    css_body = "\n".join(parts) + "\n"
    if html_path is not None:
        # Write the font CSS to a SEPARATE stylesheet and LINK it (not an inline <head><style>);
        # _reorder_head_seo then places this <link> right after <link canonical>.
        fonts_css_path = html_path.with_name(html_path.stem + "-fonts.css")
        fonts_css_path.write_text(css_body, encoding="utf-8")
        link = soup.new_tag("link", rel="stylesheet", href=fonts_css_path.name)
        link["data-site-studio-font"] = "local"
        head.append(link)
    else:
        style_tag = soup.new_tag("style")
        style_tag["data-site-studio-font"] = "local"
        style_tag.string = css_body
        head.append(style_tag)


def inject_image_object_fit_style(soup):
    """Global img { object-fit: cover } so any image forced into a fixed-size slot
    (gallery thumbs, cards, etc.) crops to fill instead of stretching/distorting."""
    head = soup.find("head")
    if not head:
        return
    for tag in head.find_all(attrs={"data-site-studio-img": True}):
        tag.decompose()
    style_tag = soup.new_tag("style")
    style_tag["data-site-studio-img"] = "cover"
    style_tag.string = "img { object-fit: cover; }"
    head.append(style_tag)


# --- icon fonts -----------------------------------------------------------------------
# Old wayback exports almost always ship BROKEN icons: the icon webfont files (Font
# Awesome's fontawesome-webfont.*, Bootstrap's glyphicons-halflings-regular.*) weren't
# captured, so every icon renders as an empty box. Fix: self-host a known-good Font Awesome
# 4.7 (whose "fa fa-*" markup is exactly what these templates use - FA5+ split it into
# fas/fab and would break the existing markup) locally from cdnjs, and remap Bootstrap
# glyphicons onto the same Font Awesome classes so they share that one working font.
FA_CDN = "https://cdnjs.cloudflare.com/ajax/libs/font-awesome"
# Match the Font Awesome MAJOR to the markup: v4 uses one base class "fa" (fa fa-home);
# v5/6 split it into style prefixes (fas/far/fab). Self-hosting the wrong major leaves icons
# blank because the base class the CSS keys on (.fa vs .fas) doesn't match the elements.
FA_VARIANTS = {
    "fa4": {"version": "4.7.0", "css": "css/font-awesome.min.css", "glyph_prefix": "fa"},
    "fa6": {"version": "6.5.2", "css": "css/all.min.css", "glyph_prefix": "fas"},
}
_FA_ICON_TOKEN_RE = re.compile(r"^fa-[a-z0-9-]+$", re.I)
_FA4_BASE_RE = re.compile(r"^fa$", re.I)
_FA5_PREFIX_RE = re.compile(
    r"^(fas|far|fab|fal|fad|fass|fa-solid|fa-brands|fa-regular|fa-light|fa-duotone)$", re.I)
# Icon webfont FILE names - never try to recover these from the archive (they're framework/
# CDN assets that were never captured; a burst of failing lookups is exactly what made
# icon-heavy sites crawl). Covers FA4 (fontawesome-webfont), FA5/6 (fa-solid-900/
# fa-brands-400/fa-v4compatibility/...) and Bootstrap glyphicons.
_ICON_WEBFONT_RE = re.compile(
    r"(glyphicons?-halflings|fontawesome|font-awesome|fa-solid-\d|fa-brands-\d"
    r"|fa-regular-\d|fa-light-\d|fa-duotone-\d|fa-v4compat)", re.I)

_FONTFACE_BLOCK_RE = re.compile(r"@font-face\s*\{([^{}]*)\}", re.I)
_SRC_DECL_RE = re.compile(r"src\s*:[^;{}]*;?", re.I)
_WOFF2_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")?]+\.woff2)[^'\")]*['\"]?\s*\)", re.I)
_DBL_SEMI_RE = re.compile(r";\s*;+")


def _fontface_woff2_only(css):
    """Rewrite every @font-face in `css` to reference ONLY its woff2 url (dropping any eot/
    svg/woff/ttf), so we ship just woff2 files - covers every modern browser, tiny download,
    zero console 404s. Works across FA4 (one @font-face) and FA5/6 (several)."""
    def repl(m):
        body = m.group(1)
        w = _WOFF2_URL_RE.search(body)
        if not w:
            return m.group(0)
        new_body = _DBL_SEMI_RE.sub(";", _SRC_DECL_RE.sub("", body)).strip().strip(";").strip()
        src = f"src:url('{w.group(1)}') format('woff2')"
        return "@font-face{" + (new_body + ";" if new_body else "") + src + "}"
    return _FONTFACE_BLOCK_RE.sub(repl, css)

GLYPHICON_TO_FA = {
    "ok": "check", "remove": "times", "plus-sign": "plus-circle", "minus-sign": "minus-circle",
    "remove-sign": "times-circle", "ok-sign": "check-circle", "question-sign": "question-circle",
    "info-sign": "info-circle", "exclamation-sign": "exclamation-circle", "warning-sign": "warning",
    "ok-circle": "check-circle-o", "remove-circle": "times-circle-o", "ban-circle": "ban",
    "zoom-in": "search-plus", "zoom-out": "search-minus", "off": "power-off", "trash": "trash-o",
    "time": "clock-o", "star-empty": "star-o", "heart-empty": "heart-o", "eye-open": "eye",
    "eye-close": "eye-slash", "picture": "picture-o", "facetime-video": "video-camera",
    "edit": "pencil-square-o", "share": "share-square-o", "check": "check-square-o",
    "move": "arrows", "resize-full": "expand", "resize-small": "compress",
    "resize-vertical": "arrows-v", "resize-horizontal": "arrows-h", "fullscreen": "arrows-alt",
    "screenshot": "crosshairs", "menu-hamburger": "bars", "menu-left": "chevron-left",
    "menu-right": "chevron-right", "menu-up": "chevron-up", "menu-down": "chevron-down",
    "option-vertical": "ellipsis-v", "option-horizontal": "ellipsis-h",
    "triangle-right": "caret-right", "triangle-left": "caret-left", "triangle-top": "caret-up",
    "triangle-bottom": "caret-down", "log-in": "sign-in", "log-out": "sign-out",
    "new-window": "external-link", "folder-close": "folder", "floppy-disk": "floppy-o",
    "floppy-save": "floppy-o", "save": "floppy-o", "open": "folder-open-o", "saved": "check",
    "send": "paper-plane", "import": "sign-in", "export": "sign-out", "transfer": "exchange",
    "list-alt": "list-alt", "indent-left": "outdent", "indent-right": "indent",
    "hand-right": "hand-o-right", "hand-left": "hand-o-left", "hand-up": "hand-o-up",
    "hand-down": "hand-o-down", "circle-arrow-right": "arrow-circle-right",
    "circle-arrow-left": "arrow-circle-left", "circle-arrow-up": "arrow-circle-up",
    "circle-arrow-down": "arrow-circle-down", "play-circle": "play-circle-o",
    "unchecked": "square-o", "pushpin": "thumb-tack", "dashboard": "tachometer",
    "stats": "bar-chart", "sort-by-alphabet": "sort-alpha-asc", "flash": "bolt",
    "earphone": "phone", "phone-alt": "phone", "tower": "building-o",
    "registration-mark": "registered", "grain": "th", "header": "header",
    "compressed": "file-archive-o", "tree-conifer": "tree", "tree-deciduous": "tree",
    "cd": "circle-o-notch", "sd-video": "video-camera", "hd-video": "video-camera",
    "subtitles": "cc",
}


def _class_tokens(tag):
    c = tag.get("class")
    if not c:
        return []
    return list(c) if isinstance(c, list) else str(c).split()


def _detect_icon_font(soup):
    """('fa4'|'fa6'|None, glyphicon_tags): which Font Awesome major the page's icon markup
    uses (fas/far/fab = v5/6 -> 'fa6'; plain 'fa fa-*' = v4 -> 'fa4'), plus every element
    using Bootstrap glyphicons. Prefers fa6 if both styles somehow appear."""
    fa4 = fa6 = False
    glyph_tags = []
    for tag in soup.find_all(True):
        toks = _class_tokens(tag)
        if not toks:
            continue
        if any(_FA_ICON_TOKEN_RE.match(t) for t in toks):
            if any(_FA5_PREFIX_RE.match(t) for t in toks):
                fa6 = True
            elif any(_FA4_BASE_RE.match(t) for t in toks):
                fa4 = True
        if "glyphicon" in toks:
            glyph_tags.append(tag)
    ver = "fa6" if fa6 else ("fa4" if fa4 else None)
    return ver, glyph_tags


# Custom icon-font classes (end2end-icons, themify, ionicons, a theme's own set...): <i class=
# "icon icon-twitter">. The webfont behind them is virtually never archived, so they render as
# empty squares. The NAME, though, is right there - remap it onto the Font Awesome we self-host.
_CUSTOM_ICON_RE = re.compile(r"^(?:icon|ico|icn|fi|if|e2e|social)-([a-z0-9][a-z0-9-]*)$", re.I)
_CUSTOM_ICON_BASE_RE = re.compile(r"^(?:icon|ico|icn|fi|if|e2e|social|icons?)$", re.I)
# names FA puts in the BRANDS font (fab); everything else is solid (fas)
_FA_BRAND_NAMES = {
    "twitter", "x-twitter", "facebook", "facebook-f", "instagram", "linkedin", "linkedin-in",
    "github", "gitlab", "youtube", "vimeo", "pinterest", "reddit", "tumblr", "whatsapp",
    "telegram", "skype", "snapchat", "tiktok", "discord", "slack", "dribbble", "behance",
    "flickr", "soundcloud", "spotify", "medium", "wordpress", "google", "google-plus",
    "google-plus-g", "apple", "android", "windows", "amazon", "paypal", "stack-overflow",
    "quora", "vk", "weibo", "wechat", "line", "viber", "rss",
}
# custom-set name -> FA name, where they differ
_CUSTOM_ICON_TO_FA = {
    "mail": "envelope", "email": "envelope", "letter": "envelope",
    "tel": "phone", "telephone": "phone", "call": "phone", "mobile": "mobile-screen",
    "pin": "location-dot", "map": "location-dot", "marker": "location-dot",
    "location": "location-dot", "place": "location-dot", "address": "location-dot",
    "time": "clock", "date": "calendar", "cart": "cart-shopping", "basket": "cart-shopping",
    "magnifier": "magnifying-glass", "zoom": "magnifying-glass", "search": "magnifying-glass",
    "user": "user", "profile": "user", "people": "users", "team": "users",
    "gplus": "google-plus-g", "googleplus": "google-plus-g", "fb": "facebook-f",
    "tw": "twitter", "in": "linkedin-in", "yt": "youtube", "ig": "instagram",
    "feed": "rss", "arrow-right": "arrow-right", "arrow-left": "arrow-left",
    "close": "xmark", "cross": "xmark", "menu": "bars", "burger": "bars",
    "heart": "heart", "star": "star", "check": "check", "download": "download",
    "link": "link", "share": "share-nodes", "comment": "comment", "quote": "quote-left",
    "home": "house", "info": "circle-info", "help": "circle-question",
}


def _detect_custom_icons(soup):
    """Elements using a NON-Font-Awesome icon-font class (icon-twitter, e2e-mail, ...). Skips
    anything already on Font Awesome or glyphicons - those have their own paths."""
    out = []
    for tag in soup.find_all(["i", "span", "a", "em"]):
        toks = _class_tokens(tag)
        if not toks or "glyphicon" in toks:
            continue
        if any(_FA_ICON_TOKEN_RE.match(t) or _FA5_PREFIX_RE.match(t) or _FA4_BASE_RE.match(t)
               for t in toks):
            continue  # already Font Awesome
        if any(_CUSTOM_ICON_RE.match(t) for t in toks):
            out.append(tag)
    return out


def rewrite_custom_icons_to_fa(soup, tags, report, prefix="fas"):
    """Rewrite a custom icon set onto Font Awesome in place: <i class="icon icon-twitter"> ->
    <i class="fab fa-twitter">. The base token (icon/ico/e2e...) becomes the FA style prefix -
    `fab` when the name is one of FA's brands, else `prefix` (fas) - and icon-<name> becomes
    fa-<mapped name>. The site keeps its own markup and layout; only the classes change, so the
    icons stop being empty squares once our self-hosted Font Awesome is linked."""
    n = 0
    for tag in tags:
        name = None
        for t in _class_tokens(tag):
            m = _CUSTOM_ICON_RE.match(t)
            if m:
                name = m.group(1).lower()
                break
        if not name:
            continue
        fa_name = _CUSTOM_ICON_TO_FA.get(name, name)
        style = "fab" if fa_name in _FA_BRAND_NAMES else prefix
        new = [t for t in _class_tokens(tag)
               if not _CUSTOM_ICON_RE.match(t) and not _CUSTOM_ICON_BASE_RE.match(t)]
        new += [style, "fa-" + fa_name]
        tag["class"] = new
        n += 1
    report.custom_icons_rewritten += n
    return n


def rewrite_glyphicons_to_fa(soup, glyph_tags, report, prefix="fa"):
    """Rewrite Bootstrap glyphicon classes to Font Awesome in place (base 'glyphicon' ->
    `prefix`, 'glyphicon-x' -> 'fa-<mapped>'), so a self-hosted Font Awesome covers them too
    (the glyphicons webfont is almost always missing). Unmapped names fall back to same-name."""
    n = 0
    for tag in glyph_tags:
        new, changed = [], False
        for t in _class_tokens(tag):
            if t == "glyphicon":
                new.append(prefix)
                changed = True
            elif t.startswith("glyphicon-"):
                fa = GLYPHICON_TO_FA.get(t[10:], t[10:])
                if fa:
                    new.append("fa-" + fa)
                changed = True
            else:
                new.append(t)
        if changed:
            tag["class"] = new
            n += 1
    report.glyphicons_rewritten += n
    return n


def _ensure_fa_cache(variant):
    """Download the given Font Awesome variant once into a shared user cache (css rewritten to
    woff2-only, plus exactly the woff2 files it references) and return the cache dir, or None
    on failure. Shared across every site: on a batch of many domains only the FIRST cleanup
    hits the network; every site after just copies from the cache instantly."""
    cfg = FA_VARIANTS[variant]
    cache = Path.home() / ".cache" / "cms-webarchive-fa" / f"{variant}-{cfg['version']}"
    css_c = cache / cfg["css"]
    if css_c.is_file():
        return cache
    try:
        css_bytes = _fetch_url_bytes(f"{FA_CDN}/{cfg['version']}/{cfg['css']}", timeout=15)
    except Exception:  # noqa: BLE001
        return None
    css_text = _fontface_woff2_only(css_bytes.decode("utf-8", "replace"))
    css_c.parent.mkdir(parents=True, exist_ok=True)
    css_c.write_text(css_text, encoding="utf-8")
    refs = set(re.findall(r"url\(\s*['\"]?([^'\")?]+\.woff2)", css_text, re.I))
    from concurrent.futures import ThreadPoolExecutor

    def _grab(ref):
        try:
            url = f"{FA_CDN}/{cfg['version']}/" + ref.replace("../", "")
            dest = (css_c.parent / ref).resolve()
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(_fetch_url_bytes(url, timeout=15))
        except Exception:  # noqa: BLE001 - a missing font just means those glyphs won't show
            pass

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(_grab, refs))
    return cache if any((css_c.parent / r).exists() for r in refs) else None


def self_host_font_awesome(html_path, soup, report, variant, dry_run=False):
    """Self-host the matching Font Awesome `variant` (fa4/fa6) in the project and link it
    locally, replacing any broken/CDN Font Awesome <link> - so icons render from a working
    local font whose base classes (.fa vs .fas/.fab) match the markup. Copies from the shared
    cache (downloaded once, see _ensure_fa_cache); skips if the project already has it."""
    head = soup.find("head")
    if not head:
        return
    cfg = FA_VARIANTS[variant]
    assets_dir = html_path.with_name(html_path.stem + "_files")
    fa_dir = assets_dir / "fontawesome"
    css_path = fa_dir / cfg["css"]
    rel_href = f"{assets_dir.name}/fontawesome/{cfg['css']}"

    # Drop the export's pre-existing Font Awesome stylesheet link (a broken local copy, a CDN
    # one, or the FA5/6 "all.min.css") - we add our own known-good local one. The orphaned old
    # file is then swept into _unused_removed by the unused-asset pass at the end of cleanup.
    patterns = r"font-?awesome|fontawesome" + (r"|all\.min\.css" if variant == "fa6" else "")
    for link in list(head.find_all("link")):
        href = link.get("href") or ""
        if href != rel_href and re.search(patterns, href, re.I):
            report.removed_links_css.append(href)
            link.decompose()

    if not dry_run and not css_path.is_file():
        cache = _ensure_fa_cache(variant)
        if cache is None:
            print("[icons] could not obtain Font Awesome - icons left as-is")
            return
        shutil.copytree(cache, fa_dir, dirs_exist_ok=True)
        print(f"[icons] self-hosted Font Awesome {cfg['version']} locally (from cache)")

    if not head.find("link", href=rel_href):
        head.append(soup.new_tag("link", rel="stylesheet", href=rel_href))
    report.icon_font_selfhosted = True


def ensure_icon_fonts(soup, html_path, report, dry_run=False):
    """If the page uses icons (Font Awesome and/or Bootstrap glyphicons), make them actually
    render: detect the Font Awesome MAJOR from the markup, remap glyphicons onto it, and
    self-host that matching Font Awesome locally. No-op (no network) on pages with no icons."""
    ver, glyph_tags = _detect_icon_font(soup)
    # A theme's OWN icon font (icon-twitter, e2e-*, ...) is never archived, so those icons render
    # as empty squares. The names are usable though - remap them onto the Font Awesome we
    # self-host anyway. Needs FA5/6 (brand icons like fab fa-twitter don't exist in FA4).
    custom_tags = _detect_custom_icons(soup)
    if not ver and not glyph_tags and not custom_tags:
        return
    if custom_tags and not ver:
        ver = "fa6"
    if glyph_tags and not ver:
        ver = "fa4"  # glyphicons are Bootstrap-3 era - pair them with FA4
    if glyph_tags:
        rewrite_glyphicons_to_fa(soup, glyph_tags, report, prefix=FA_VARIANTS[ver]["glyph_prefix"])
    if custom_tags:
        if ver == "fa4":
            ver = "fa6"  # brands need FA5+; upgrade rather than ship dead classes
        rewrite_custom_icons_to_fa(soup, custom_tags, report,
                                   prefix=FA_VARIANTS[ver]["glyph_prefix"])
    self_host_font_awesome(html_path, soup, report, ver, dry_run=dry_run)

    # Shield icons from EVERY global "* { font-family: ... !important }" reset - ours is
    # already scoped, but the site theme almost always ships its own unscoped one that would
    # otherwise blank every icon. Patch it in the surviving inline <style> blocks and in every
    # linked local stylesheet (e.g. the extracted -custom.css).
    for st in soup.find_all("style"):
        txt = st.string if st.string is not None else st.get_text()
        if txt and "font-family" in txt:
            new = _shield_icons_from_font_resets(txt)
            if new != txt:
                st.string = new
    if not dry_run:
        for css_path in _local_stylesheet_paths(soup, html_path):
            try:
                txt = read_text_safe(css_path)
            except OSError:
                continue
            if "font-family" not in txt:
                continue
            new = _shield_icons_from_font_resets(txt)
            if new != txt:
                css_path.write_text(new, encoding="utf-8")


# A restored theme's scripts almost always assume jQuery, but the library itself is frequently
# NOT in the export (CDN-loaded originally, or dropped as an unclassified/minified bundle). The
# page then throws "jQuery is not defined" and every one of its behaviours is dead. Self-host it.
JQUERY_VERSION = "3.7.1"
JQUERY_CDN = f"https://code.jquery.com/jquery-{JQUERY_VERSION}.min.js"
# calls that prove a script NEEDS jQuery
_JQ_USE_RE = re.compile(r"jQuery\s*\(|\$\s*\(\s*(?:document|function|window|['\"])|\$\.\w+\s*\(")
# a file that IS jQuery (or bundles it) - then nothing needs adding
_JQ_DEFINES_RE = re.compile(r"window\.jQuery\s*=|jQuery\s*=\s*function|\.fn\.jquery\s*=|"
                            r"jQuery\.fn\.jquery\s*=", re.I)
_JQ_FILENAME_RE = re.compile(r"jquery[.-][\w.]*\.js$|^jquery\.js$", re.I)


def _ensure_jquery_cache():
    """Download jQuery once into a shared user cache and return the file, or None. Shared across
    every site, like the Font Awesome cache: only the first cleanup hits the network."""
    cache = Path.home() / ".cache" / "cms-webarchive-jq" / JQUERY_VERSION
    dest = cache / "jquery.min.js"
    if dest.is_file() and dest.stat().st_size > 20000:
        return dest
    try:
        data = _fetch_url_bytes(JQUERY_CDN, timeout=15)
    except Exception:  # noqa: BLE001 - offline/blocked: leave the page as-is
        return None
    if not data or len(data) < 20000 or b"jQuery" not in data[:4000]:
        return None
    cache.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(data)
    return dest


def ensure_jquery(soup, html_path, report, dry_run=False):
    """If anything still on the page uses jQuery but no jQuery is loaded, self-host it locally and
    link it FIRST. Runs after the script passes, so it judges the scripts that actually survived."""
    scripts = list(soup.find_all("script"))
    if not scripts:
        return
    site_root = html_path.parent
    uses = loaded = False
    for tag in scripts:
        src = (tag.get("src") or "").split("?")[0]
        if src:
            if _JQ_FILENAME_RE.search(src.rsplit("/", 1)[-1]):
                loaded = True
                continue
            if src.startswith(("http://", "https://", "//")):
                continue  # external ref (kept library) - can't inspect, assume it isn't jQuery
            p = (site_root / src)
            if not p.is_file():
                continue
            text = read_text_safe(p)[:400000]
        else:
            text = tag.string or tag.get_text() or ""
        if _JQ_DEFINES_RE.search(text):
            loaded = True
        elif _JQ_USE_RE.search(text):
            uses = True
    if not uses or loaded:
        return

    cached = _ensure_jquery_cache()
    if cached is None:
        return
    assets_dir = html_path.with_name(html_path.stem + "_files")
    dest = assets_dir / "jquery.min.js"
    rel = f"{assets_dir.name}/{dest.name}"
    if not dry_run:
        assets_dir.mkdir(parents=True, exist_ok=True)
        if not dest.is_file():
            shutil.copyfile(cached, dest)
    tag = soup.new_tag("script", src=rel)
    # Must run BEFORE the scripts that use it. The inline bundle we emit is deferred, and defer
    # keeps document order, so putting jQuery ahead of every other script is enough - it is
    # already executed by the time anything else runs.
    first = soup.find("script")
    if first is not None:
        first.insert_before(tag)
    else:
        (soup.find("head") or soup.find("body")).append(tag)
    report.jquery_selfhosted = f"{JQUERY_VERSION} -> {rel} (page used jQuery with none loaded)"


# Scroll-reveal libraries (WOW.js, AOS, ScrollReveal, animate.css wrappers) hide an element and
# only un-hide it when JS sees it scroll into view. We strip that JS, so anything the visitor
# hadn't scrolled to at snapshot time stays hidden FOREVER - whole bands of the page render blank
# (tuonggohungthinh: 38 hidden blocks, cards simply absent).
_REVEAL_CLASS_RE = re.compile(
    r"(?:^|\s)(?:wow|animated|animate__animated|aos-init|sr|scrollreveal|reveal|"
    r"fade-?up|fade-?in|slide-?up|slide-?in|zoom-?in)(?:\s|$)", re.I)
_REVEAL_ATTRS = ("data-wow-delay", "data-wow-duration", "data-wow-offset", "data-wow-iteration",
                 "data-aos", "data-aos-delay", "data-aos-duration", "data-sr-id", "data-scroll-reveal")
# the declarations that keep it invisible - dropped so the element falls back to its normal state
_REVEAL_HIDE_DECL_RE = re.compile(
    r"\s*(?:visibility\s*:\s*hidden|opacity\s*:\s*0(?:\.0+)?|animation-name\s*:\s*none)\s*;?", re.I)
# CSS-side equivalent, for themes that hide via a stylesheet rule instead of an inline style
_CSS_REVEAL_HIDE_RE = re.compile(
    r"(\.wow|\[data-aos\][^{,]*|\.scrollreveal)([^{]*)\{([^}]*)\}", re.I)


def unhide_scroll_reveal(soup, report, html_path=None):
    """Make scroll-reveal content visible again. The animation library's JS is gone, so its
    "hidden until scrolled into view" state is permanent - this returns the element to its
    revealed state DETERMINISTICALLY (no JS re-added, nothing re-downloaded).

    We only strip the *hiding* declarations, never the animation itself: the class (e.g.
    `wow fadeInUp`) plus the site's own animate.css keyframes then drive the animation on load,
    so the page still looks alive. Where the keyframes aren't archived the element simply shows -
    which is the whole point: visible-without-animation beats invisible.

    Deliberately targeted: an element must carry a reveal-library marker (class or data-attr).
    Deliberate off-screen hiding (`position:absolute; left:-9999px` a11y text, bambooship) and
    normal hidden dropdowns/modals are left completely alone."""
    n = 0
    for tag in soup.find_all(True):
        style = tag.get("style") or ""
        cls = " ".join(tag.get("class", []))
        is_reveal = bool(_REVEAL_CLASS_RE.search(cls)) or any(tag.has_attr(a) for a in _REVEAL_ATTRS)
        if not is_reveal:
            continue
        if tag.has_attr("data-aos"):
            # AOS reveals via a class its own CSS keys off - add it rather than fight the stylesheet
            classes = tag.get("class", [])
            if "aos-animate" not in classes:
                tag["class"] = classes + ["aos-animate"]
                n += 1
        if not style or not _REVEAL_HIDE_DECL_RE.search(style):
            continue
        new_style = _REVEAL_HIDE_DECL_RE.sub("", style).strip().strip(";").strip()
        if new_style:
            tag["style"] = new_style
        else:
            del tag["style"]
        n += 1
    if n:
        report.reveal_unhidden = n
    # Some themes hide the reveal elements from a stylesheet instead ( .wow{visibility:hidden} ).
    # Without the JS that rule is permanent too - neutralize just that declaration.
    if html_path is not None:
        for css_path in sorted(html_path.parent.rglob("*.css")):
            if any(part in QUARANTINE_DIR_NAMES for part in css_path.relative_to(html_path.parent).parts):
                continue
            try:
                txt = read_text_safe(css_path)
            except OSError:
                continue
            if ".wow" not in txt and "data-aos" not in txt:
                continue

            def _fix(m):
                body = _REVEAL_HIDE_DECL_RE.sub("", m.group(3))
                return f"{m.group(1)}{m.group(2)}{{{body}}}"

            new = _CSS_REVEAL_HIDE_RE.sub(_fix, txt)
            if new != txt:
                try:
                    css_path.write_text(new, encoding="utf-8")
                except OSError:
                    pass
    return n


# Sliders/carousels are JS widgets. We strip the JS, so whatever state the library had written
# into the DOM at snapshot time freezes there - and that state is usually NOT "showing slide 1".
_SLIDER_TRACK_RE = re.compile(
    r"(?:^|\s)(?:owl-stage|swiper-wrapper|slick-track|carousel-inner|bxslider|flexslider)(?:\s|$)", re.I)
_SLIDER_BOX_RE = re.compile(
    r"(?:^|\s)(?:owl-carousel|owl-stage|swiper|slick|carousel|n2-ss|n2-section-smartslider|"
    r"flexslider|bx-wrapper|rev_slider|tp-banner|elementor-background-slideshow|"
    r"ls-container|master-slider)[\w-]*(?:\s|$)", re.I)
_TRANSFORM_DECL_RE = re.compile(r"\s*(?:-webkit-)?transform\s*:[^;]*;?|\s*(?:-webkit-)?transition\s*:[^;]*;?", re.I)
_CAROUSEL_ITEM_RE = re.compile(r"(?:^|\s)carousel-item(?:\s|$)", re.I)
_BS3_ITEM_RE = re.compile(r"(?:^|\s)item(?:\s|$)", re.I)
# One slide, whatever the library calls it.
_SLIDE_ITEM_RE = re.compile(
    r"(?:^|\s)(?:owl-item|slick-slide|swiper-slide|carousel-item|flex-active-slide|ls-slide)(?:\s|$)", re.I)
# The edge-duplicate marker each looping library uses.
_SLIDE_CLONE_RE = re.compile(
    r"(?:^|\s)(?:cloned|clone|slick-cloned|swiper-slide-duplicate[\w-]*|bx-clone)(?:\s|$)", re.I)
_INLINE_HIDDEN_RE = re.compile(r"\s*display\s*:\s*none\s*;?", re.I)


def fix_static_sliders(soup, report):
    """Leave every slider in a sane STATIC state - it must never render as a blank band.

    Three things the stripped JS leaves behind, all fixed deterministically (no library is
    re-added, nothing is downloaded):

    1. Bootstrap carousel with no `.active` slide. The CSS only shows `.carousel-item.active`, so
       with none marked the whole carousel renders EMPTY - kyx.vn had 4 slides and 12 images and
       displayed nothing. Activate the first slide (and its indicator).
    2. A track frozen mid-scroll: the library wrote `transform: translate3d(-1234px,0,0)` on
       .owl-stage/.swiper-wrapper, so slide 1 sits off-screen and the visitor sees blank inside an
       overflow:hidden box. Drop that inline transform/transition so the track starts at slide 1.
    3. A slider whose slides were built by JS from JSON (Smart Slider 3, Elementor slideshow):
       nothing is in the HTML at all, leaving a tall empty band (bambooship: 500px of nothing).
       Nothing can be restored, so collapse the empty shell instead of shipping a hole.
    """
    activated = untracked = collapsed = 0

    for box in soup.find_all(class_=re.compile(r"carousel", re.I)):
        # Bootstrap 4/5 call a slide `.carousel-item`; Bootstrap 3 - very common in archived
        # sites - calls it plain `.item`. Matching only the modern name silently skipped every
        # BS3 carousel, which renders just as blank without an `.active` slide.
        items = box.find_all(class_=_CAROUSEL_ITEM_RE)
        if not items:
            inner = box if "carousel-inner" in " ".join(box.get("class") or []).lower() \
                else box.find(class_=re.compile(r"(?:^|\s)carousel-inner(?:\s|$)", re.I))
            if inner is not None:
                items = inner.find_all(class_=_BS3_ITEM_RE, recursive=False) \
                    or inner.find_all(class_=_BS3_ITEM_RE)
        if not items or any("active" in (i.get("class") or []) for i in items):
            continue
        items[0]["class"] = list(items[0].get("class", [])) + ["active"]
        activated += 1
        ind = box.find_all(attrs={"data-bs-slide-to": True}) or box.find_all(attrs={"data-slide-to": True})
        if ind and not any("active" in (x.get("class") or []) for x in ind):
            ind[0]["class"] = list(ind[0].get("class", [])) + ["active"]

    # Every looping slider duplicates its edge slides so the wrap-around looks seamless. That is a
    # RUNTIME trick and each library names it differently - Owl `.cloned`, Slick `.slick-cloned`,
    # Swiper `.swiper-slide-duplicate`, bxSlider `.bx-clone`, FlexSlider `.clone`. With the JS
    # stripped these stay in the DOM as visible DUPLICATE content (the same logo or testimonial
    # rendered twice). Drop them - but only while a real, non-clone slide survives, so a slider
    # whose every item happens to be marked as a clone is never emptied.
    decloned = 0
    for track in soup.find_all(class_=_SLIDER_TRACK_RE):
        items = track.find_all(class_=_SLIDE_ITEM_RE)
        clones = [i for i in items if _SLIDE_CLONE_RE.search(" ".join(i.get("class") or []))]
        if not clones or len(clones) >= len(items):
            continue
        for c in clones:
            c.decompose()
            decloned += 1

    # Same failure mode as the missing `.active` above, but library-agnostic: some sliders hide
    # every slide but the current one with an INLINE `display:none` and reveal it from JS. With
    # the JS gone all of them stay hidden and the slider is a blank band. If literally every slide
    # is inline-hidden (i.e. nothing is left to show), un-hide the first one. The all-hidden guard
    # is what keeps this from touching a slider that already has a visible slide.
    for track in soup.find_all(class_=_SLIDER_TRACK_RE):
        slides = [s for s in track.find_all(class_=_SLIDE_ITEM_RE)
                  if not _SLIDE_CLONE_RE.search(" ".join(s.get("class") or []))]
        if not slides or not all(_INLINE_HIDDEN_RE.search(s.get("style") or "") for s in slides):
            continue
        first = slides[0]
        newstyle = _INLINE_HIDDEN_RE.sub("", first.get("style") or "").strip().strip(";").strip()
        if newstyle:
            first["style"] = newstyle
        else:
            del first["style"]
        activated += 1

    for track in soup.find_all(class_=_SLIDER_TRACK_RE):
        style = track.get("style") or ""
        if "transform" not in style.lower() and "transition" not in style.lower():
            continue
        new = _TRANSFORM_DECL_RE.sub("", style).strip().strip(";").strip()
        if new:
            track["style"] = new
        else:
            del track["style"]
        untracked += 1

    # Collapse JS-templated slider shells that carry NOTHING (Smart Slider 3 / RevSlider / Elementor
    # slideshow build their slides in JS from JSON, so the archived HTML has only empty divs that
    # CSS still gives a 500px height - a hole in the page). Judge by CONTENT, not by class name:
    # any slider box with no text, no media and no inline background image can only render blank.
    # Outermost first, so removing a parent takes its equally-empty children with it.
    boxes = sorted(soup.find_all(class_=_SLIDER_BOX_RE), key=lambda t: len(list(t.parents)))
    for box in boxes:
        if box.decomposed or not box.find_parent("body"):
            continue  # already removed together with an ancestor
        # Сам медиа-элемент — это КОНТЕНТ, а не пустая оболочка. Класс слайдер-бокса нередко висит
        # прямо на картинке (Elementor: `<img class="swiper-slide-image">` у логотипов партнёров),
        # а у <img> нет вложенных <img>, поэтому проверка «есть ли внутри картинка» ложно давала
        # «пусто» и удаляла саму картинку (bambooship: логотипы партнёров исчезали из карусели).
        if box.name in ("img", "video", "picture", "svg", "canvas", "iframe", "source", "audio"):
            continue
        # NB: ignore <style>/<script> text - Smart Slider embeds its whole stylesheet INSIDE the
        # slider div, which made "does it have text?" always true and the empty shell survive.
        visible_text = "".join(
            t for t in box.find_all(string=True)
            if getattr(t.parent, "name", "") not in ("script", "style")
        ).strip()
        if visible_text or box.find(["img", "video", "iframe", "picture", "svg", "canvas"]):
            continue  # real content - keep
        if "background-image" in (box.get("style") or "").lower():
            continue  # a CSS background IS visible content
        if any("background-image" in (d.get("style") or "").lower() for d in box.find_all(True)):
            continue
        box.decompose()
        collapsed += 1

    if activated or untracked or collapsed or decloned:
        report.sliders_fixed = (f"activated first slide: {activated}, "
                                f"un-frozen tracks: {untracked}, empty shells collapsed: {collapsed}, "
                                f"duplicate cloned slides removed: {decloned}")
    return activated, untracked, collapsed, decloned


def clean_data_and_event_attrs(soup):
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.startswith("on"):
                del tag[attr]
                continue
            if attr.startswith("data-"):
                if not any(hint in attr for hint in DATA_ATTR_KEEP_HINTS):
                    del tag[attr]


_EMPTY_STYLE_DECL_RE = re.compile(r"[a-zA-Z-]+\s*:\s*(?=;|$)")
_STRAY_STYLE_SEMI_RE = re.compile(r";\s*;+")


def strip_empty_style_declarations(soup):
    """Various earlier passes (unwrapping a wayback URL out of an inline
    background-image, remove_asset_reference, CSS asset recovery, ...) can leave a
    style attribute with an empty declaration behind - 'background-color:;' if
    something followed it, or 'height:' with nothing at all if it was the last one
    (no trailing ';' to anchor on). Sweeps every style= attribute clean of these
    regardless of which step produced them, dropping the attribute entirely if
    nothing real is left."""
    for tag in soup.find_all(style=True):
        original = tag["style"]
        new_val = _EMPTY_STYLE_DECL_RE.sub("", original)
        new_val = _STRAY_STYLE_SEMI_RE.sub(";", new_val).strip().strip(";").strip()
        if new_val:
            if new_val != original:
                tag["style"] = new_val
        else:
            del tag["style"]


_EXCESS_BLANK_LINES_RE = re.compile(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)+")


def collapse_blank_lines(text):
    """Removing tags (decompose()) leaves their surrounding whitespace/newlines
    behind - re-running cleanup on an already-cleaned file compounds this into
    longer and longer runs of blank lines each pass. Collapse any run of 2+ blank
    lines down to a single one."""
    return _EXCESS_BLANK_LINES_RE.sub("\n\n", text)


# Ленивая загрузка: плагины (WP Rocket LazyLoad, a3 Lazy Load, Lazy Load by WP, jQuery Lazy…)
# кладут в src ПУСТЫШКУ (прозрачный gif или инлайновый svg-спейсер нужного размера), а настоящий
# URL прячут в data-атрибут. Реальные имена атрибутов у плагинов разные — перечисляем известные.
_LAZY_SRC_ATTRS = ("data-lazy-src", "data-src", "data-original", "data-echo",
                   "data-lazyload", "data-lazy", "data-img-url", "data-original-src")
_LAZY_SRCSET_ATTRS = ("data-lazy-srcset", "data-srcset", "data-original-srcset")
# Пустышка-плейсхолдер: инлайновый svg-спейсер или 1x1-gif. Настоящий data:-образ так не начинается
# с viewBox/пустого gif, поэтому подмену получают только заглушки.
_LAZY_PLACEHOLDER_RE = re.compile(
    r"^\s*data:image/(?:svg\+xml|gif)[;,]", re.I)


def _is_lazy_placeholder_src(src):
    """src отсутствует или это заглушка ленивой загрузки (её надо заменить настоящим URL)."""
    if not src or not src.strip():
        return True
    return bool(_LAZY_PLACEHOLDER_RE.match(src))


def promote_src(soup):
    """Поднять настоящий URL ленивой загрузки в src/srcset.

    Без этого страница показывает пустые плейсхолдеры вместо картинок: у saramonicvietnam так
    «не скачались» 55 фото галереи — файлы лежали локально (`index_files/photo_…jpg`), но `src`
    оставался svg-заглушкой, а реальный путь висел в `data-lazy-src`. Проверялся только `data-src`,
    поэтому lazy-load любого распространённого WP-плагина ронял всю галерею в пустоту.
    """
    for tag in soup.find_all(["img", "source", "video", "audio"]):
        if _is_lazy_placeholder_src(tag.get("src")):
            for _a in _LAZY_SRC_ATTRS:
                if tag.get(_a) and not _is_lazy_placeholder_src(tag.get(_a)):
                    tag["src"] = tag[_a]
                    break
        if not (tag.get("srcset") or "").strip():
            for _a in _LAZY_SRCSET_ATTRS:
                if (tag.get(_a) or "").strip():
                    tag["srcset"] = tag[_a]
                    break


def add_lazy_loading(soup, report):
    """Every <img> defers offscreen loads via the native loading="lazy" attribute,
    unless it already has some loading behavior set (lazy or explicit eager)."""
    for img in soup.find_all("img"):
        if img.get("loading"):
            continue
        img["loading"] = "lazy"
        report.lazy_loading_added += 1


def unwayback_all_attrs(soup):
    for tag in soup.find_all(True):
        for attr, val in list(tag.attrs.items()):
            if isinstance(val, list):
                tag.attrs[attr] = [unwayback(v) for v in val]
            elif isinstance(val, str):
                tag.attrs[attr] = unwayback(val)


def clean_links_a(soup, site_domain, report, keep_contact_info=True):
    """Unwrap the wayback wrapper off every <a href> and rewrite same-domain links as
    relative paths. External/social links are NOT deleted anymore - just unwaybacked
    and left in place as-is (the site owner may want them kept). mailto:/tel: links are
    also KEPT by default (they're the site's real contact info) - they're removed only
    when keep_contact_info is False (the opt-in contact-stripping pass)."""
    for a in soup.find_all("a", href=True):
        raw_href = a["href"]
        href = unwayback(raw_href)
        if href.startswith(("mailto:", "tel:")):
            if keep_contact_info:
                a["href"] = href  # keep the contact link, just normalize the wrapper off
                continue
            report.removed_contact_links.append(href)
            _remove_a_and_empty_parent(a)
            continue
        if href.startswith(("#", "javascript:")) or not is_external(href):
            a["href"] = href
            report.kept_a_internal += 1
            continue
        d = domain_of(href)
        if site_domain and matches_suffix(d, {_bare_domain(site_domain)}):
            a["href"] = to_relative(href, site_domain)
            report.kept_a_internal += 1
            continue
        # A restored page must not send anyone to third-party resources. Strip the href and keep
        # the text: the element stays (layout intact), it just stops being a live outbound link.
        # EXCEPTION: links inside the header/nav/footer keep their href for now - auto_link_menu
        # runs next and turns those into in-page section anchors (with a proper label) instead;
        # it can only find them while they still have an href.
        if a.find_parent(["header", "nav", "footer"]) is not None:
            a["href"] = href
            (report.kept_a_social if matches_suffix(d, SOCIAL_DOMAINS)
             else report.kept_a_external).append(href)
            continue
        del a["href"]
        report.external_hrefs_stripped.append(href)


def _old_domain_mention_re(old_domain):
    bare = old_domain[4:] if old_domain.lower().startswith("www.") else old_domain
    return re.compile(rf"(?:https?://)?(?:www\.)?{re.escape(bare)}(?:/[^\s<>\"']*)?", re.IGNORECASE)


def strip_contact_info(soup, report, old_domain=None):
    """Redact stray email addresses / phone numbers / physical addresses left in
    visible text or attributes, and any leftover mentions of `old_domain` - the
    domain this content was actually written for, if that turned out to differ from
    the domain we're restoring to (site_domain, named after the export folder)."""
    domain_re = _old_domain_mention_re(old_domain) if old_domain else None

    def _phone_sub(m):
        matched = m.group(0)
        digits = sum(c.isdigit() for c in matched)
        has_separator = any(c in " -()" for c in matched) or matched.strip().startswith("+")
        if digits < 6 or (digits < 9 and not has_separator):
            return matched
        report.redacted_phones.append(matched.strip())
        return ""

    def _email_sub(m):
        report.redacted_emails.append(m.group(0))
        return ""

    def _domain_sub(m):
        report.redacted_domain_mentions.append(m.group(0))
        return ""

    def _looks_like_address(text):
        low = f" {text.lower()} "
        return any(kw in low for kw in ADDRESS_KEYWORDS) and bool(ADDRESS_POSTAL_RE.search(text))

    def _redact(text):
        new = EMAIL_RE.sub(_email_sub, text)
        new = PHONE_RE.sub(_phone_sub, new)
        if domain_re:
            new = domain_re.sub(_domain_sub, new)
        if new.strip() and _looks_like_address(new):
            report.redacted_addresses.append(new.strip()[:100])
            new = ""
        return new

    for text_node in soup.find_all(string=True):
        if isinstance(text_node, Comment):
            continue
        if text_node.parent and text_node.parent.name in ("script", "style"):
            continue
        original = str(text_node)
        new = _redact(original)
        if new != original:
            text_node.replace_with(new)

    for tag in soup.find_all(True):
        for attr in CONTACT_TEXT_ATTRS:
            val = tag.get(attr)
            if not isinstance(val, str):
                continue
            new_val = _redact(val)
            if new_val != val:
                tag[attr] = new_val.strip()


def normalize_email_domains(soup, site_domain, report):
    """Make every e-mail address use THIS site's domain (the one the export folder is named after)
    so a restored page doesn't keep the previous owner's address - info@gigaworks.com on a
    gigaworks.in site becomes info@gigaworks.in. Local part is kept, only the domain is swapped to
    the bare site domain. Runs when contacts are KEPT (when they're stripped this is moot)."""
    bare = _bare_domain(site_domain)
    if not bare:
        return
    seen = set()

    def _swap(m):
        local, dom = m.group(1), m.group(2)
        if dom.lower() == bare.lower():
            return m.group(0)
        newv = f"{local}@{bare}"
        key = (m.group(0), newv)
        if key not in seen:
            seen.add(key)
            report.emails_domain_normalized.append(f"{m.group(0)} -> {newv}")
        return newv

    # mailto: hrefs (an attribute, not a text node)
    for a in soup.find_all("a", href=True):
        if a["href"].lower().startswith("mailto:"):
            a["href"] = _EMAIL_LOCAL_DOMAIN_RE.sub(_swap, a["href"])
    # visible text (covers <p>info@old.com</p> and the mailto anchor's own text)
    for node in list(soup.find_all(string=_EMAIL_LOCAL_DOMAIN_RE)):
        if isinstance(node, Comment):
            continue
        if node.parent and node.parent.name in ("script", "style"):
            continue
        newv = _EMAIL_LOCAL_DOMAIN_RE.sub(_swap, str(node))
        if newv != str(node):
            node.replace_with(newv)


def _remove_a_and_empty_parent(a_tag):
    parent = a_tag.parent
    a_tag.decompose()
    if parent and parent.name in ("li", "span") and not parent.get_text(strip=True) and not parent.find(True):
        parent.decompose()


def strip_base_href(soup, report=None):
    """Remove a leftover <base href="https://origin/"> - THE classic "restored page has no styles"
    trap. A base href makes the browser resolve EVERY relative URL (stylesheets, scripts, images)
    against that absolute origin, so once the page is a self-contained local export, all its
    index_files/*.css etc. try to load from the (usually dead) original domain and nothing applies
    - the page renders as bare unstyled HTML. A restored static site must resolve relatives against
    its own folder, so any <base> with an absolute/protocol-relative href is dropped. A bare
    <base target="_blank"> (no href) is harmless and kept."""
    for base in soup.find_all("base"):
        href = (base.get("href") or "").strip()
        if href.lower().startswith(("http://", "https://", "//")):
            if report is not None:
                report.base_tag_removed = href
            if base.get("target"):
                del base["href"]  # keep a meaningful target=, just drop the poisoning href
            else:
                base.decompose()


def _audit_stylesheets(soup, html_path, site_domain, report):
    """QA self-check on the stylesheets the browser will actually try to load. Two universal
    breakages this catches for ANY site:
      - a <link rel=stylesheet> whose local file is really an HTML page (the archive served an
        error/interstitial and it got saved under a .css name, e.g. customize.css) - unparseable as
        CSS, so those styles silently vanish. Try to recover the real CSS from the archive (domain
        guess, like the corrupted-image sweep); failing that, drop the dead <link> and quarantine
        the file so it's visibly gone instead of silently broken.
      - a <link rel=stylesheet> pointing at a local file that doesn't exist -> flag it.
    Returns True if it changed the soup (a <link> was removed)."""
    site_root = html_path.parent
    bare = _bare_domain(site_domain) if site_domain else None
    changed = False
    for link in list(soup.find_all("link")):
        rels = [r.lower() for r in (link.get("rel") or [])]
        if "stylesheet" not in rels:
            continue
        href = (link.get("href") or "").strip()
        if not href or href.lower().startswith(("http://", "https://", "//", "data:")):
            continue  # remote/data URL - not a local file we own
        local = site_root / href.split("?")[0].split("#")[0]
        if not local.is_file():
            report.qa_warnings.append(f"стиль отсутствует локально: {href}")
            continue
        try:
            head = local.read_bytes().lstrip()[:64].lower()
        except OSError:
            continue
        if not head.startswith((b"<!doctype", b"<html", b"<head", b"<script", b"<body")):
            continue  # real CSS (never starts with an HTML tag) - fine
        # This "stylesheet" is actually HTML. Recover real CSS, or drop the dead link.
        recovered = False
        if bare:
            try:
                data, _ext = recover_asset_bytes(f"http://{bare}/{local.name}")
            except Exception:  # noqa: BLE001
                data = None
            if data and not data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
                local.write_bytes(data)
                recovered = True
        if recovered:
            report.qa_fixed.append(f"стиль был HTML → восстановлен из архива: {href}")
        else:
            trash = local.parent / "_wayback_removed"
            trash.mkdir(exist_ok=True)
            dest, i = trash / local.name, 1
            while dest.exists():
                dest = trash / f"{local.stem}_{i}{local.suffix}"
                i += 1
            shutil.move(str(local), str(dest))
            link.decompose()
            changed = True
            report.qa_fixed.append(f"стиль был HTML (не CSS) → снят и в карантин: {href}")
    return changed


def _audit_final_output(html_path, report):
    """Universal 'did it come out broken?' check, run LAST on the FINAL files. Catches the signals
    that mean a page renders wrong for ANY archive - a class we've closed or one we haven't yet:
      - leftover wayback refs ('/web/<ts>/...') anywhere in the HTML or local CSS/JS = an asset ref
        we failed to localize -> that asset won't load;
      - <link rel=stylesheet> whose local file is missing -> those styles won't apply.
    Cheap (no render). Turns a SILENT breakage into a visible warning so a new bad archive announces
    itself in the report instead of surfacing later as 'опять пришло кривым'."""
    site_root = html_path.parent
    try:
        html = read_text_safe(html_path)
    except Exception:  # noqa: BLE001
        return
    wb = len(re.findall(r"/web/\d{8,}[a-z_]*/", html))
    for p in site_root.rglob("*"):
        if (p.is_file() and p.suffix.lower() in (".css", ".js")
                and not any(part in QUARANTINE_DIR_NAMES for part in p.relative_to(site_root).parts)):
            try:
                wb += len(re.findall(r"/web/\d{8,}[a-z_]*/", read_text_safe(p)))
            except Exception:  # noqa: BLE001
                pass
    if wb:
        report.qa_warnings.append(
            f"осталось {wb} wayback-ссылок в HTML/CSS/JS — часть ассетов может не грузиться (проверь рендер)")
    soup = BeautifulSoup(html, PARSER)
    missing = broken = 0
    for l in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        h = (l.get("href") or "").split("?")[0].split("#")[0]
        if not h or h.startswith(("http", "//", "data:")):
            continue
        f = site_root / h
        if not f.is_file():
            missing += 1
            continue
        # The file exists but is EMPTY/truncated or is really an HTML error page -> it applies no
        # CSS, so a single critical stylesheet in this state leaves the whole page unstyled. Flag it.
        try:
            data = f.read_bytes()
        except OSError:
            continue
        if len(data.strip()) < 8 or data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
            broken += 1
    if missing:
        report.qa_warnings.append(
            f"{missing} <link rel=stylesheet> ведут на несуществующий локальный файл — стили не применятся")
    if broken:
        report.qa_warnings.append(
            f"{broken} подключённых CSS пустые/битые (не настоящий CSS) — стили с них не применятся (проверь рендер)")
    if soup.find(["frameset", "frame"]) is not None:
        report.qa_warnings.append(
            "страница на <frameset> — контент в отдельных фреймах, нужен ручной разбор (старый сайт)")
    body = soup.find("body")
    if body is not None and len(body.get_text(strip=True)) < 40 and not body.find(["img", "iframe", "svg"]):
        report.qa_warnings.append(
            "в <body> почти нет контента — возможно, страница скачалась пустой/сломанной")


def strip_blocking_meta(soup, report=None):
    """Remove a <meta http-equiv="Content-Security-Policy"> (and its Report-Only variant). An
    archived page's CSP almost always whitelists ONLY the original domain, so once the page is a
    self-contained local export the CSP BLOCKS every local stylesheet/script/image/font - the page
    goes completely blank/unstyled, and INVISIBLY (no toolbar, no error the user sees). A restored
    static site needs no CSP at all, so dropping it is always safe (it only relaxes). Anticipatory:
    not in the test batch, but common on modern sites and a guaranteed whole-page breaker."""
    n = 0
    for meta in soup.find_all("meta", attrs={"http-equiv": re.compile(
            r"^\s*content-security-policy(-report-only)?\s*$", re.I)}):
        meta.decompose()
        n += 1
    if report is not None and n:
        report.qa_fixed.append(f"снят <meta CSP> ({n}) — иначе блокировал бы все локальные стили/скрипты")
    return n


def strip_sri_attrs(soup, report=None):
    """Remove integrity= (and crossorigin=) from <link>/<script>. Subresource Integrity pins a hash
    of the ORIGINAL remote asset; once we serve a LOCAL copy (recovered/rewritten) the hash no
    longer matches and the browser BLOCKS the stylesheet/script outright - an INVISIBLE "no styles /
    dead JS" on any modern archived site that shipped SRI (common on CDN <link>/<script>). Not in the
    test batch, but a guaranteed future breaker, so close the class now. crossorigin on a now-local
    ref is moot too."""
    n = 0
    for tag in soup.find_all(["link", "script", "style"]):
        for attr in ("integrity", "crossorigin"):
            if tag.has_attr(attr):
                del tag[attr]
                n += 1
    if report is not None and n:
        report.sri_stripped = n
    return n


def strip_wayback_toolbar(soup):
    for tag_id in ("wm-ipp-base", "wm-ipp-print"):
        el = soup.find(id=tag_id)
        if el:
            el.decompose()
    stamp = soup.find(id="yui3-css-stamp")
    if stamp:
        stamp.decompose()


HTML_TAG_KEEP_ATTRS = ("lang", "class")


def strip_wayback_comment_and_html_attrs(soup):
    """Remove all HTML comments, and strip the <html> tag down to just lang/class -
    everything else (id, style, itemscope/itemtype, xmlns:*, runtime-injected junk)
    is either wayback/JS-runtime cruft or leftover markup nobody needs."""
    for c in soup.find_all(string=lambda s: isinstance(s, Comment)):
        c.extract()
    html_tag = soup.find("html")
    if html_tag:
        for attr in list(html_tag.attrs.keys()):
            if attr not in HTML_TAG_KEEP_ATTRS:
                del html_tag[attr]


def move_orphaned_wayback_assets(html_path, cleaned_html_text, report):
    assets_dir = html_path.with_name(html_path.stem + "_files")
    if not assets_dir.is_dir():
        return
    trash_dir = assets_dir / "_wayback_removed"
    known = set(WAYBACK_JS_NAME_HINTS) | WAYBACK_TOOLBAR_CSS_NAMES
    for f in assets_dir.iterdir():
        if f.is_file() and f.name in known and f.name not in cleaned_html_text:
            trash_dir.mkdir(exist_ok=True)
            shutil.move(str(f), str(trash_dir / f.name))
            report.moved_assets.append(f.name)


def _asset_name_referenced(name, haystack):
    """A plain 'name in haystack' substring check false-positives hard on short/
    generic filenames - a file literally called "css" (a saved Google Fonts CSS
    response commonly ends up named just that, no extension) matches inside
    "landing-page.css", "text/css", any font-family mentioning nothing at all, ...
    i.e. it always "looks referenced" and never gets swept. Require it to appear as
    its own path/filename token instead - not immediately preceded or followed by a
    word/dot/hyphen character, which is what actually separates a real reference
    ("/index_files/css\"") from a fragment inside an unrelated longer name."""
    pattern = re.compile(r"(?<![\w.-])" + re.escape(name) + r"(?![\w.-])")
    return bool(pattern.search(haystack))


_TYPE_ATTR_RE = re.compile(r"""type\s*=\s*(["'])[^"']*\1""", re.IGNORECASE)


def remove_unused_local_assets(html_path, cleaned_html_text, report, dry_run=False, extra_texts=None):
    """Any file left in the assets folder that the final cleaned HTML - or any of its
    linked local stylesheets (extra_texts), e.g. a CSS background-image - no longer
    references anywhere (old responsive-size duplicates, orphaned CMS bundles not on
    the wayback-specific list, leftovers from earlier experiments, ...) is unused
    clutter - quarantine it into _unused_removed rather than deleting outright."""
    assets_dir = html_path.with_name(html_path.stem + "_files")
    if not assets_dir.is_dir():
        return
    # A mime type attribute value (type="text/css", type="text/javascript", ...) is
    # not a path reference, but for a short/generic asset name (a saved Google Fonts
    # response gets saved as literally "css", no extension) it reads as one to
    # _asset_name_referenced's boundary check - "text/css" has "css" right after a
    # non-word "/". Blank those out before matching so this whole class of
    # short-name false positives (css/js/json/xml/svg all appear in MIME types too)
    # can't keep an orphaned file "referenced" forever.
    haystack = _TYPE_ATTR_RE.sub("", cleaned_html_text + "\n" + "\n".join(extra_texts or []))
    trash_dir = assets_dir / "_unused_removed"
    quarantine_dirs = (trash_dir, assets_dir / "_wayback_removed")
    for f in sorted(assets_dir.rglob("*")):
        if not f.is_file() or any(q in f.parents for q in quarantine_dirs):
            continue
        if _asset_name_referenced(f.name, haystack):
            continue
        rel = f.relative_to(assets_dir)
        report.removed_unused_assets.append(str(rel))
        if not dry_run:
            dest = trash_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))


UNWAYBACK_SWEEP_EXTS = {".css", ".js", ".json", ".svg", ".xml", ".txt", ".webmanifest"}
# Extension-only filtering misses e.g. a saved Google Fonts CSS response, which a
# browser/crawler often names just "css" with no extension at all (the source URL is
# "fonts.googleapis.com/css?family=..." - a query string, not a ".css" path). Skip
# only the extensions that are DEFINITELY binary; sniff everything else by content.
UNWAYBACK_SWEEP_SKIP_EXTS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tiff", ".tif",
    ".woff", ".woff2", ".ttf", ".otf", ".eot",
    ".mp4", ".webm", ".mp3", ".wav", ".ogg", ".pdf", ".zip", ".gz", ".7z", ".rar",
    ".swf", ".exe", ".dll",
}
UNWAYBACK_SWEEP_MAX_BYTES = 4_000_000
CSS_CONTENT_SNIFF_RE = re.compile(r"^\s*@(?:font-face|import|charset|media)\b", re.IGNORECASE)


def _looks_like_text_bytes(data):
    if not data:
        return True
    if b"\x00" in data[:2048]:
        return False
    sample = data[:2048]
    printable = sum(1 for b in sample if b in (9, 10, 13) or 32 <= b <= 126 or b >= 128)
    return printable / len(sample) > 0.85


def _looks_like_css(text, suffix):
    return suffix == ".css" or bool(CSS_CONTENT_SNIFF_RE.match(text)) or ("{" in text and "url(" in text and "@font-face" in text)
QUARANTINE_DIR_NAMES = {"_wayback_removed", "_unused_removed"}
CSS_URL_RE = re.compile(r"url\(\s*(['\"]?)([^'\")]+)\1\s*\)", re.IGNORECASE)
CSS_BG_RASTER_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
CSS_FONT_EXTS = {".woff", ".woff2", ".ttf", ".otf", ".eot"}
CSS_SVG_EXTS = {".svg"}
CSS_RECOVERABLE_EXTS = CSS_BG_RASTER_EXTS | CSS_FONT_EXTS | CSS_SVG_EXTS
FONT_MAGIC_PREFIXES = (b"wOFF", b"wOF2", b"OTTO", b"true", b"\x00\x01\x00\x00")
# Matches BOTH forms a wayback rewrite leaves behind: the absolute
# "https://web.archive.org/web/<ts><flags>/<original-url>" and the root-relative
# "/web/<ts><flags>/<original-url>" that CSS url() rewriting sometimes produces
# (no domain - assumes the CSS is served from archive.org's own origin, which breaks
# the moment it's exported to a static local file).
WAYBACK_ASSET_URL_RE = re.compile(
    r"^(?:(?:https?:)?//web\.archive\.org)?/web/(\d{1,14})[a-zA-Z_]*/(https?://.+)$"
)


RECOVERY_FETCH_RETRIES = 1  # one gentle retry covers the common transient web.archive.org
# hiccup (a genuinely-present asset lost to a single slow response) without turning a page
# full of genuinely-gone assets into minutes of backoff sleeps. Kept SHORT on purpose - the
# earlier 2-retry / 1.5s+3s-backoff version made cleanup ~10x slower on any Bootstrap site
# (many failing CDX lookups x big sleeps). Not recovering framework icon fonts at all (see
# _recover_css_asset) removes most of those failing lookups in the first place.


# web.archive.org rate-limits aggressively per-IP: two cleanups recovering assets at the same
# time stampede it, every request then hangs to its 10s timeout instead of ~0.5s, and a page
# that cleans in 30s alone sits for minutes in parallel. Serialize ONLY archive.org fetches
# across every thread so we stay a single, polite stream and never trip the throttle. Other
# hosts (Google Fonts on gstatic, Font Awesome on cdnjs) aren't throttle-sensitive and must NOT
# share this lock, or parallel cleanups needlessly serialize their font/icon downloads too.
# Bounded GLOBAL concurrency for archive.org. Was 1 (fully serial) to avoid the throttle-stampede
# that once hung 2 parallel cleanups; but the real culprit then was the Google-Fonts recovery spam
# (fixed via _FONT_CDN_HOSTS). A small pool is polite AND lets a single big page's CSS recovery run
# several lookups at once (the phase-88 bottleneck: dozens of serialized CDX round-trips). The cap is
# GLOBAL, so even N parallel cleanups never exceed _ARCHIVE_POOL concurrent archive requests total.
_ARCHIVE_POOL = 4
_ARCHIVE_FETCH_LOCK = threading.Semaphore(_ARCHIVE_POOL)

# Remembers URLs that already failed this process, so a serialized archive fetch is never spent
# twice on the same dead asset (see _fetch_url_bytes). Keyed by URL -> the original exception.
_FETCH_FAILED = {}
_FETCH_FAIL_LOCK = threading.Lock()


class CleanupCancelled(BaseException):
    """Raised when the running cleanup's cancel checkpoint fires, so it can be aborted from the
    UI - at phase boundaries, inside the per-asset recovery loops, AND inside any network fetch.
    Subclasses BaseException (not Exception) on purpose: the recovery loops wrap fetches in
    `except Exception: continue`, which would otherwise swallow the cancellation and keep going."""


# Per-thread cancel hook: clean_html_file installs its cancelled() callback here for the duration
# of the run, so the shared low-level network fetch can become a cancellation point without every
# intermediate function having to thread a `cancelled` argument through. Thread-local => each
# parallel cleanup sees only its own.
_CANCEL_TL = threading.local()


def _set_cancel_check(fn):
    _CANCEL_TL.check = fn


def _cancel_active():
    fn = getattr(_CANCEL_TL, "check", None)
    return fn is not None and fn()


def _raise_if_cancelled(cancelled=None):
    """Raise CleanupCancelled if either the explicit callback OR the thread-local hook says so."""
    if cancelled is not None and cancelled():
        raise CleanupCancelled()
    if _cancel_active():
        raise CleanupCancelled()


# Per-run archive-recovery circuit breaker + negative cache. A page whose assets simply aren't in
# the archive (a common case) otherwise pays a full serialized CDX round-trip for EVERY missing
# asset - 100+ of them = minutes. After too many CONSECUTIVE misses we conclude the archive doesn't
# have this site's assets and stop the network for the rest of the run (recovery is best-effort;
# the same "couldn't recover" fallbacks still fire). A single success resets the streak, so sites
# that DO recover are never cut off. Thread-local => each parallel cleanup has its own breaker.
# Enabled only inside clean_html_file; standalone recover_asset_bytes callers keep old behaviour.
_REC_TL = threading.local()
_REC_MAX_CONSEC_FAILS = 10


def _reset_recovery_state():
    _REC_TL.enabled = True
    _REC_TL.consec_fails = 0
    _REC_TL.dead = False
    _REC_TL.miss = set()  # original URLs already known-dead this run (skip re-lookup)


def _recovery_giving_up():
    return getattr(_REC_TL, "enabled", False) and getattr(_REC_TL, "dead", False)


def _recovery_note(success):
    if not getattr(_REC_TL, "enabled", False):
        return
    if success:
        _REC_TL.consec_fails = 0
    else:
        _REC_TL.consec_fails = getattr(_REC_TL, "consec_fails", 0) + 1
        if _REC_TL.consec_fails >= _REC_MAX_CONSEC_FAILS:
            _REC_TL.dead = True


# A throttled web.archive.org hands back timeouts, dropped connections and 429/5xx for assets
# that ARE archived and DO come back a second later - so those get backed-off retries and are
# never memoized as dead. A 404/410 (or anything else) is a definitive verdict: fail once,
# remember it, never ask again. This split is what lets a page with a dead plugin's unarchived
# sprites STAMPEDING right before a perfectly-archived theme icon still recover that icon,
# without reintroducing the "minutes of backoff on a page full of genuinely-gone assets"
# regression - definitive misses cost exactly one request each, as before.
_ARCHIVE_TRANSIENT_RETRIES = 3
_TRANSIENT_HTTP_CODES = frozenset({408, 425, 429, 500, 502, 503, 504})


def _is_transient_fetch_error(e):
    import http.client
    import socket
    import urllib.error

    if isinstance(e, urllib.error.HTTPError):
        return e.code in _TRANSIENT_HTTP_CODES
    if isinstance(e, (socket.timeout, TimeoutError, ConnectionError,
                      http.client.IncompleteRead, http.client.RemoteDisconnected)):
        return True
    if isinstance(e, urllib.error.URLError):  # wraps the socket-level failures above
        return isinstance(getattr(e, "reason", None),
                          (socket.timeout, TimeoutError, ConnectionError, OSError))
    return False


def _fetch_url_bytes(url, timeout=RECOVERY_FETCH_TIMEOUT, retries=RECOVERY_FETCH_RETRIES):
    import time
    import urllib.request

    # Archive fetches are SERIALIZED (see _ARCHIVE_FETCH_LOCK), so every wasted request costs the
    # whole run ~1.5s on a good day and the full timeout when throttled. The same dead URL gets
    # asked for again and again (the same missing sprite referenced from several stylesheets, the
    # same font from every page), so remember what already failed and fail those instantly. This
    # is the single biggest cleanup speed-up on asset-heavy sites - no behaviour is lost, we just
    # stop re-asking for things we already know aren't there. Only DEFINITIVE failures are
    # memoized (see below) - a transient throttle timeout is not a verdict about the URL.
    with _FETCH_FAIL_LOCK:
        if url in _FETCH_FAILED:
            raise _FETCH_FAILED[url]

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (image-recovery-bot)"})
    use_lock = "archive.org" in url  # only the throttle-sensitive archive host is serialized
    last_err = None
    attempt = 0
    while True:
        _raise_if_cancelled()  # every network attempt is a cancellation point
        try:
            if use_lock:
                with _ARCHIVE_FETCH_LOCK:
                    with urllib.request.urlopen(req, timeout=timeout) as resp:
                        return resp.read()
            else:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return resp.read()
        except CleanupCancelled:
            raise
        except Exception as e:  # noqa: BLE001 - classify: throttle spike (retry) vs gone (bail)
            last_err = e
            # Definitive miss (404/410/...): bail immediately, no wasted backoff sleep. Transient
            # throttle on archive.org: back off and retry - the asset is really there. The lock is
            # already released here (we're outside the `with`), so the sleep frees the slot.
            if _is_transient_fetch_error(e):
                max_attempts = _ARCHIVE_TRANSIENT_RETRIES if use_lock else retries
                if attempt < max_attempts:
                    time.sleep(min(4.0, 0.5 * (2 ** attempt)))
                    attempt += 1
                    continue
            break

    # Only remember DEFINITIVE failures. Memoizing a transient throttle timeout would turn a
    # temporary spike into a permanent "dead" verdict and drop an asset that's really archived.
    if not _is_transient_fetch_error(last_err):
        with _FETCH_FAIL_LOCK:
            _FETCH_FAILED[url] = last_err
    raise last_err


def _looks_like_valid_asset_bytes(data, ext=""):
    """_looks_like_image_bytes only recognizes image magic bytes - fonts need their
    own check, and anything else just needs to not obviously be an HTML error/
    redirect page (wayback and dead domains alike tend to hand those back with a 200)."""
    if not data or len(data) < 32:
        return False
    ext = (ext or "").lower()
    if ext in CSS_BG_RASTER_EXTS:
        return _looks_like_image_bytes(data)
    if ext in CSS_FONT_EXTS:
        return data[:4] in FONT_MAGIC_PREFIXES or data[:2] == b"\x00\x01"
    if ext in CSS_SVG_EXTS:
        head = data.lstrip()[:100].lower()
        return head.startswith((b"<?xml", b"<svg"))
    return not data.lstrip()[:15].lower().startswith((b"<!doctype", b"<html"))


# A CDX *search* is a far heavier query than a raw id_ asset fetch and legitimately takes
# 15-20s when archive.org is under load - measured 18s for a URL that has only 2 captures. The
# 8s asset-fetch timeout kills it mid-answer, so an asset that IS archived gets a false "gone"
# verdict. Give the CDX lookup its own generous ceiling; it only bites when archive.org is slow
# (a clean "no captures" still returns fast and costs one request), which is exactly when we
# must wait for the authoritative answer rather than guess the asset away.
RECOVERY_CDX_TIMEOUT = 25


def _wayback_nearest_snapshot_url(original_url, timestamp):
    """The exact timestamp embedded in a rewritten reference sometimes 404s (the page
    around it was captured, this one asset wasn't, at that exact crawl) - ask the CDX
    API for every capture of that exact original URL and pick the best one: nearest to
    `timestamp` if one was given (a wayback-wrapped reference has one), else (a bare
    reference back to the site's own domain has no timestamp to go by) the MOST
    RECENT successful capture. Returns a raw-bytes ('id_') fetch URL, or None."""
    import json
    import urllib.parse

    api = "https://web.archive.org/cdx/search/cdx?" + urllib.parse.urlencode(
        {"url": original_url, "output": "json", "filter": "statuscode:200"}
    )
    try:
        rows = json.loads(_fetch_url_bytes(api, timeout=RECOVERY_CDX_TIMEOUT).decode("utf-8", "replace"))
    except Exception:
        return None
    if len(rows) < 2:
        return None
    header, entries = rows[0], rows[1:]
    ts_i = header.index("timestamp")
    if timestamp:
        best = min(entries, key=lambda r: abs(int(r[ts_i]) - int(timestamp)))
    else:
        best = max(entries, key=lambda r: int(r[ts_i]))
    return f"https://web.archive.org/web/{best[ts_i]}id_/{original_url}"


def _cap_asset_name(name):
    """Cap a recovered asset's filename so <deep site folder>/index_files/.../<name> never blows
    Windows' 260-char MAX_PATH - blogspot/Google asset names are 100+ char hashes, and the write
    then CRASHES the whole cleanup (puratoni). Short readable stem + an md5 tag keeps distinct long
    names distinct; the extension is preserved."""
    name = name or "asset"
    if len(name) <= 64:
        return name
    import hashlib
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    return f"{stem[:40]}_{hashlib.md5(name.encode('utf-8')).hexdigest()[:8]}" + (f".{ext[:8]}" if ext else "")


def recover_asset_bytes(original_url, timestamp=None):
    """Try to fetch the real bytes of `original_url` from the Wayback Machine - the
    exact-timestamp raw fetch first if a timestamp hint is available, then the CDX
    best-match snapshot of that same URL either way. Returns (bytes, ext) on success,
    or (None, ext) if nothing could be recovered. Shared by the CSS asset recovery
    below and the broken-resource auto-cleanup in site_edit.py."""
    ext = Path(safe_urlsplit(original_url).path).suffix.lower()
    enabled = getattr(_REC_TL, "enabled", False)
    if enabled:
        # This site's assets are clearly not archived, or we already tried this exact URL and it
        # was dead - skip the (serialized, slow) CDX round-trip and go straight to the fallback.
        if _recovery_giving_up() or original_url in getattr(_REC_TL, "miss", ()):
            return None, ext
    candidates = []
    if timestamp:
        candidates.append(f"https://web.archive.org/web/{timestamp}id_/{original_url}")
    nearest = _wayback_nearest_snapshot_url(original_url, timestamp)
    if nearest and nearest not in candidates:
        candidates.append(nearest)
    for candidate in candidates:
        try:
            data = _fetch_url_bytes(candidate)
        except Exception:
            continue
        if _looks_like_valid_asset_bytes(data, ext):
            _recovery_note(True)
            return data, ext
    _recovery_note(False)
    if enabled:
        _REC_TL.miss.add(original_url)
    return None, ext


def _css_asset_recovery_target(raw_url, site_domain):
    """What to try recovering `raw_url` from, if anything: a wayback-wrapped
    reference (any timestamp) - or a bare absolute reference straight back to the
    site's OWN (now presumably abandoned) domain, no wrapper at all - a common shape
    for vendor CSS bundles (Bootstrap's glyphicon @font-face etc.) whose relative
    url() got resolved to an absolute one against the live site during capture,
    without ever picking up the full /web/<ts>/ wrapper the rest of the page got.
    Returns (timestamp_or_None, original_url), or None if raw_url is neither."""
    m = WAYBACK_ASSET_URL_RE.match(raw_url)
    if m:
        return m.group(1), m.group(2)
    if site_domain and is_external(raw_url) and matches_suffix(domain_of(raw_url), {_bare_domain(site_domain)}):
        return None, raw_url
    return None


def _recover_css_asset(raw_url, css_path, report, site_domain):
    """raw_url is a candidate asset reference found inside a local CSS file's
    url(...) - PBN checklist item 9/10 territory, for CSS background-image AND
    @font-face src (woff/woff2/ttf/otf/eot, plus the legacy .svg font format some
    @font-face blocks still ship - Bootstrap's glyphicons among them) alike. A
    '#fragment' on an .svg reference (selects a specific <glyph>/<font> element
    inside the file) is stripped before fetching but re-attached to the final local
    reference, since it's meaningless to the HTTP request but load-bearing for the
    browser. On success, saves the recovered file next to the CSS file and returns
    the new local relative path. On total failure: if this was merely a BARE
    same-domain reference (no wayback wrapper at all - just an absolute URL back to
    the site's own now-abandoned domain), it's downgraded to root-relative instead of
    left as a hardcoded absolute URL (nothing "external" should survive, recoverable
    or not); a wayback-wrapped reference falls back to the bare filename - a clearly-
    broken local reference, but one site_studio's image-slot picker can still find
    and fill in later (it needs SOME non-empty url() text to detect the slot at all -
    a truly empty url() is invisible to it)."""
    target = _css_asset_recovery_target(raw_url, site_domain)
    if target is None:
        # Bare relative reference the wayback/same-domain rules don't cover. If it points at
        # a recoverable asset type that's genuinely MISSING locally - e.g. a background a
        # previous run downgraded to a bare filename after a transient recovery failure, so
        # the archive URL provenance is gone - and we know the site domain, guess
        # "<domain>/<filename>" (the flat-URL layout these exports consistently use) and
        # recover that. This is what lets a re-run pick up what a slow/throttled first pass
        # dropped, instead of the reference staying permanently broken.
        if site_domain and not is_external(raw_url) and not raw_url.startswith(("data:", "#")):
            clean = raw_url.split("?")[0].split("#")[0].lstrip("/")
            name0 = Path(clean).name
            local = (css_path.parent / clean).resolve()
            if name0 and Path(name0).suffix.lower() in CSS_RECOVERABLE_EXTS and not local.is_file():
                target = (None, f"http://{_bare_domain(site_domain)}/{name0}")
        if target is None:
            return None  # not a recoverable reference - let the plain unwayback pass handle it
    timestamp, original_url = target
    fragment = ""
    if "#" in original_url:
        original_url, fragment = original_url.split("#", 1)
        fragment = "#" + fragment
    name = Path(safe_urlsplit(original_url).path).name or "asset"
    # Framework icon fonts (Bootstrap glyphicons, Font Awesome, ...) never actually lived at
    # "<site domain>/<file>" - they shipped from the framework/CDN - so hitting the archive
    # for them is pure wasted time: each is a slow CDX round-trip that always fails AND the
    # burst of them triggers web.archive.org rate-limiting that then slows the assets that
    # CAN be recovered. This was the bulk of the cleanup slowdown on any Bootstrap/FA site.
    # Skip the network entirely and neutralize the reference the same way a failed recovery
    # would; self-hosting the real icon font is a separate, deliberate step (not archive).
    if any(hint in name.lower() for hint in ICON_FONT_HINTS) or _ICON_WEBFONT_RE.search(name):
        return to_relative(raw_url, site_domain) if timestamp is None else name
    # Asset served from a third-party font/framework CDN - never the site's own file; the cleaner
    # re-injects fonts/icons fresh. Skip the network entirely (see _FONT_CDN_HOSTS).
    host = domain_of(original_url)
    if host and matches_suffix(host, _FONT_CDN_HOSTS):
        return to_relative(raw_url, site_domain) if timestamp is None else name
    # WEBFONTS are never worth an archive round-trip. Whatever font the page's CSS references
    # (a Google-Fonts mirror whose gstatic host got rewritten to the site's own domain, so the
    # CDN check above misses it - this was 58 of 62 fetches and ~750s on a Blogger site; or the
    # theme's own self-hosted face), the cleaner re-injects its own Google Font with a global
    # !important override, so the original never renders anyway. Any .woff2/.woff/.ttf/.otf/.eot
    # that isn't already sitting on disk locally is pure wasted time (a slow CDX lookup that then
    # usually 404s AND whose burst triggers the rate-limiting that slows the assets that DO matter).
    if Path(name).suffix.lower() in CSS_FONT_EXTS:
        return to_relative(raw_url, site_domain) if timestamp is None else name
    if Path(name).suffix.lower() not in CSS_RECOVERABLE_EXTS:
        if timestamp is None:
            # Bare same-domain absolute URL, not an asset type we know how to fetch -
            # still shouldn't stay hardcoded-absolute. Downgrade to root-relative.
            return to_relative(raw_url, site_domain)
        return None  # a wayback-wrapped non-asset url - leave to the plain unwayback pass

    # СНАЧАЛА локальный файл, потом сеть. Тот же ассет часто УЖЕ скачан рядом с CSS под своим или
    # дедуп-именем (`accordion_up.png` и `accordion_up_1.png` от двух копий одного стиля). Ходить в
    # архив за тем, что уже на диске, — это и лишняя копия, и риск: под троттлом сеть падает, ассет
    # уходит в «мёртвые», а рабочий файл всё это время лежал в той же папке. Ищем в каталоге CSS
    # точное имя и его дедуп-вариант `<stem>_<цифры><ext>`; берём файл, а не wayback-HTML под ним.
    _local = css_path.parent / name
    if not _local.is_file():
        _cands = sorted(css_path.parent.glob(f"{Path(name).stem}_[0-9]*{Path(name).suffix}"))
        _local = next((c for c in _cands if c.is_file()), _local)
    if _local.is_file() and not _looks_like_corrupted_wayback_asset(_local.read_bytes()):
        return _local.relative_to(css_path.parent).as_posix() + fragment

    print(f"[css-recovery] fetching {original_url} (referenced in {css_path.name})...")
    data, _ext = recover_asset_bytes(original_url, timestamp)
    if data is not None:
        print(f"[css-recovery] ok: {original_url}")
        assets_dir = css_path.parent / f"{css_path.stem}_recovered"
        assets_dir.mkdir(parents=True, exist_ok=True)
        name = _cap_asset_name(name)  # long blogspot/Google hashes else blow Windows MAX_PATH -> crash
        dest = assets_dir / name
        i = 1
        while dest.exists() and dest.read_bytes() != data:
            dest = assets_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        try:
            if not dest.exists():
                dest.write_bytes(data)
        except OSError:
            return to_relative(raw_url, site_domain) if timestamp is None else Path(name).name
        report.recovered_images.append(f"{original_url} -> {dest.relative_to(css_path.parent).as_posix()} (CSS {Path(name).suffix.lstrip('.')})")
        return dest.relative_to(css_path.parent).as_posix() + fragment

    print(f"[css-recovery] FAILED: {original_url}")
    report.failed_image_recovery.append(f"{original_url}: could not recover from web.archive.org (CSS asset in {css_path.name})")
    if timestamp is None:
        return to_relative(raw_url, site_domain)
    return name


def clean_local_linked_files(html_path, report=None, site_domain=None, cancelled=None):
    """Strip every remaining web.archive.org time-travel wrapper out of EVERY local
    text-based asset file next to the site - not just the HTML page and its <link
    rel=stylesheet> CSS (that used to be all clean_local_css_files touched). A wayback
    wrapper can end up embedded in a JS string, an SVG xlink:href, a JSON config
    value, a sitemap.xml URL, etc. - files that are never parsed as HTML, so the DOM-
    level unwayback passes never reach them. NOT gated on a fixed extension list - a
    saved Google Fonts CSS response is commonly named just "css" with no extension at
    all (the source URL is a query string, "fonts.googleapis.com/css?family=...", not
    a ".css" path), so it would otherwise slip through untouched. Known-binary
    extensions are skipped outright; everything else under 4MB is sniffed by content
    (byte-level printable-ratio check) rather than trusted/distrusted by name. Also
    strips any leftover @import of an old font service, and (if `report` is given)
    tries to actually recover any CSS background-image/@font-face asset that's still
    a wayback reference OR a bare reference back to `site_domain` - downloading the
    real bytes and relinking to a local file, rather than just unwrapping down to a
    dead/live-original-domain URL. Skips the quarantine folders (_wayback_removed/
    _unused_removed) - dead files already, no point cleaning them. Returns the list
    of files actually changed."""
    site_root = html_path.parent
    changed = []
    for path in site_root.rglob("*"):
        _raise_if_cancelled(cancelled)  # bail promptly if the user hit "Отменить"
        if not path.is_file():
            continue
        if any(part in QUARANTINE_DIR_NAMES for part in path.parts):
            continue
        suffix = path.suffix.lower()
        if suffix in UNWAYBACK_SWEEP_SKIP_EXTS:
            continue
        try:
            if path.stat().st_size > UNWAYBACK_SWEEP_MAX_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            continue
        if suffix not in UNWAYBACK_SWEEP_EXTS and not _looks_like_text_bytes(raw):
            continue

        text = read_text_safe(path)
        working = text
        is_css = _looks_like_css(text, suffix)
        if is_css and report is not None:
            # A big framework/theme CSS has dozens of url()s, each a slow serialized archive lookup -
            # the phase-88 bottleneck. PRE-recover the unique targets CONCURRENTLY (bounded globally by
            # _ARCHIVE_FETCH_LOCK), then the regex pass just reads the results. No network in the sub.
            _seen, _targets = set(), []
            for _m in CSS_URL_RE.finditer(working):
                _ru = _m.group(2)
                if _ru not in _seen:
                    _seen.add(_ru)
                    _targets.append(_ru)
            _recovered = {}
            if _targets:
                _raise_if_cancelled(cancelled)
                from concurrent.futures import ThreadPoolExecutor

                def _rec_one(ru):
                    if cancelled is not None and cancelled():
                        raise CleanupCancelled()
                    try:
                        return ru, _recover_css_asset(ru, path, report, site_domain)
                    except CleanupCancelled:
                        raise
                    except Exception:  # noqa: BLE001 - a single asset failure never breaks the file
                        return ru, None

                with ThreadPoolExecutor(max_workers=min(_ARCHIVE_POOL, len(_targets))) as _ex:
                    for _ru, _res in _ex.map(_rec_one, _targets):
                        _recovered[_ru] = _res

            def _sub_css_url(m):
                quote, raw_url = m.group(1), m.group(2)
                new_ref = _recovered.get(raw_url)
                if new_ref is None:
                    return m.group(0)
                q = quote or "'"
                return f"url({q}{new_ref}{q})"

            working = CSS_URL_RE.sub(_sub_css_url, working)
        new_text = strip_wayback_appended_comments(unwayback(working))
        if is_css:
            new_text = FONT_IMPORT_RE.sub("", new_text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")
            changed.append(path.relative_to(site_root).as_posix())
    return changed


CORRUPTED_ASSET_MARKERS = (
    "file archived on",
    "retrieved from the",
    "internet archive",
    "wayback machine",
    "__wm.init",
    "petaboxloader",
)


def _looks_like_corrupted_wayback_asset(data):
    """A local 'image' file whose actual bytes are the Wayback Machine's own HTML
    interstitial/error/toolbar page, not real image data at all - happens when the
    real resource request failed or got redirected during capture, and the crawler
    saved wayback's own page under the asset's filename instead of a 404. Genuine
    image bytes never contain readable English text this early in the file, so a
    cheap decode-and-substring-search is enough to tell them apart."""
    if not data or _looks_like_image_bytes(data):
        return False
    sample = data[:4000].decode("utf-8", "ignore").lower()
    return any(marker in sample for marker in CORRUPTED_ASSET_MARKERS)


def recover_corrupted_local_assets(html_path, report, site_domain=None, cancelled=None):
    """Sweep every local raster-image file anywhere under the site folder for the
    wayback-html-masquerading-as-image problem (see _looks_like_corrupted_wayback_asset)
    and try to recover the REAL file. Unlike the CSS-url() wayback-wrapper case, a
    corrupted file carries no trace of its original URL anymore - so this guesses it
    lived at "<site_domain>/<filename>", the flat-URL layout every other recovery in
    this pipeline has consistently found for this kind of export, and asks the CDX
    API for the closest real capture of that guess. Overwrites the corrupted file
    in place on success (every existing local reference keeps working, nothing to
    rewrite); quarantines it into "<its own folder>/_wayback_removed/" on failure,
    so a broken reference is at least visibly broken instead of silently serving
    fake HTML mislabeled as an image."""
    if not site_domain:
        return
    site_root = html_path.parent
    bare_domain = _bare_domain(site_domain)
    for path in sorted(site_root.rglob("*")):
        _raise_if_cancelled(cancelled)  # each corrupted asset can cost a ~10s CDX fetch - stay abortable
        if not path.is_file() or path.suffix.lower() not in CSS_BG_RASTER_EXTS:
            continue
        if any(part in QUARANTINE_DIR_NAMES for part in path.relative_to(site_root).parts):
            continue
        try:
            data = path.read_bytes()
        except OSError:
            continue
        if not _looks_like_corrupted_wayback_asset(data):
            continue

        rel = path.relative_to(site_root).as_posix()
        guess_url = f"http://{bare_domain}/{path.name}"
        print(f"[corrupted-asset] {rel} looks like a wayback error page, not an image - trying {guess_url}")
        new_data, _ext = recover_asset_bytes(guess_url)
        if new_data is not None:
            path.write_bytes(new_data)
            report.recovered_images.append(f"{guess_url} -> {rel} (corrupted local file replaced with the real image)")
            print(f"[corrupted-asset] recovered: {rel}")
            continue

        trash_dir = path.parent / "_wayback_removed"
        dest = trash_dir / path.name
        i = 1
        while dest.exists():
            dest = trash_dir / f"{path.stem}_{i}{path.suffix}"
            i += 1
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest))
        report.failed_image_recovery.append(
            f"{rel}: was a wayback error page saved as a local image, could not recover the real file - quarantined"
        )
        print(f"[corrupted-asset] FAILED, quarantined: {rel}")


def drop_dangling_local_media(html_path, report):
    """A local <img>/<source> can be left pointing at a file that isn't on disk - most often
    because recover_corrupted_local_assets just quarantined a wayback-error-page-masquerading-as-
    an-image (sanjhapunjab's justice.jpg: the archive never captured it, so it can't be rebuilt)
    AFTER the HTML was already written, so localize_media_refs (which ran on the soup, while the
    file still existed) couldn't catch it. strip_dead_css_urls does exactly this for CSS
    backgrounds; this is its HTML-side twin. Prune each missing-local src / srcset candidate, and
    if a tag is left with no working source at all, drop it - a page showing nothing beats a
    broken-image icon. External (http/data) refs are never touched."""
    if html_path is None:
        return 0
    try:
        soup = BeautifulSoup(read_text_safe(html_path), PARSER)
    except Exception:  # noqa: BLE001 - a post-pass never fails the whole clean
        return 0
    base = html_path.parent

    def _missing_local(ref):
        ref = (ref or "").strip()
        if not ref or ref.startswith(("data:", "http://", "https://", "//", "#", "mailto:", "tel:")):
            return False  # not a local file ref - leave it be
        target = (base / ref.split("?")[0].split("#")[0]).resolve()
        return not target.exists()

    dropped, changed = 0, False
    for tag in soup.find_all(["img", "source"]):
        for attr in ("src", "poster"):
            if tag.has_attr(attr) and _missing_local(tag[attr]):
                del tag[attr]
                changed = True
        if tag.has_attr("srcset"):
            kept = [p.strip() for p in tag["srcset"].split(",")
                    if p.strip() and not _missing_local(p.strip().split(" ", 1)[0])]
            if len(kept) != len([p for p in tag["srcset"].split(",") if p.strip()]):
                changed = True
            if kept:
                tag["srcset"] = ", ".join(kept)
            else:
                del tag["srcset"]
        if tag.name in ("img", "source") and not tag.get("src") and not tag.get("srcset"):
            report.detached_media.append(
                f"<{tag.name}> \"{(tag.get('alt') or '').strip()[:40]}\" (локальный файл отсутствует, битую картинку убрал)"
            )
            tag.decompose()
            dropped += 1
            changed = True

    # WordPress wraps each image in an <a> to its full-size file; when that file is the missing
    # one (or the same one we just dropped), the link 404s. De-link it, and if the anchor only
    # existed to wrap the now-removed image (no text, no other media left), drop the empty shell.
    for a in soup.find_all("a", href=True):
        if _missing_local(a["href"]):
            del a["href"]
            changed = True
            if not a.get_text(strip=True) and not a.find(["img", "picture", "source", "svg", "video", "audio", "iframe"]):
                a.decompose()

    if changed:
        try:
            html_path.write_text(collapse_blank_lines(str(soup)), encoding="utf-8")
        except OSError:
            pass
    return dropped


def localize_media_refs(soup, html_path, site_domain, report, dry_run=False, cancelled=None):
    """No <img>/<source>/<video>/<audio> should point at an ABSOLUTE same-domain URL (e.g.
    "https://mysite.com/clip.mp4") - those must be local. For every such ref: relink it to the
    matching local file if it exists, else recover it from the archive and save it locally,
    else - it's genuinely dead - drop the ref and remove any <img>/<video>/<audio> left with
    no working source. Relative refs are left to the existing image-recovery / image-picker
    flow; only same-domain-absolute ones are rewritten here."""
    if not site_domain:
        return
    site_root = html_path.parent
    bare = _bare_domain(site_domain)
    recovered_dir = html_path.with_name(html_path.stem + "_files") / "_recovered"

    local_by_name = {}
    for p in site_root.rglob("*"):
        if p.is_file() and not any(part in QUARANTINE_DIR_NAMES for part in p.relative_to(site_root).parts):
            local_by_name.setdefault(p.name, p)

    # Pre-recover all same-domain-absolute media CONCURRENTLY (pool 4, globally bounded): each is a
    # slow serialized archive fetch and a gallery has dozens - the phase-60-70 bottleneck. Populates
    # local_by_name + disk so the serial pass below just relinks locally (no network in it).
    if not dry_run:
        pend = set()
        for tag in soup.find_all(["img", "source", "video", "audio"]):
            for attr in ("src", "poster"):
                if tag.has_attr(attr):
                    pend.add(tag[attr])
            if tag.has_attr("srcset"):
                for part in tag["srcset"].split(","):
                    bit = part.strip().split(" ", 1)
                    if bit and bit[0]:
                        pend.add(bit[0])
        want, seen = [], set()
        for url in pend:
            u = unwayback((url or "").strip())
            if not (u and is_external(u) and matches_suffix(domain_of(u), {bare})):
                continue
            name = Path(safe_urlsplit(u.split("?")[0].split("#")[0]).path).name or "asset"
            if name in local_by_name or name in seen:
                continue
            seen.add(name)
            want.append((u.split("#")[0], name))
        if want:
            from concurrent.futures import ThreadPoolExecutor

            def _rec_media(item):
                _u, _name = item
                if cancelled is not None and cancelled():
                    raise CleanupCancelled()
                try:
                    _data, _ = recover_asset_bytes(_u)
                except CleanupCancelled:
                    raise
                except Exception:  # noqa: BLE001
                    _data = None
                return _name, _u, _data

            with ThreadPoolExecutor(max_workers=min(_ARCHIVE_POOL, len(want))) as _ex:
                for _name, _u, _data in _ex.map(_rec_media, want):
                    if not _data:
                        continue
                    recovered_dir.mkdir(parents=True, exist_ok=True)
                    _dest = recovered_dir / _cap_asset_name(_name)
                    try:
                        _dest.write_bytes(_data)
                        local_by_name.setdefault(_name, _dest)
                        report.recovered_images.append(
                            f"{_u} -> {os.path.relpath(_dest, site_root).replace(os.sep, '/')} (media)")
                    except OSError:
                        pass

    def _localize(url):
        """(new_ref, dead) for a single ref - only touches same-domain absolute URLs."""
        u = unwayback((url or "").strip())
        if not u or not (is_external(u) and matches_suffix(domain_of(u), {bare})):
            return url, False  # not a same-domain absolute ref - leave it
        name = Path(safe_urlsplit(u.split("?")[0].split("#")[0]).path).name or "asset"
        found = local_by_name.get(name)
        if found:
            rel = os.path.relpath(found, site_root).replace(os.sep, "/")
            report.localized_media.append(f"{u} -> {rel}")
            return rel, False
        if not dry_run:
            try:
                data, _ext = recover_asset_bytes(u.split("#")[0])
            except Exception:  # noqa: BLE001
                data = None
            if data:
                recovered_dir.mkdir(parents=True, exist_ok=True)
                safe = _cap_asset_name(name)  # else a long blogspot/Google hash blows MAX_PATH -> crash
                dest = recovered_dir / safe
                try:
                    dest.write_bytes(data)
                except OSError:
                    return None, True  # can't save it -> treat as dead rather than crash the run
                local_by_name.setdefault(name, dest)
                rel = os.path.relpath(dest, site_root).replace(os.sep, "/")
                report.recovered_images.append(f"{u} -> {rel} (media)")
                report.localized_media.append(f"{u} -> {rel}")
                return rel, False
        return None, True  # same-domain absolute AND unrecoverable -> dead

    for tag in soup.find_all(["img", "source", "video", "audio"]):
        _raise_if_cancelled(cancelled)  # recovery per tag can hit the network - stay abortable
        for attr in ("src", "poster"):
            if tag.has_attr(attr):
                ref, dead = _localize(tag[attr])
                if dead:
                    report.detached_media.append(f"<{tag.name} {attr}=\"{tag[attr]}\">")
                    del tag[attr]
                elif ref != tag[attr]:
                    tag[attr] = ref
        if tag.has_attr("srcset"):
            kept = []
            for part in tag["srcset"].split(","):
                part = part.strip()
                if not part:
                    continue
                bits = part.split(" ", 1)
                ref, dead = _localize(bits[0])
                if not dead:
                    kept.append(ref + ((" " + bits[1]) if len(bits) > 1 else ""))
            if kept:
                tag["srcset"] = ", ".join(kept)
            else:
                del tag["srcset"]
        if tag.name in ("img", "source") and not tag.get("src") and not tag.get("srcset"):
            tag.decompose()

    for tag in soup.find_all(["video", "audio"]):
        if not tag.get("src") and not tag.find("source"):
            report.detached_media.append(f"<{tag.name}> (no working source)")
            tag.decompose()

    # Stylesheets and scripts must be localized the same way. Many exports link the theme's
    # own CSS/JS by an ABSOLUTE same-domain URL (e.g. "http://site/css/bootstrap.min.css")
    # while the file itself was saved into <name>_files/. Left as-is that ref is dead AND the
    # local copy looks unreferenced, so remove_unused_local_assets quarantines bootstrap /
    # landing-page.css and the whole layout collapses. Relink to the local file (recover if
    # missing), or drop the ref if it's genuinely dead.
    for tag in soup.find_all("link"):
        rel = tag.get("rel")
        rel = " ".join(rel).lower() if isinstance(rel, list) else str(rel or "").lower()
        if not tag.has_attr("href") or ("stylesheet" not in rel and "icon" not in rel):
            continue
        _raise_if_cancelled(cancelled)
        ref, dead = _localize(tag["href"])
        if dead:
            report.removed_links_css.append(tag["href"])
            tag.decompose()
        elif ref != tag["href"]:
            tag["href"] = ref
    for tag in soup.find_all("script", src=True):
        _raise_if_cancelled(cancelled)
        ref, dead = _localize(tag["src"])
        if dead:
            del tag["src"]
            if not (tag.string or "").strip():
                tag.decompose()  # dead src, no inline body -> inert, drop it
        elif ref != tag["src"]:
            tag["src"] = ref


# jQuery methods that always exist - a call to one of these proves nothing about plugins.
# (lower-cased at build time: every lookup below compares name.lower())
_JQ_CORE = {_n.lower() for _n in {
    "ready", "on", "off", "one", "trigger", "bind", "unbind", "delegate", "live", "click",
    "hover", "focus", "blur", "change", "submit", "keyup", "keydown", "keypress", "mouseover",
    "mouseout", "mouseenter", "mouseleave", "mousedown", "mouseup", "scroll", "resize", "load",
    "each", "map", "filter", "not", "is", "find", "closest", "parent", "parents", "parentsUntil",
    "children", "siblings", "next", "nextAll", "prev", "prevAll", "first", "last", "eq", "slice",
    "add", "end", "addClass", "removeClass", "toggleClass", "hasClass", "attr", "removeAttr",
    "prop", "removeProp", "val", "text", "html", "append", "appendTo", "prepend", "prependTo",
    "after", "before", "insertAfter", "insertBefore", "wrap", "wrapAll", "wrapInner", "unwrap",
    "remove", "detach", "empty", "clone", "replaceWith", "css", "width", "height", "innerWidth",
    "innerHeight", "outerWidth", "outerHeight", "offset", "position", "scrollTop", "scrollLeft",
    "show", "hide", "toggle", "fadeIn", "fadeOut", "fadeTo", "fadeToggle", "slideUp", "slideDown",
    "slideToggle", "animate", "stop", "delay", "queue", "dequeue", "data", "removeData", "index",
    "get", "size", "toArray", "serialize", "serializeArray", "ajaxComplete", "promise", "done",
    "fail", "always", "then", "push", "call", "apply", "test", "match", "replace", "split",
    "indexOf", "length", "extend", "trim", "inArray", "isArray", "isFunction", "parseJSON",
}}
_JQ_PLUGIN_DEF_RE = re.compile(r"(?:\$|jQuery)\s*\.\s*fn\s*\.\s*(\w+)\s*=|\.fn\.extend\(\s*\{\s*(\w+)", re.I)
# a plugin CALL on a jQuery result: ").pluginName(" / "$(sel).pluginName(" / "$this.pluginName("
_JQ_PLUGIN_CALL_RE = re.compile(r"(?:\)|\$\w*|\w+\$)\s*\.\s*([a-zA-Z_]\w{2,})\s*\(")


def drop_scripts_with_missing_plugins(soup, html_path, report):
    """Drop a kept LOCAL script that calls a jQuery plugin nothing else defines. The archive often
    saves a theme's plugin CALLER (jquery.dropdown.js) but not the plugin itself (hoverIntent), so
    the page throws "$(...).hoverIntent is not a function" on every load and the script does
    nothing anyway. Only same-page scripts are considered, and only names that aren't jQuery core."""
    site_root = html_path.parent
    scripts = []  # (tag, label, text) for every script that could run on this page
    for tag in soup.find_all("script"):
        src = (tag.get("src") or "").split("?")[0]
        if src:
            if is_external(src):
                continue
            p = (site_root / src).resolve()
            if p.is_file():
                scripts.append((tag, p.name, read_text_safe(p)))
        else:
            body = tag.string or tag.get_text() or ""
            if body.strip():
                scripts.append((tag, "(inline script)", body))
    if not scripts:
        return
    defined = set()
    for _t, _n, text in scripts:
        for m in _JQ_PLUGIN_DEF_RE.finditer(text):
            defined.add((m.group(1) or m.group(2) or "").lower())
    for tag, name, text in scripts:
        if _JQ_PLUGIN_DEF_RE.search(text):
            continue  # this file IS a plugin/library - keep it
        missing = {
            m.group(1) for m in _JQ_PLUGIN_CALL_RE.finditer(text)
            if m.group(1).lower() not in _JQ_CORE and m.group(1).lower() not in defined
        }
        if missing:
            report.removed_scripts.append(f"{name} (calls missing jQuery plugin: {', '.join(sorted(missing))})")
            tag.decompose()


def externalize_inline_scripts(soup, html_path, report, dry_run=False):
    """Move every surviving inline <script> out of the markup into ONE real .js file, loaded with
    `defer` at the end of <body>. Inline JS sitting in <head> is render-blocking and unfiles the
    page's behaviour; a deferred external file parses in parallel and runs after the DOM is up
    (which is what these jQuery(document).ready blocks wanted anyway). Trackers/analytics are
    already gone by this point (clean_scripts), and scripts that could only throw were dropped by
    drop_scripts_with_missing_plugins - so whatever is left here is real, wanted behaviour."""
    body = soup.find("body")
    if body is None:
        return
    inline = [t for t in soup.find_all("script")
              if not t.get("src") and (t.string or t.get_text() or "").strip()
              and "json" not in (t.get("type") or "").lower()]
    if not inline:
        return
    chunks = []
    for t in inline:
        code = (t.string or t.get_text() or "").strip()
        chunks.append(code if code.endswith((";", "}")) else code + ";")
        t.decompose()
    js_path = html_path.with_name(html_path.stem + "-inline.js")
    if not dry_run:
        js_path.write_text("\n\n".join(chunks) + "\n", encoding="utf-8")
    tag = soup.new_tag("script", src=js_path.name)
    tag["defer"] = ""
    body.append(tag)
    report.inline_scripts_externalized = f"{len(inline)} inline script(s) -> {js_path.name} (defer, end of body)"


# A 1x1 (or name-obvious) image is a tracking beacon, not content - never worth localizing.
_TRACKING_PIXEL_NAME_RE = re.compile(r"(?:^|/)(?:pixel|1x1|spacer|blank|clear|beacon|track(?:ing)?|px)\.(?:gif|png|jpg)",
                                     re.I)


def _is_tracking_pixel(tag, url):
    if _TRACKING_PIXEL_NAME_RE.search(url or ""):
        return True

    def _one(v):
        return str(v).strip().lower() in ("1", "1px", "0")

    return _one(tag.get("width") or "") and _one(tag.get("height") or "")


def localize_external_media(soup, html_path, site_domain, report, dry_run=False, cancelled=None):
    """Third-party images (paypal donate button, google thumbnails, ...) must not stay as live
    external requests: download each into <stem>_files/ and point at the local copy. If it can't
    be fetched, REMOVE the element outright rather than ship a broken/hot-linked image. Tracking
    beacons (1x1 / pixel.gif) are dropped without even trying. Covers <img>, <source> and
    PayPal-style <input type="image">. Same-domain refs are handled by localize_media_refs."""
    site_root = html_path.parent
    assets_dir = html_path.with_name(html_path.stem + "_files")
    bare = _bare_domain(site_domain or "")
    own = {bare} if bare else set()
    tags = [t for t in soup.find_all(["img", "source"])]
    tags += [t for t in soup.find_all("input") if (t.get("type") or "").lower() == "image"]
    for tag in tags:
        _raise_if_cancelled(cancelled)
        url = unwayback((tag.get("src") or "").strip())
        if not url or not is_external(url):
            continue  # relative/local ref - nothing to fetch
        if own and matches_suffix(domain_of(url), own):
            continue  # same-domain absolute - that's localize_media_refs' job
        if _is_tracking_pixel(tag, url):
            report.external_media_removed.append(f"{url} (tracking beacon)")
            tag.decompose()
            continue
        if dry_run:
            continue
        # _fetch_url_bytes RAISES on failure (it re-raises the last error) - it does not return
        # None. A single dead third-party image (404/timeout/DNS) therefore killed the whole
        # cleanup mid-run: mikatoronen died here with "HTTP Error 404: Not Found" and the site was
        # left half-cleaned, with the error surfaced on the card. An unreachable decoration must
        # only cost us that one element.
        try:
            data = _fetch_url_bytes(url)
        except Exception as e:  # noqa: BLE001 - any network/HTTP failure means "can't have it"
            data, why = None, type(e).__name__
        else:
            why = "empty response"
        if not data:
            report.external_media_removed.append(f"{url} (unreachable: {why})")
            tag.decompose()
            continue
        name = re.sub(r"[^\w.\-]+", "_", safe_urlsplit(url).path.rsplit("/", 1)[-1] or "img")[:80]
        if "." not in name:
            name += ".img"
        assets_dir.mkdir(parents=True, exist_ok=True)
        dest = assets_dir / name
        i = 1
        while dest.exists() and dest.stat().st_size != len(data):
            dest = assets_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        dest.write_bytes(data)
        rel = os.path.relpath(dest, site_root).replace(os.sep, "/")
        tag["src"] = rel
        report.external_media_localized.append(f"{url} -> {rel}")


# Legacy Flash/YouTube <object>/<embed> blocks are dead weight AND live third-party requests -
# a youtube <object> still pulls the player, which pulls googleads/doubleclick. Drop them.
_EMBED_URL_ATTRS = ("data", "src", "value")


def clean_iframes(soup, report):
    """PBN checklist item 24/25: no iframes/embeds to external resources, at all."""
    for iframe in soup.find_all("iframe"):
        src = unwayback(iframe.get("src", ""))
        if src and is_external(src) and domain_of(src) not in IFRAME_LIBRARY_DOMAINS:
            report.removed_iframes.append(src)
            iframe.decompose()
    # <object>/<embed> (old YouTube/Flash embeds) - same rule. These are what quietly load
    # googleads/doubleclick on a "clean" page, and Flash doesn't even run any more.
    for tag in soup.find_all(["object", "embed"]):
        urls = [unwayback(str(tag.get(a) or "")) for a in _EMBED_URL_ATTRS]
        urls += [unwayback(str(p.get("value") or "")) for p in tag.find_all("param")]
        hit = next((u for u in urls if u and is_external(u)
                    and domain_of(u) not in IFRAME_LIBRARY_DOMAINS), None)
        if hit:
            report.external_embeds_removed.append(f"<{tag.name}> {hit}")
            tag.decompose()


def scan_content_flags(soup, report):
    """Flag (never auto-delete) adult/casino keywords and mojibake/leaked HTML - these
    are full-sentence judgment calls with no clean substring to cut, unlike
    contacts/addresses/domain mentions which strip_contact_info actually removes."""
    body = soup.find("body") or soup
    for text_node in body.find_all(string=True):
        if isinstance(text_node, Comment):
            continue
        if text_node.parent and text_node.parent.name in ("script", "style"):
            continue
        text = str(text_node)
        stripped = text.strip()
        if not stripped:
            continue
        low = f" {stripped.lower()} "
        for kw in ADULT_KEYWORDS:
            if kw in low:
                report.flagged_adult.append(stripped[:100])
                break
        if MOJIBAKE_RE.search(stripped) or RAW_HTML_LEAK_RE.search(stripped):
            report.flagged_garbage_text.append(stripped[:100])


def _is_logo_hint(value):
    if not value:
        return False
    if isinstance(value, list):
        value = " ".join(value)
    low = value.lower()
    return any(hint in low for hint in LOGO_HINT_TOKENS)


def _is_excluded_text_hint(tag):
    for attr in ("class", "id"):
        val = tag.get(attr)
        if not val:
            continue
        low = " ".join(val).lower() if isinstance(val, list) else val.lower()
        if any(x in low for x in TEXT_LOGO_EXCLUDE_TOKENS):
            return True
    return False


def _drop_ancestor_duplicates(tags):
    """If a matched element contains another match as a descendant, drop the
    ancestor and keep the more specific inner one (avoids e.g. matching both the
    #logo wrapper div and the <h1> title inside it as two separate "logos")."""
    id_set = {id(t) for t in tags}
    kept = []
    for t in tags:
        if any(id(d) in id_set and id(d) != id(t) for d in t.find_all(True)):
            continue
        kept.append(t)
    return kept


def find_logo_elements(soup):
    """Returns (image_like_tags, text_logo_tags) - deduped, in document order."""
    images, texts = [], []
    seen = set()
    for tag in soup.find_all(["img", "svg", "image"]):
        hint = (
            _is_logo_hint(tag.get("class"))
            or _is_logo_hint(tag.get("id"))
            or _is_logo_hint(tag.get("alt"))
            or (tag.parent and (_is_logo_hint(tag.parent.get("class")) or _is_logo_hint(tag.parent.get("id"))))
        )
        if hint and id(tag) not in seen:
            seen.add(id(tag))
            images.append(tag)
    for tag in soup.find_all(True):
        if tag.name in ("script", "style"):
            continue
        if _is_excluded_text_hint(tag):
            continue
        if (_is_logo_hint(tag.get("class")) or _is_logo_hint(tag.get("id"))) and not tag.find(["img", "svg"]):
            if tag.get_text(strip=True) and id(tag) not in seen:
                seen.add(id(tag))
                texts.append(tag)
    texts = _drop_ancestor_duplicates(texts)
    return images, texts


def detect_and_report_logo(soup, report):
    images, texts = find_logo_elements(soup)
    for tag in images:
        label = tag.get("src") or tag.get("id") or " ".join(tag.get("class") or []) or "(svg logo)"
        report.logo_candidates.append(f"<{tag.name}> {label}")
    for tag in texts:
        report.logo_candidates.append(f"<{tag.name}> text: {tag.get_text(strip=True)[:60]!r}")


def apply_logo_image(soup, html_path, logo_path, report, dry_run=False):
    images, _ = find_logo_elements(soup)
    if not images:
        return

    logo_path = Path(logo_path)
    assets_dir = html_path.with_name(html_path.stem + "_files")
    dest = assets_dir / f"logo-new{logo_path.suffix}"
    if not dry_run:
        assets_dir.mkdir(exist_ok=True)
        shutil.copyfile(logo_path, dest)
    rel_href = f"{assets_dir.name}/{dest.name}"

    for tag in images:
        if tag.name == "img":
            tag["src"] = rel_href
            report.logo_replaced += 1
        elif tag.name in ("svg", "image"):
            new_img = soup.new_tag("img", src=rel_href, alt="logo")
            tag.replace_with(new_img)
            report.logo_replaced += 1


def apply_brand_text(soup, brand_text, report):
    _, texts = find_logo_elements(soup)
    old_name = texts[0].get_text(strip=True) if texts else None

    for tag in texts:
        tag.string = brand_text
        report.brand_text_replaced += 1

    title_tag = soup.find("title")
    if title_tag:
        if not old_name:
            old_name = title_tag.get_text(strip=True)
        title_tag.string = brand_text
        report.brand_text_replaced += 1

    for meta_name, attr, val in (
        ("og:site_name", "property", brand_text),
        ("og:title", "property", brand_text),
        ("twitter:title", "name", brand_text),
        ("apple-mobile-web-app-title", "name", brand_text),
    ):
        tag = soup.find("meta", attrs={attr: meta_name})
        if tag:
            tag["content"] = val
            report.brand_text_replaced += 1

    name_tag = soup.find("meta", attrs={"itemprop": "name"})
    if name_tag:
        name_tag["content"] = brand_text
        report.brand_text_replaced += 1

    # Description-style fields are full sentences, not just a name - swap the old
    # brand name out of them rather than overwriting the whole sentence.
    if old_name:
        for meta_name, attr in (
            ("og:description", "property"),
            ("twitter:description", "name"),
            ("description", "name"),
        ):
            tag = soup.find("meta", attrs={attr: meta_name})
            if tag and tag.get("content") and old_name in tag["content"]:
                tag["content"] = tag["content"].replace(old_name, brand_text)
                report.brand_text_replaced += 1
        desc_tag = soup.find("meta", attrs={"itemprop": "description"})
        if desc_tag and desc_tag.get("content") and old_name in desc_tag["content"]:
            desc_tag["content"] = desc_tag["content"].replace(old_name, brand_text)
            report.brand_text_replaced += 1


BRAND_TEXT_ATTRS = ("alt", "title", "aria-label", "placeholder", "content", "value")
BRAND_SKIP_PARENTS = {"script", "style"}


def replace_brand_in_text(soup, old_name, new_name, report=None):
    """Swap the old owner's brand name for the new one EVERYWHERE it appears as a whole
    word - across every visible text node AND the text-bearing attributes
    (alt/title/aria-label/placeholder/meta content/value). Unlike apply_brand_text
    (which only overwrites the detected logo element, <title> and og/twitter title
    meta), this reaches the running body text, headings, footer (©...), image alts and
    so on - the places the old company name is actually scattered. Case-insensitive,
    whole-word (so 'Sun' won't hit 'Sunday'); skips <script>/<style> bodies. Returns
    {'text_replacements': int, 'attr_replacements': int}."""
    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name or not new_name:
        return {"text_replacements": 0, "attr_replacements": 0}

    pattern = re.compile(r"(?<!\w)" + re.escape(old_name) + r"(?!\w)", re.IGNORECASE)

    text_hits = 0
    for node in soup.find_all(string=True):
        if node.parent is not None and node.parent.name in BRAND_SKIP_PARENTS:
            continue
        new_val, n = pattern.subn(new_name, str(node))
        if n:
            node.replace_with(new_val)
            text_hits += n

    attr_hits = 0
    for tag in soup.find_all(True):
        for attr in BRAND_TEXT_ATTRS:
            val = tag.get(attr)
            if isinstance(val, str) and val:
                new_val, n = pattern.subn(new_name, val)
                if n:
                    tag[attr] = new_val
                    attr_hits += n

    if report is not None:
        report.brand_body_replacements += text_hits + attr_hits
    return {"text_replacements": text_hits, "attr_replacements": attr_hits}


VERIFY_META_NAMES = {
    "google-site-verification", "msvalidate.01", "yandex-verification",
    "p:domain_verify", "norton-safeweb-site-verification", "alexaverifyid",
    "facebook-domain-verification", "baidu-site-verification", "wot-verification",
    "shopify-checkout-api-token", "csrf-token",
}
TRACE_META_NAMES = {"generator", "author", "copyright", "publisher"}
VERIFY_META_PROPS = {"fb:app_id", "fb:admins", "fb:pages"}
COPYRIGHT_YEAR_RE = re.compile(r"(©|copyright)(\s*[-–—]?\s*)\d{4}(?:\s*[-–—]\s*\d{4})?", re.IGNORECASE)


def strip_owner_traces(soup, update_year=True, report=None):
    """Remove the previous owner's leftover fingerprints that the script-level cleanup
    doesn't touch: search-console / social verification <meta> tags (google-site-
    verification, yandex, bing msvalidate, fb domain verify, pinterest, ...),
    fb:app_id/fb:admins, generator/author/copyright/publisher meta, and any GoogleTag-
    Manager/Analytics <noscript> fallback still baked in. Optionally bumps a '© 20xx'
    footer year to the current year. Returns the list of removed items."""
    removed = []
    for meta in list(soup.find_all("meta")):
        name = (meta.get("name") or "").strip().lower()
        prop = (meta.get("property") or "").strip().lower()
        if name in VERIFY_META_NAMES or name in TRACE_META_NAMES or prop in VERIFY_META_PROPS:
            removed.append(f'<meta {"property" if prop else "name"}="{prop or name}">')
            meta.decompose()

    for ns in list(soup.find_all("noscript")):
        blob = str(ns)
        if "googletagmanager.com" in blob or "google-analytics.com" in blob:
            removed.append("<noscript> GTM/GA fallback")
            ns.decompose()

    year_updates = 0
    if update_year:
        current = str(datetime.now().year)
        for node in soup.find_all(string=COPYRIGHT_YEAR_RE):
            if node.parent is not None and node.parent.name in BRAND_SKIP_PARENTS:
                continue
            new_val, n = COPYRIGHT_YEAR_RE.subn(lambda m: f"{m.group(1)}{m.group(2)}{current}", str(node))
            if n:
                node.replace_with(new_val)
                year_updates += n

    if report is not None:
        report.removed_owner_traces.extend(removed)
        report.owner_trace_year_updates += year_updates
    return {"removed": removed, "year_updates": year_updates}


def _brand_name_from_domain(domain):
    """'best-coffee-shop.com' -> 'Best Coffee Shop'; 'mikatoronen.com' -> 'Mikatoronen'."""
    if not domain:
        return None
    stem = domain.split(".")[0]
    words = [w for w in re.split(r"[-_]+", stem) if w]
    return " ".join(w.capitalize() for w in words) or None


def _logo_target_size(tag):
    """Best-effort box size (px) the logo currently occupies, from width/height
    attributes or inline style - falls back to a generic logo-ish box so the
    generated wordmark still has something sane to fit into."""

    def _px(v):
        if v is None:
            return None
        m = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:px)?\s*$", str(v))
        return int(float(m.group(1))) if m else None

    style = tag.get("style") or ""
    w = _px(tag.get("width"))
    h = _px(tag.get("height"))
    if w is None:
        m = re.search(r"width\s*:\s*(\d+(?:\.\d+)?)px", style)
        if m:
            w = int(float(m.group(1)))
    if h is None:
        m = re.search(r"height\s*:\s*(\d+(?:\.\d+)?)px", style)
        if m:
            h = int(float(m.group(1)))
    if w and not h:
        h = round(w * 0.3)
    if h and not w:
        w = round(h * 3.4)
    if not w or not h:
        w, h = 176, 52
    w = max(60, min(w, 420))
    h = max(20, min(h, 140))
    return w, h


def _find_truetype_font():
    for candidate in ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            from PIL import ImageFont

            ImageFont.truetype(candidate, 40)
            return candidate
        except Exception:
            continue
    return None


def parse_color(spec):
    """Accepts 'white', 'black', '#rgb', '#rrggbb', 'rgb(r,g,b)', or a bare 'r,g,b' -
    returns an (r, g, b) tuple, or None if spec is empty/unrecognized (falls back to
    the deterministic per-name color)."""
    if not spec:
        return None
    s = spec.strip().lower()
    if s in ("white", "#fff", "#ffffff"):
        return (255, 255, 255)
    if s in ("black", "#000", "#000000"):
        return (0, 0, 0)
    m = re.match(r"^#?([0-9a-f]{6})$", s)
    if m:
        h = m.group(1)
        return tuple(int(h[i : i + 2], 16) for i in (0, 2, 4))
    m = re.match(r"^#?([0-9a-f]{3})$", s)
    if m:
        h = m.group(1)
        return tuple(int(c * 2, 16) for c in h)
    m = re.match(r"^rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", s)
    if m:
        return tuple(min(255, int(x)) for x in m.groups())
    m = re.match(r"^(\d+)\s*,\s*(\d+)\s*,\s*(\d+)$", s)
    if m:
        return tuple(min(255, int(x)) for x in m.groups())
    return None


def _generate_auto_logo_image(dest_path, brand_text, box_w, box_h, color=None):
    """Draw `brand_text` as a bold wordmark that fits inside box_w x box_h - font
    size shrinks to fit longer domain-derived names into the same slot the
    original image logo occupied, so the swap doesn't break the layout. `color` is
    an explicit (r,g,b) override; without it, color is deterministic per brand_text."""
    _ensure_pillow()
    from PIL import Image, ImageDraw, ImageFont

    scale = 3
    W, H = box_w * scale, box_h * scale
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = color or _letter_to_bg_color(brand_text)

    font_path = _find_truetype_font()
    pad = W * 0.06
    size = int(H * 0.62)
    font = ImageFont.truetype(font_path, size) if font_path else ImageFont.load_default()

    while size > 8 and font_path:
        bbox = draw.textbbox((0, 0), brand_text, font=font)
        tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if tw <= W - pad * 2 and th <= H - pad * 2:
            break
        size -= 2
        font = ImageFont.truetype(font_path, size)

    bbox = draw.textbbox((0, 0), brand_text, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1]), brand_text, font=font, fill=color + (255,))

    img = img.resize((box_w, box_h), Image.LANCZOS)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, format="PNG")


def apply_auto_logo(soup, html_path, site_domain, report, brand_text=None, dry_run=False, color=None):
    """Replace every detected image-based logo (<img>/<svg>) with a generated text
    wordmark, sized to fit the box the original logo occupied and named after the
    site domain (or brand_text, if given) - no logo file needs to be supplied.
    `color` is an explicit (r,g,b) override (see parse_color); without it, color is
    deterministic per name. Returns the name used, or None if there was no image logo."""
    images, _ = find_logo_elements(soup)
    if not images:
        return None
    name = brand_text or _brand_name_from_domain(site_domain) or "Site"

    sizes = [_logo_target_size(t) for t in images]
    gen_w, gen_h = max(s[0] for s in sizes), max(s[1] for s in sizes)

    assets_dir = html_path.with_name(html_path.stem + "_files")
    dest = assets_dir / "logo-auto.png"
    if not dry_run:
        _generate_auto_logo_image(dest, name, gen_w, gen_h, color=color)
    rel_href = f"{assets_dir.name}/{dest.name}"

    for tag, (w, h) in zip(images, sizes):
        if tag.name == "img":
            tag["src"] = rel_href
            tag["width"] = str(w)
            tag["height"] = str(h)
            report.logo_replaced += 1
        elif tag.name in ("svg", "image"):
            new_img = soup.new_tag("img", src=rel_href, alt=name, width=str(w), height=str(h))
            tag.replace_with(new_img)
            report.logo_replaced += 1
    return name


def _ensure_pillow():
    try:
        import PIL  # noqa: F401
    except ImportError:
        print("[setup] installing missing dependency: Pillow ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "Pillow"])
        import importlib

        importlib.invalidate_caches()


def _letter_to_bg_color(letter):
    """Deterministic (same letter -> same color every run) but varied background."""
    import colorsys
    import hashlib

    digest = hashlib.md5(letter.encode("utf-8")).hexdigest()
    hue = (int(digest[:8], 16) % 360) / 360.0
    r, g, b = colorsys.hls_to_rgb(hue, 0.42, 0.55)
    return (int(r * 255), int(g * 255), int(b * 255))


def _generate_default_favicon(dest_path, letter):
    """No branding assets to work with yet - draw a simple flat-color monogram icon
    (first letter of the site/brand name) instead of leaving no favicon at all."""
    _ensure_pillow()
    from PIL import Image, ImageDraw, ImageFont

    size = 256
    bg = _letter_to_bg_color(letter)
    img = Image.new("RGB", (size, size), bg)
    draw = ImageDraw.Draw(img)

    font = None
    for candidate in ("seguisb.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "arial.ttf"):
        try:
            font = ImageFont.truetype(candidate, int(size * 0.6))
            break
        except Exception:
            continue
    if font is None:
        font = ImageFont.load_default()

    bbox = draw.textbbox((0, 0), letter, font=font)
    w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(((size - w) / 2 - bbox[0], (size - h) / 2 - bbox[1]), letter, font=font, fill="white")

    dest_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(dest_path, format="ICO", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)])


def ensure_favicon(soup, html_path, favicon_path, report, dry_run=False, brand_hint=None):
    head = soup.find("head")
    existing = None
    existing_is_local = False
    if head:
        for link in head.find_all("link"):
            rel = link.get("rel") or []
            rel = " ".join(rel) if isinstance(rel, list) else str(rel)
            if rel.lower() in FAVICON_RELS:
                existing = link
                href = link.get("href", "")
                existing_is_local = bool(href) and not is_external(href)
                break
    report.favicon_present = existing_is_local
    if existing and not existing_is_local:
        report.favicon_was_external = existing.get("href", "")

    if not head:
        return

    if favicon_path:
        assets_dir = html_path.with_name(html_path.stem + "_files")
        src_path = Path(favicon_path)
        dest = assets_dir / f"favicon{src_path.suffix}"
        if not dry_run:
            assets_dir.mkdir(exist_ok=True)
            shutil.copyfile(src_path, dest)
        rel_href = f"{assets_dir.name}/{dest.name}"
        if existing:
            existing["href"] = rel_href
        else:
            link_tag = soup.new_tag("link", rel="shortcut icon", href=rel_href)
            head.append(link_tag)
        report.favicon_present = True
        report.favicon_set = True
        return

    if existing_is_local:
        return  # already a real local favicon - leave it alone

    # No favicon, or the only one found points off-site (e.g. a leftover CMS CDN
    # default icon) - auto-generate a simple local one so the site never ships
    # without a favicon at all.
    letter = (brand_hint or "S").strip()[:1].upper() or "S"
    assets_dir = html_path.with_name(html_path.stem + "_files")
    dest = assets_dir / "favicon-generated.ico"
    rel_href = f"{assets_dir.name}/{dest.name}"
    if not dry_run:
        _generate_default_favicon(dest, letter)
    if existing:
        existing["href"] = rel_href
        existing["rel"] = "shortcut icon"
        existing["type"] = "image/x-icon"
    else:
        link_tag = soup.new_tag("link", rel="shortcut icon", type="image/x-icon", href=rel_href)
        head.append(link_tag)
    report.favicon_present = True
    report.favicon_set = True
    report.favicon_auto_generated = True


def ensure_local_seo_files(html_path, site_domain, report, dry_run=False, overwrite=False):
    """PBN checklist items 39/40: robots.txt and sitemap.xml must exist. Archivarix
    normally generates these; a plain wayback/browser export never does - so create
    them ourselves if missing, rather than just flagging the gap. With overwrite=True
    (the cleanup pass) they're (re)generated even if already present - both are
    machine-generated, so replacing a stale one is safe and keeps them in sync with the
    current domain/page set."""
    site_root = html_path.parent
    robots_path = site_root / "robots.txt"
    sitemap_path = site_root / "sitemap.xml"
    report.robots_present = robots_path.is_file()
    report.sitemap_present = sitemap_path.is_file()

    if dry_run:
        return

    domain = site_domain or "example.com"

    if overwrite or not report.robots_present:
        robots_path.write_text(
            f"User-agent: *\nAllow: /\n\nSitemap: https://{domain}/sitemap.xml\n", encoding="utf-8"
        )
        report.robots_present = True
        report.robots_created = True

    if overwrite or not report.sitemap_present:
        html_files = sorted(site_root.rglob("*.html"))
        urls = []
        for f in html_files:
            rel = f.relative_to(site_root).as_posix()
            loc = f"https://{domain}/" if rel == "index.html" else f"https://{domain}/{rel}"
            urls.append(loc)
        entries = "\n".join(f"  <url><loc>{u}</loc></url>" for u in urls)
        xml = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            f"{entries}\n"
            "</urlset>\n"
        )
        sitemap_path.write_text(xml, encoding="utf-8")
        report.sitemap_present = True
        report.sitemap_created = True


def _htaccess_with_canonical_redirect(site_domain):
    """DEFAULT_HTACCESS ships with BOTH www<->non-www 301 blocks commented out. Uncomment the
    one that enforces the site's chosen canonical form: a www-named folder wants everyone sent
    to www (non-www -> www); a bare folder wants the opposite (www -> non-www). No domain known
    -> leave both commented (don't guess a redirect that could loop)."""
    text = DEFAULT_HTACCESS
    if not site_domain:
        return text
    if site_domain.lower().startswith("www."):
        return text.replace(
            "# RewriteCond %{HTTP_HOST} !^www\\. [NC]\n# RewriteRule ^(.*)$ https://www.%{HTTP_HOST}/$1 [R=301,L]",
            "RewriteCond %{HTTP_HOST} !^www\\. [NC]\nRewriteRule ^(.*)$ https://www.%{HTTP_HOST}/$1 [R=301,L]",
        )
    return text.replace(
        "# RewriteCond %{HTTP_HOST} ^www\\.(.*)$ [NC]\n# RewriteRule ^(.*)$ https://%1/$1 [R=301,L]",
        "RewriteCond %{HTTP_HOST} ^www\\.(.*)$ [NC]\nRewriteRule ^(.*)$ https://%1/$1 [R=301,L]",
    )


def ensure_htaccess(html_path, report, dry_run=False, overwrite=False, site_domain=None):
    """Every restored PBN site gets the same standard .htaccess (gzip compression,
    .js.gz serving, font/webp mime types) plus the canonical www/non-www 301 redirect for
    site_domain's chosen form. Written unless one is already present, or always
    (overwrite=True, the cleanup pass) - the block is a fixed standard, so replacing a
    leftover export .htaccess with it is the intended behaviour."""
    site_root = html_path.parent
    htaccess_path = site_root / ".htaccess"
    report.htaccess_present = htaccess_path.is_file()
    if dry_run or (report.htaccess_present and not overwrite):
        return
    htaccess_path.write_text(_htaccess_with_canonical_redirect(site_domain), encoding="utf-8")
    report.htaccess_created = True
    report.htaccess_present = True


def ensure_canonical(soup, html_path, site_domain, report, dry_run=False):
    """Rewrite (or add) <link rel="canonical"> to point at the site's real target
    domain - named after the export folder - instead of leaving whatever the
    archived page happened to have (old domain, a wayback URL, or nothing)."""
    if not site_domain:
        return
    site_root = html_path.parent
    try:
        rel = html_path.relative_to(site_root).as_posix()
    except ValueError:
        rel = html_path.name
    if rel == "index.html":
        loc = f"https://{site_domain}/"
    else:
        slug = rel[:-len(".html")] if rel.endswith(".html") else rel
        loc = f"https://{site_domain}/{slug}"

    head = soup.find("head")
    if not head:
        return
    canonical = soup.find("link", rel="canonical")
    old_href = canonical.get("href") if canonical else None
    if old_href == loc:
        return

    report.canonical_old = old_href
    report.canonical_new = loc
    if dry_run:
        return
    if canonical:
        canonical["href"] = loc
    else:
        head.append(soup.new_tag("link", rel="canonical", href=loc))


def _rewrite_domain_absolute_links(soup, site_domain):
    """Point every absolute in-page link that targets THIS site's domain (mainly the logo/home
    link) at site_domain's exact www/non-www form, preserving path/query/hash. Returns count."""
    bare = _bare_domain(site_domain)
    if not bare:
        return 0
    n = 0
    for a in soup.find_all("a", href=True):
        href = (a["href"] or "").strip()
        if not is_external(href) or not matches_suffix(domain_of(href), {bare}):
            continue
        parts = safe_urlsplit(href)
        rest = parts.path or "/"
        if parts.query:
            rest += "?" + parts.query
        if parts.fragment:
            rest += "#" + parts.fragment
        new = f"https://{site_domain}{rest}"
        if a["href"] != new:
            a["href"] = new
            n += 1
    return n


def recanonicalize_domain(site_dir, site_domain):
    """Switch an ALREADY-cleaned site between www and non-www WITHOUT re-running the destructive
    full cleanup (that pass is for raw archive exports, not clean sites). Only touches what
    actually depends on the domain form: <link canonical>, the absolute in-page domain links
    (logo/home), and the domain-dependent config files (robots.txt/sitemap.xml/.htaccess)."""
    site_dir = Path(site_dir)
    report = Report()
    pages = 0
    for html_path in sorted(site_dir.rglob("*.html")):
        if any(part in QUARANTINE_DIR_NAMES for part in html_path.relative_to(site_dir).parts):
            continue
        soup = BeautifulSoup(read_text_safe(html_path), PARSER)
        ensure_canonical(soup, html_path, site_domain, report)
        _rewrite_domain_absolute_links(soup, site_domain)
        _reorder_head_seo(soup)  # keep description -> canonical -> fonts order after the swap
        html_path.write_text(collapse_blank_lines(str(soup)), encoding="utf-8")
        pages += 1
    index_html = site_dir / "index.html"
    if index_html.is_file():
        ensure_local_seo_files(index_html, site_domain, report, overwrite=True)
        ensure_htaccess(index_html, report, overwrite=True, site_domain=site_domain)
    return {"pages": pages, "domain": site_domain}


def _reorder_head_seo(soup):
    """Enforce the head order the project wants: <title> -> <meta description> -> <link
    canonical> -> the self-hosted fonts <link>, placed right after <meta charset>. Only reorders
    the tags that actually exist. Runs late (after canonical + fonts are both in the head)."""
    head = soup.find("head")
    if not head:
        return
    title = head.find("title")
    desc = head.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    canonical = head.find("link", rel="canonical")
    fontcss = head.find("link", attrs={"data-site-studio-font": True})
    seq = [t for t in (title, desc, canonical, fontcss) if t is not None]
    if not seq:
        return
    charset = head.find("meta", charset=True) or head.find(
        "meta", attrs={"http-equiv": re.compile(r"^content-type$", re.I)}
    )
    for t in seq:
        t.extract()
    ref = charset
    for t in seq:
        if ref is None:
            head.insert(0, t)
        else:
            ref.insert_after(t)
        ref = t


# Unicode script -> ISO-639-1 language, for pages whose <html> never declared a lang and carry no
# og:locale. Ordered most-specific first (Gurmukhi before the generic Indic fallback). Latin is the
# implicit default (en) - listing every Latin-script language is hopeless, and en is the safe base.
_SCRIPT_LANG_RANGES = (
    ("ru", (0x0400, 0x04FF)),   # Cyrillic
    ("el", (0x0370, 0x03FF)),   # Greek
    ("he", (0x0590, 0x05FF)),   # Hebrew
    ("ar", (0x0600, 0x06FF)),   # Arabic (also Urdu/Farsi - og:locale disambiguates when present)
    ("hi", (0x0900, 0x097F)),   # Devanagari
    ("pa", (0x0A00, 0x0A7F)),   # Gurmukhi (Punjabi)
    ("bn", (0x0980, 0x09FF)),   # Bengali
    ("ta", (0x0B80, 0x0BFF)),   # Tamil
    ("th", (0x0E00, 0x0E7F)),   # Thai
    ("ja", (0x3040, 0x30FF)),   # Hiragana/Katakana -> Japanese
    ("zh", (0x4E00, 0x9FFF)),   # CJK unified -> Chinese
    ("ko", (0xAC00, 0xD7A3)),   # Hangul
)
_LANG_CODE_RE = re.compile(r"^[a-zA-Z]{2,3}(?:-[a-zA-Z0-9]{2,8})*$")


def _detect_page_lang(soup):
    """Best-effort page language for <html lang>. og:locale (en_US) and a content-language meta are
    authoritative when present; otherwise sniff the dominant non-Latin script of the visible text.
    Latin script (and empty/ambiguous) -> 'en', the safe default for a restored PBN page."""
    loc = soup.find("meta", property="og:locale") or soup.find("meta", attrs={"property": "og:locale"})
    if loc and loc.get("content"):
        code = re.split(r"[_\-]", loc["content"].strip())[0].lower()
        if len(code) in (2, 3) and code.isalpha():
            return code
    cl = soup.find("meta", attrs={"http-equiv": re.compile(r"^content-language$", re.I)})
    if cl and cl.get("content"):
        code = re.split(r"[,_\-]", cl["content"].strip())[0].lower()
        if len(code) in (2, 3) and code.isalpha():
            return code
    body = soup.find("body")
    text = body.get_text(" ", strip=True) if body else ""
    if not text:
        return "en"
    counts = {}
    for ch in text[:8000]:  # a sample is plenty to find the dominant script
        o = ord(ch)
        for lang, (lo, hi) in _SCRIPT_LANG_RANGES:
            if lo <= o <= hi:
                counts[lang] = counts.get(lang, 0) + 1
                break
    if counts:
        return max(counts, key=counts.get)
    return "en"


def ensure_html_lang(soup, report):
    """<html> must declare a language (accessibility, SEO, and it drives the footer copyright's
    localisation). Keep a valid existing lang; otherwise auto-pick one (see _detect_page_lang)."""
    html = soup.find("html")
    if html is None:
        return
    cur = (html.get("lang") or "").strip()
    if cur and _LANG_CODE_RE.match(cur):
        return  # already declared and well-formed - leave the site's own choice
    lang = _detect_page_lang(soup)
    if lang:
        html["lang"] = lang
        report.html_lang = lang


def dedupe_head_meta(soup, report):
    """Wayback/CMS exports pile up duplicate social metas (sanjhapunjab shipped og:site_name x3,
    og:type x3, og:title x2). Keep the FIRST of each property/name key and drop the rest - the
    duplicates are pure head clutter. Repeatable properties (og:image, article:tag, ...) are left
    alone; those legitimately appear more than once."""
    head = soup.find("head")
    if head is None:
        return 0
    repeatable = ("og:image", "og:video", "og:audio", "article:tag", "article:author",
                  "article:section", "book:author", "music:musician")
    seen, removed = set(), 0
    for m in head.find_all("meta"):
        prop = (m.get("property") or "").strip().lower()
        name = (m.get("name") or "").strip().lower()
        key = ("property", prop) if prop else (("name", name) if name else None)
        if key is None or key[1].startswith(repeatable):
            continue
        if key in seen:
            m.decompose()
            removed += 1
        else:
            seen.add(key)
    if removed:
        report.head_meta_deduped = removed
    return removed


def check_and_fix_noindex(soup, report):
    """PBN checklist item 41: important pages must not be blocked from indexing."""
    tag = soup.find("meta", attrs={"name": re.compile("^robots$", re.I)})
    if tag and tag.get("content") and "noindex" in tag["content"].lower():
        report.noindex_removed = tag["content"]
        tag.decompose()


JS_REDIRECT_RE = re.compile(
    r"(?:window\.location(?:\.href)?|top\.location|document\.location)\s*=\s*['\"]([^'\"]+)['\"]"
)


def check_and_fix_redirect(soup, site_domain, report):
    """PBN checklist item 4: no redirect to an external domain (meta-refresh or JS)."""
    meta = soup.find("meta", attrs={"http-equiv": re.compile("^refresh$", re.I)})
    if meta and meta.get("content"):
        m = re.search(r"url\s*=\s*([^;]+)", meta["content"], re.I)
        if m:
            target = unwayback(m.group(1).strip().strip("'\""))
            if is_external(target) and not matches_suffix(
                domain_of(target), {_bare_domain(site_domain)} if site_domain else set()
            ):
                report.external_redirect_found = target
        meta.decompose()

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        m = JS_REDIRECT_RE.search(text)
        if m:
            target = unwayback(m.group(1))
            if is_external(target) and not matches_suffix(
                domain_of(target), {_bare_domain(site_domain)} if site_domain else set()
            ):
                report.external_redirect_found = report.external_redirect_found or target
                script.decompose()


def check_internal_link_targets(soup, html_path, report, fix=True):
    """PBN checklist item 5: main menu pages must actually open. A single-page wayback
    export commonly keeps nav links to sibling pages that were never saved. Can't
    fabricate those pages, so by default (fix=True) neutralize the link to "#" rather
    than ship a dead link - still logged in the report so it's not silently lost."""
    site_root = html_path.parent
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if href.startswith(("#", "mailto:", "tel:", "javascript:")) or is_external(href):
            continue
        path_part = href.split("#")[0].split("?")[0].lstrip("/")
        if not path_part:
            continue
        candidate = (site_root / path_part).resolve()
        candidate_html = candidate if candidate.suffix else candidate.with_suffix(".html")
        exists = candidate.is_file() or candidate_html.is_file() or (candidate / "index.html").is_file()
        if not exists:
            report.broken_internal_targets.append(href)
            if fix:
                a["href"] = "#"


# --------------------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------------------


# --------------------------------------------------------------------------------------
# AI semantic pass (optional, Haiku) - see site_studio/semantics.py. All best-effort: the
# deterministic cleanup runs identically with or without an ANTHROPIC_API_KEY.
# --------------------------------------------------------------------------------------

def _semantics():
    """Lazily import the optional semantics module. It lives in site_studio/, which isn't on
    sys.path when this file runs as a root CLI - add it on demand. Returns the module or None."""
    try:
        import semantics  # already importable when the process started from site_studio (server)
        return semantics
    except ImportError:
        pass
    try:
        sub = Path(__file__).resolve().parent / "site_studio"
        if str(sub) not in sys.path:
            sys.path.insert(0, str(sub))
        import semantics
        return semantics
    except Exception:  # noqa: BLE001 - the whole feature is optional
        return None


_SEM_HEADINGS = ("h1", "h2", "h3")
_SEM_RENAMEABLE = {"div", "section"}  # only rewrite generic containers, never real landmarks
# A hero/intro/banner band is CONTENT, never the page header or footer - it becomes a <section>.
# The page header is the top NAV bar. These two patterns drive the deterministic header/hero rules
# below so a hero (with, say, a couple of CTA buttons) is never mistaken for the menu bar.
_SEM_HERO_RE = re.compile(
    r"(?:^|[\s_-])(?:hero|intro|banner|masthead|jumbotron|slider|carousel|cover|splash|welcome)(?:[\s_-]|$)",
    re.I,
)
_SEM_NAVBAR_RE = re.compile(
    r"(?:^|[\s_-])(?:navbar|nav-bar|topnav|top-nav|main-nav|primary-nav|site-?header|masthead-nav|header-nav|menu-bar)(?:[\s_-]|$)",
    re.I,
)


def _sem_cls(el):
    """Class AND id together - every semantic check must see both.

    Old sites mark structure with ids, not classes: `<div id="top">`, `<div id="footer">`,
    `<div id="navigation">`. Reading only `class` made those invisible, so a site whose real footer
    was `<div id="footer">` shipped with no footer at all, and its top bar was never recognised as
    the header. site_edit already matched on id; the cleaner did not, and the two disagreeing about
    the same page is what produced half of today's landmark bugs."""
    if not hasattr(el, "get"):
        return ""
    return (" ".join(el.get("class", []) or []) + " " + (el.get("id") or "")).strip()


def can_be_header(el, root=None):
    """THE single answer to "may this element become the page <header>?".

    This check used to live in four places with four different signal sets - one read only the
    class, another class+id, a third took the first <nav> in the document, a fourth required an
    href. They disagreed about the same page, so a rule fixed in one path left the others open: the
    CTA button pair "Get In Touch / Connect" was rejected by the deterministic path and then walked
    straight in through the model's plan, twice. One function, used by every path, is the only way
    that class of bug stops coming back."""
    if getattr(el, "name", None) is None:
        return False
    ident = _sem_cls(el)
    if _SEM_HERO_RE.search(ident) or _CTA_CLASS_RE.search(ident):
        return False                      # a hero band or a CTA button pair is content
    if _MOBILE_PANEL_RE.search(ident):
        return False                      # an off-canvas drawer is not the page header
    if el.find_parent(lambda p: p is not el and _SEM_HERO_RE.search(_sem_cls(p))) is not None:
        return False                      # sitting inside a hero makes it part of the hero
    if el.name in ("main", "body", "html", "footer"):
        return False
    if _too_big_for_header(el, root):
        return False                      # a header is a bar, not a third of the page
    links = [a for a in el.find_all("a") if a.get_text(strip=True)]
    if len(links) < 2:
        return False                      # a header carries navigation
    return True


def _too_big_for_header(el, root=None):
    """True when a block is too much of the page to be its header bar.

    The footer has had a 40% cap for a while; the header never got one, so a whole left-hand column
    (menu + a "Mayor's Corner" content block) could be promoted to <header> - and everything inside
    a header stops counting as content, so those sections vanish from the menu. A header is a bar."""
    body = (root.find_parent("body") if root is not None else None) or el.find_parent("body")
    if body is None:
        return False
    total = len(body.get_text(" ", strip=True)) or 1
    return len(el.get_text(" ", strip=True)) > total * 0.4


def _looks_like_nav_block(el):
    """A bare container (div/ul/center/table) that IS the site's menu even with NO nav class - many
    text links that dominate its content and no heading of its own. This is what separates a real
    menu bar (lots of short links, little else) from a hero band (a couple of CTA buttons + big
    heading text), so old exports whose nav is just `<div>`/`<center>` full of links still get a
    proper <header> instead of being missed."""
    if getattr(el, "name", None) not in ("div", "ul", "center", "nav", "table", "td", "tr", "p", "section"):
        return False
    links = [a for a in el.find_all("a") if a.get_text(strip=True)]
    if len(links) < 3:
        return False
    link_text = sum(len(a.get_text(" ", strip=True)) for a in links)
    total = len(el.get_text(" ", strip=True)) or 1
    ratio = link_text / total
    heading = el.find(_SEM_HEADINGS)
    if heading is not None:
        # A heading used to veto the block outright, on the logic that a nav bar has none. But
        # menus do carry small labels - sylhetcitycorporation's real menu (26 links, 81% of its
        # text) holds a "Menu" caption, so it was rejected and an 8-link services list eight levels
        # deeper became the header instead. Veto only when the block is not overwhelmingly links,
        # or when the heading is long enough to be actual content rather than a caption.
        if ratio < 0.75 or len(heading.get_text(" ", strip=True)) > 30:
            return False
    return ratio > 0.55  # predominantly links -> it's the menu


# A class like page-header / site-header / masthead names the header bar outright. Themes use it
# constantly and it carries no nav/menu token, so the navbar regex alone missed it (bikenfoot's
# div.page-header held the whole 14-link menu and was never recognised).
_SEM_HEADER_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:(?:page|site|main|top|global|primary)[\s_-]?)?"
                                  r"(?:header|masthead|topbar|top[\s_-]?nav)\d*(?:[\s_-]|$)", re.I)


_CTA_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:cta|call-to-action|buttons?|btns?|actions?)(?:[\s_-]|$)", re.I)


def _is_header_like(el):
    """The site's header BAR: named as a header by its class (or a navbar/nav) and actually
    carrying navigation. The link requirement is what keeps a decorative `.page-header` title
    band - a heading with no menu in it - from being mistaken for the site header."""
    if getattr(el, "name", None) is None:
        return False
    cls = _sem_cls(el)
    if _SEM_HERO_RE.search(cls) or _CTA_CLASS_RE.search(cls):
        return False  # a hero band or a CTA button pair is content, never the site header
    if el.find_parent(lambda p: p is not el and _SEM_HERO_RE.search(_sem_cls(p))) is not None:
        return False  # sitting inside a hero makes it part of the hero
    named = bool(_SEM_HEADER_CLASS_RE.search(cls) or _SEM_NAVBAR_RE.search(cls))
    if not named and el.name != "nav" and el.find("nav") is None:
        return False
    return len([a for a in el.find_all("a") if a.get_text(strip=True)]) >= 2


def _is_header_bar(prev):
    """Является ли соседний СВЕРХУ блок частью шапки — логотип, баннер названия, топбар, узкая
    служебная полоса. Один ответ на этот вопрос для всех веток сборки шапки."""
    if prev is None or getattr(prev, "name", None) in (
            None, "script", "style", "link", "meta", "noscript",
            "header", "main", "footer", "nav"):
        return False
    ident = _sem_cls(prev)
    text = prev.get_text(" ", strip=True)
    named_header = bool(_SEM_HEADER_CLASS_RE.search(ident))
    logo_bar = bool(prev.find("img")) and len(text) <= 60
    # Узкая служебная полоса вплотную над шапкой (телефон, язык, вход, соцсети) — часть шапки.
    # По имени класса её не поймать: у firsttalk это id="nav-top", те же слова в обратном порядке.
    # Признак надёжнее — РАЗМЕР: полоска в пару ссылок и десяток символов не может быть контентом.
    thin_strip = len(text) <= 120 and len(prev.find_all("a")) <= 3 and prev.find(_HEADING_RE) is None
    if _SEM_HERO_RE.search(ident) and not named_header:
        return False   # геройская секция — контент, а не шапка
    if len(text) > 400:
        return False   # это уже контент
    return named_header or logo_bar or thin_strip


def _wrap_nav_in_header(soup, nav_block):
    """Обернуть nav-блок и стоящие ВЫШЕ полосы шапки в СВЕЖИЙ НЕЙТРАЛЬНЫЙ <header>, сохранив
    исходные блоки как СОСЕДЕЙ, а не вкладывая их друг в друга.

    Это замена «переименовать div#nav → header#nav и втянуть баннер ВНУТРЬ». Тот подход ломал
    вёрстку: у sanjhapunjab на `#nav` висит CSS синей полосы меню (фон, высота, ширина), и баннер
    `div#header`, засунутый внутрь, наследовал этот бокс. Нейтральная обёртка без id/class ничего
    не навязывает — каждый исходный блок сохраняет свой стиль 1-в-1, а порядок остаётся прежним.
    Возвращает созданный <header>.
    """
    bars = []
    prev = nav_block
    for _ in range(3):
        prev = prev.find_previous_sibling(lambda t: getattr(t, "name", None) is not None)
        if not _is_header_bar(prev):
            break
        bars.append(prev)
    header = soup.new_tag("header")
    anchor = bars[-1] if bars else nav_block   # верхний блок задаёт место вставки
    anchor.insert_before(header)
    for bar in reversed(bars):                 # сверху вниз, порядок сохраняется
        header.append(bar.extract())
    header.append(nav_block.extract())
    return header


def _ensure_inner_nav(header_el):
    """Гарантировать <nav> внутри шапки для списка ссылок меню, если его ещё нет."""
    if header_el.find("nav") is not None:
        return
    for sub in header_el.find_all(["div", "ul"], recursive=True):
        if len([a for a in sub.find_all("a") if a.get_text(strip=True)]) >= 2:
            sub.name = "nav"
            break


def _make_header(soup, ch):
    """Сделать блок `ch` шапкой страницы, сохранив вёрстку.

    Если СВЕРХУ стоит полоса шапки (логотип/баннер/топбар) — оборачиваем в нейтральный <header>
    как соседей, не трогая исходные блоки (их id/class несут CSS). Если полосы нет — просто
    переименовываем сам блок в <header>: вкладывать не во что, ломать нечего."""
    prev = ch.find_previous_sibling(lambda t: getattr(t, "name", None) is not None)
    if _is_header_bar(prev):
        header = _wrap_nav_in_header(soup, ch)
        _ensure_inner_nav(header)
    else:
        ch.name = "header"
        _ensure_inner_nav(ch)


def _absorb_header_bar(header_el):
    """Втянуть в СУЩЕСТВУЮЩИЙ нейтральный <header> соседние сверху полосы шапки.

    Применяется, только когда `header_el` — уже настоящий/свежесозданный <header> без своего
    навязчивого стиля (втягивание в такой безопасно). Для блока со своим id/class (styled box)
    вкладывать в него полосы НЕЛЬЗЯ — там используется `_wrap_nav_in_header`. Возвращает число
    втянутых полос.
    """
    if header_el is None:
        return 0
    taken = 0
    for _ in range(3):   # максимум три полосы: топбар + логотип + служебная строка
        prev = header_el.find_previous_sibling(lambda t: getattr(t, "name", None) is not None)
        if not _is_header_bar(prev):
            break
        header_el.insert(0, prev.extract())
        taken += 1
    return taken


def _promote_header(soup, root):
    """Deterministically make the site's primary top navigation the page <header> - a hero/intro
    band is NEVER the header. Scans the first few top-level blocks for a genuine nav signal (a
    <nav> tag, a navbar-classed container, or a block that CONTAINS a <nav>); turns that into, or
    wraps it in, <header>. No '2+ links' guessing, so a hero with CTA buttons isn't grabbed as the
    menu. Returns the old tag name (for the report) or None if there's no top nav to promote."""
    # Self-heal: a hero/intro/banner band that an earlier run mis-tagged as <header>/<footer> is
    # demoted back to <section>, so re-running the cleanup fixes a prior bad guess instead of
    # freezing it in (the check below would otherwise see the stray <header> and bail).
    for ch in root.find_all(recursive=False):
        if getattr(ch, "name", None) in ("header", "footer") and _SEM_HERO_RE.search(_sem_cls(ch)):
            ch.name = "section"
    _existing = root.find("header")
    if _existing is not None:
        # У готовой шапки полосы над ней тоже надо втянуть. Раньше функция выходила здесь сразу,
        # поэтому втягивание работало ТОЛЬКО на страницах, где шапку строили с нуля: у firsttalk
        # <header> был в исходнике, а <div id="nav-top"> так и оставался выше него — то есть
        # <header> не был первым элементом страницы.
        _absorb_header_bar(_existing)
        return None
    # Descend through a single generic wrapper that holds the whole page (old layouts wrap everything
    # in one <center>/<div>/<table>), so the nav that sits INSIDE it is reachable by the top-level
    # scan below - otherwise the wrapper is the only "child" and the menu is never seen.
    _WRAP = ("center", "div", "table", "tbody", "tr", "form", "section", "main")
    for _ in range(5):
        kids = [c for c in root.find_all(recursive=False)
                if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "br", "noscript")]
        if len(kids) == 1 and kids[0].name in _WRAP:
            root = kids[0]
            continue
        # The page wrapper often is NOT an only child: modern themes park an off-canvas mobile
        # menu and a dim overlay next to it (bikenfoot: div.site + h-offcanvas-panel + overlay).
        # Requiring a single child meant we never descended, and the real nav five levels down
        # was never even looked at. Descend into the one child that carries essentially the whole
        # page, as long as its siblings are comparatively tiny.
        if 1 < len(kids) <= 5:
            # If a sibling already IS the header bar, we are at the right level - descending into
            # the (much heavier) content block would step straight PAST it. bikenfoot's wrapper
            # holds skip-link + page-header + page-content + page-footer, and page-content alone
            # outweighed the rest 3:1, so the header sitting right there was skipped every time.
            if any(_is_header_like(k) for k in kids):
                break
            weight = lambda k: len(k.get_text(" ", strip=True)) + 40 * len(k.find_all("a"))
            ranked = sorted(((weight(k), k) for k in kids), key=lambda x: -x[0])
            biggest, others = ranked[0], sum(w for w, _ in ranked[1:])
            # Never descend INTO the menu itself: links weigh heavily, so a nav bar easily outweighs
            # the article next to it, and stepping inside it leaves only <a> tags to scan - the nav
            # is then invisible to both the loop below and the deep fallback.
            if (biggest[1].name in _WRAP and biggest[0] >= 3 * max(others, 1)
                    and not _looks_like_nav_block(biggest[1])
                    and not _SEM_NAVBAR_RE.search(_sem_cls(biggest[1]))):
                root = biggest[1]
                continue
        break
    children = [c for c in root.find_all(recursive=False) if getattr(c, "name", None)]
    # A block the model already labelled as the header wins outright - that label is a judgement
    # about meaning, which is exactly what the scan below can only approximate.
    # The model sometimes labels two blocks "header" (a leftover empty wrapper plus the real bar),
    # so take the one that actually carries navigation - sanjhapunjab ended up with an EMPTY
    # <header> and its real 40-link menu left as a plain <div>.
    planned_headers = [c for c in children if _role_of(c) == "header"]
    planned_headers.sort(key=lambda c: -len([a for a in c.find_all("a") if a.get_text(strip=True)]))
    for ch in planned_headers[:1]:
        if can_be_header(ch, root):
            old_name = ch.name
            _make_header(soup, ch)
            return old_name
    for ch in children[:6]:
        cls = _sem_cls(ch)
        if _SEM_HERO_RE.search(cls):
            continue  # a hero/intro/banner is content - skip past it, keep looking for the nav
        # NOTE: do NOT bail just because an earlier block has a heading - the menu often sits right
        # AFTER a logo/title bar (which carries the site's <h1>), e.g. <div>logo+h1</div><div>nav</div>.
        # Only a link-dominated block (nav tag / navbar class / contains <nav> / _looks_like_nav_block)
        # is taken as the header; ordinary content blocks are skipped, not treated as a stop signal.
        is_nav = (ch.name == "nav" or bool(_SEM_NAVBAR_RE.search(cls))
                  or ch.find("nav") is not None or _looks_like_nav_block(ch)
                  or _is_header_like(ch))
        if not is_nav or not can_be_header(ch, root):
            continue
        if ch.name == "nav":
            # a bare top <nav> -> wrap it (plus an immediately-preceding logo-only sibling) in
            # a fresh <header>, so the menu bar sits inside the page header where it belongs.
            header = soup.new_tag("header")
            ch.insert_before(header)
            header.append(ch.extract())
            # Полосы шапки, стоящие выше (логотип, топбар), втягиваются тем же общим правилом,
            # что и в остальных ветках — раньше здесь была своя, более узкая проверка.
            _absorb_header_bar(header)
            return "nav"
        # a navbar-classed container (or any block wrapping a <nav>) -> make it the <header>.
        # _make_header wraps in a neutral <header> when a logo/banner bar sits above (keeping the
        # styled block's CSS intact), else renames in place.
        old = ch.name
        _make_header(soup, ch)
        return old

    # Nothing nav-like among the top-level blocks. On old table/<center> layouts the menu is buried
    # several wrappers deep and carries NO class at all (sylhetcitycorporation: a bare <div> with 27
    # links at depth 4-8), so the scan above can't reach it and the page shipped with no <header>.
    # Fall back to the SHALLOWEST link-dominated block anywhere on the page - shallowest because the
    # menu sits near the top of the tree, while link lists deep inside content do not.
    # Rank by LINK COUNT first, depth only as a tie-break: the site menu is the richest link list
    # on the page. Going by depth alone picked whichever small link group happened to sit highest -
    # on sylhetcitycorporation that was a 3-link sub-list instead of the real 27-link menu.
    best = None
    for el in root.find_all(["nav", "div", "ul", "center", "table", "td", "tr", "p", "section"]):
        if el.find_parent("footer") is not None or el.find_parent("header") is not None:
            continue
        if not _looks_like_nav_block(el) or not can_be_header(el, root):
            continue
        links = len([a for a in el.find_all("a") if a.get_text(strip=True)])
        depth = len(list(el.parents))
        if best is None or (-links, depth) < (-best[0], best[1]):
            best = (links, depth, el)
    if best is not None:
        ch = best[2]
        header = soup.new_tag("header")
        ch.insert_before(header)
        header.append(ch.extract())
        if header.find("nav") is None:
            ch.name = "nav" if ch.name in ("div", "ul", "center") else ch.name
        return "deep-nav"
    return None


_SEM_FOOTER_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:(?:page|site|main|global|bottom)[\s_-]?)?"
                                  r"(?:footer|colophon|bottom[\s_-]?bar)\d*(?:[\s_-]|$)", re.I)
_COPYRIGHT_RE = re.compile(r"©|&copy;|copyright|all rights reserved", re.I)
# Drawers, burger panels and dim overlays sit last in the body and often end with a copyright line,
# so without this they get mistaken for the page footer.
_MOBILE_PANEL_RE = re.compile(r"offcanvas|off-canvas|offscreen|drawer|burger|hamburger|mobile-menu|"
                              r"overlay|modal|popup|sidenav|side-nav", re.I)


def _promote_footer(soup, root):
    """Deterministically mark the page footer - the counterpart of _promote_header, which for a
    long time had no equivalent: only the AI pass could ever produce a <footer>, so WITHOUT a key a
    site whose footer bar is a plain `div.page-footer` (bikenfoot, sylhet) shipped with none at all
    and the header/main/footer guarantee was only two-thirds true.

    Two signals, both checked on the LAST few top-level blocks only, so nothing mid-page is grabbed:
    a footer-ish class, or a closing block that carries a copyright notice."""
    if root.find("footer") is not None:
        return None

    # Search the whole subtree, not just direct children: on an old <center>/<table> layout the
    # labelled footer sits several wrappers down, and looking only one level deep found nothing
    # (sylhetcitycorporation shipped with no footer although the model had labelled one).
    body_all = root.find_parent("body") or root
    body_len = len(body_all.get_text(" ", strip=True)) or 1
    for ch in root.find_all(True):
        if _role_of(ch) != "footer":
            continue
        txt = ch.get_text(" ", strip=True)
        if not txt:
            continue
        # A footer is a CLOSING BAR, never the page. The model labelled a block holding almost the
        # whole of sylhetcitycorporation as "footer"; applying that verbatim turned the entire site
        # into a <footer> and left <main> with nothing. Size is the check the label cannot give us.
        if len(txt) > body_len * 0.4:
            continue
        old_name = ch.name
        ch.name = "footer"
        return old_name

    def _off_limits(el):
        # An off-canvas drawer / mobile menu / overlay is NOT the footer, even though it sits last
        # in the body and often ends with a copyright line. bikenfoot's mobile panel was grabbed as
        # the footer while the real div.page-footer, nested inside the page wrapper, was ignored.
        if _SEM_HERO_RE.search(_sem_cls(el)) or el.name in ("header", "main", "nav"):
            return True
        for anc in [el, *el.parents]:
            if getattr(anc, "name", None) is None:
                break
            if _MOBILE_PANEL_RE.search(_sem_cls(anc)):
                return True
            if anc.name in ("header", "nav"):
                return True
        return False

    # Class-based, searched over the WHOLE subtree (not just direct children): the footer bar is
    # usually nested inside the page wrapper, exactly like the header is.
    cands = [el for el in root.find_all(["div", "section", "center", "table"])
             if _SEM_FOOTER_CLASS_RE.search(_sem_cls(el)) and not _off_limits(el)]
    if cands:
        outer = [c for c in cands if not any(c is not o and c in o.descendants for o in cands)]
        ch = (outer or cands)[-1]
        old = ch.name
        ch.name = "footer"
        return old

    children = [c for c in root.find_all(recursive=False)
                if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "noscript")]
    tail = children[-4:]
    # No footer class anywhere: take the closing block that states a copyright, as long as it is a
    # small closing bar and not a whole content column that happens to end with one.
    for ch in reversed(tail):
        if _off_limits(ch):
            continue
        text = ch.get_text(" ", strip=True)
        if _COPYRIGHT_RE.search(text) and len(text) <= 600 and ch.find(("h1", "h2")) is None:
            old = ch.name
            ch.name = "footer"
            return old
    return None


def _visible_text_for_meta(soup):
    """A concise visible-text sample of the page's main content for meta generation: headings and
    paragraphs from <body>, minus nav/footer/script/style noise, capped so the prompt stays cheap."""
    body = soup.find("body") or soup
    parts, total = [], 0
    for el in body.find_all(["h1", "h2", "h3", "p", "li"]):
        if el.find_parent(["nav", "footer", "script", "style"]):
            continue
        t = el.get_text(" ", strip=True)
        if not t:
            continue
        parts.append(t)
        total += len(t)
        if total > 5000:
            break
    return "\n".join(parts)


def apply_semantic_meta(soup, site_domain, report):
    """Fill/refresh <title> and <meta name="description"> from the page content via Haiku. Runs
    BEFORE _reorder_head_seo so the new tags get placed in the canonical head order. Best-effort:
    a no-op without the semantics module/key, or when the LLM call fails."""
    sem = _semantics()
    if sem is None or not sem.available():
        return
    head = soup.find("head")
    if head is None:
        return
    existing_title = soup.title.get_text(strip=True) if soup.title else None
    meta = sem.generate_meta(
        _visible_text_for_meta(soup),
        existing_title=existing_title,
        domain=_bare_domain(site_domain) or site_domain,
    )
    if not meta:
        return
    title_el = soup.find("title")
    if title_el is None:
        title_el = soup.new_tag("title")
        head.append(title_el)
    title_el.string = meta["title"]
    report.semantic_title = meta["title"]
    desc_el = soup.find("meta", attrs={"name": re.compile(r"^description$", re.I)})
    if desc_el is None:
        desc_el = soup.new_tag("meta")
        desc_el["name"] = "description"
        head.append(desc_el)
    desc_el["content"] = meta["description"]
    report.semantic_description = meta["description"]


_HEADING_RE = re.compile(r"^h[1-6]$")


def drop_empty_headings(soup, report):
    """Remove headings that carry nothing. An `<h1></h1>` left behind by a stripped widget is an
    invisible element that still counts as a heading: sanjhapunjab shipped three <h1>, one of them
    empty, which is what made its outline look broken. A heading holding only an image stays - that
    is a logo heading, real content."""
    dropped = demoted = 0
    for h in soup.find_all(_HEADING_RE):
        if h.get_text(strip=True):
            continue
        if h.find(["img", "svg", "picture", "video"]) is not None:
            # A heading holding ONLY a logo image is a logo, not a heading - themes wrap the site
            # logo in <h1> and that made sanjhapunjab ship three <h1> on one page. Keep the element
            # and its classes (so the site's CSS still styles the logo) but stop it counting as a
            # heading, which is what breaks the h1->h2->h3 outline.
            h.name = "div"
            demoted += 1
            continue
        h.decompose()
        dropped += 1
    if dropped or demoted:
        report.headings_normalized.append(
            f"пустых заголовков удалено: {dropped}, лого-заголовков разжаловано в div: {demoted}")
    return dropped + demoted


def strip_dead_css_urls(html_path, report):
    """Drop url(...) references from local CSS when the file simply isn't there.

    Recovery does its best, but whatever it can't fetch stays in the stylesheet as a live request
    to a file that does not exist - the browser logs ERR_FILE_NOT_FOUND for every one of them
    (sanjhapunjab: dropdown.css asking for a bg-menu.jpg that was never archived). A background
    that cannot load is not worth a console error, so the declaration goes."""
    if html_path is None:
        return 0
    removed = 0
    for css in html_path.parent.rglob("*.css"):
        try:
            text = css.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        def _is_dead(ref):
            ref = ref.strip().strip("'\"").strip()
            if not ref or ref.startswith(("data:", "http://", "https://", "//", "#")):
                return False
            target = (css.parent / ref.split("?")[0].split("#")[0]).resolve()
            return not target.exists()

        # Режем ТОЛЬКО мёртвый url()-токен, а не всё объявление. `background: #285b8b url(dead)
        # no-repeat` должен потерять только картинку и сохранить цвет и позицию — иначе пропадает
        # синий фон, и блок «худеет» (аккордеон sanjhapunjab). Пустое объявление, оставшееся после
        # выреза (`background-image:` без значения), убираем следующим проходом, чтобы не плодить
        # `prop:;`-огрызки.
        cnt = [0]

        def _sub(m):
            if _is_dead(m.group(1)):
                cnt[0] += 1
                return ""
            return m.group(0)

        out = re.sub(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", _sub, text, flags=re.I)
        # Объявление, чьё значение стало ПУСТЫМ после выреза url (`background-image: ;`), убираем.
        # ТОЛЬКО пустое — не `none`: `display:none` это осмысленное значение, его трогать нельзя.
        out = re.sub(r"[a-zA-Z-]+\s*:\s*;", "", out)          # пустое значение перед ;
        out = re.sub(r"[a-zA-Z-]+\s*:\s*(?=\})", "", out)     # пустое значение перед }
        changed = cnt[0] > 0 and out != text
        removed += cnt[0]
        if changed:
            try:
                css.write_text(out, encoding="utf-8")
            except OSError:
                pass
    if removed:
        report.removed_links_css.append(f"мёртвых url() в CSS вычищено: {removed}")
    return removed


_SEM_SECTIONISH_RE = re.compile(
    r"(?:^|[\s_-])(?:section|block|module|panel|band|row-section|content-section|"
    r"post|entry|article|hentry)(?:[\s_-]|$)", re.I)
_ITEM_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:post-item|news-item|menu-post|list-item|card|entry|product|item|tile|thumb|"
    r"teaser|caption|excerpt|preview|article-item|blog-item|grid-item)", re.I)
# NB: сюда НЕЛЬЗЯ добавлять col-* — это бутстраповские колонки, они есть у всего подряд,
# включая обёртки заголовков разделов.
_SECTION_TITLE_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:section-title|section-head|section-header|block-title|widget-head|"
    r"category-title|module-title|heading-block)", re.I)


_HEADING_CACHE_ATTR = "_wb_heading_classes"


def classify_headings(soup):
    """Разделить заголовки на ЗАГОЛОВКИ РАЗДЕЛОВ и ЗАГОЛОВКИ КАРТОЧЕК.

    РЕЗУЛЬТАТ КЭШИРУЕТСЯ НА ДОКУМЕНТ. Функция вызывается дважды — из расстановки уровней заголовков
    и из сборки секций, — а внутри неё работает агент, который НЕДЕТЕРМИНИРОВАН. Два вызова давали
    РАЗНЫЕ ответы: один и тот же заголовок статьи оказывался «карточкой» при выставлении уровней и
    «разделом» при расстановке якорей. Отсюда у sanjhapunjab одновременно выходило ноль <h2>,
    карта из 11 разделов и меню из одного пункта.

    На листинге (новости, каталог, блог) заголовок карточки и заголовок раздела — оба <h2>, но это
    совершенно разные вещи: «Health» открывает раздел, а «Northborne Partners Advises…» это одна из
    двадцати статей внутри него. Раньше скрипт их не различал, поэтому карточки оставались h2
    (вместо h3) и якорь цеплялся к первой попавшейся карточке, а не к разделу.

    Главный признак — ПОВТОРЯЕМОСТЬ, а не имя класса: если полсотни заголовков имеют одинаковую
    цепочку родителей, это перечисление однотипных карточек. Работает на любом сайте и языке,
    даже когда классы названы как угодно. Имя класса — дополнительный сигнал.

    Возвращает (section_headings, card_headings).
    """
    from collections import Counter
    heads = soup.find_all(_HEADING_RE)
    if not heads:
        return [], []
    # Кэш живёт НА САМОМ объекте документа, а не в словаре по id(soup). Словарь по id() был прямой
    # причиной «ноль <h2> при 11 найденных разделах»: id() это адрес в памяти, и CPython
    # ПЕРЕИСПОЛЬЗУЕТ его после сборки мусора. Чистильщик разбирает документы пачкой, новый soup
    # садится на адрес освобождённого, проверка по числу заголовков совпадает — и функция отдаёт
    # теги ЧУЖОЙ, уже мёртвой страницы. Дальше вызывающий код сверяет их через id(), не находит
    # ни одного совпадения в живом документе, и ни один заголовок не признаётся карточкой.
    # Атрибут на объекте умирает вместе с ним, так что перепутать документы больше нечем.
    _hit = getattr(soup, _HEADING_CACHE_ATTR, None)
    if _hit is not None and _hit[0] == len(heads):
        return list(_hit[1]), list(_hit[2])

    def sig(h):
        out = []
        for anc in list(h.parents)[:3]:
            cls = " ".join(anc.get("class") or [])[:40] if hasattr(anc, "get") else ""
            out.append(f"{getattr(anc, 'name', '')}.{cls}")
        return " < ".join(out)

    counts = Counter(sig(h) for h in heads)

    # ПРАВИЛО ВЛАДЕЛЬЦА (2026-07-20), строго:
    #     div/section > h2 (заголовок раздела, СЮДА якорь) > h3 (карточки внутри)
    # То есть в каждом блоке-разделе ПЕРВЫЙ заголовок — это заголовок раздела, а все следующие
    # внутри того же блока — карточки. Признак «первый в своём блоке» работает даже там, где нет
    # ни говорящих классов, ни повторяемости: на новостной странице «Health» идёт первым в своём
    # <section>, а двадцать статей под ним — следующими.
    _blocks = {}
    for h in heads:
        holder = None
        for anc in h.parents:
            if getattr(anc, "name", None) in ("section", "article", "main", "body", None):
                holder = anc
                break
            cls = " ".join(anc.get("class") or []) if hasattr(anc, "get") else ""
            if _SEM_SECTIONISH_RE.search(cls):
                holder = anc
                break
        _blocks.setdefault(id(holder) if holder is not None else 0, []).append(h)
    _first_in_block = {id(v[0]) for v in _blocks.values() if v}

    sections, cards = [], []
    for h in heads:
        chain = [a for a in list(h.parents)[:4] if hasattr(a, "get")]
        chain_cls = " ".join(" ".join(a.get("class") or []) for a in chain)
        in_section_title = bool(_SECTION_TITLE_CLASS_RE.search(chain_cls))
        in_item = bool(_ITEM_CLASS_RE.search(chain_cls))
        repeated = counts[sig(h)] >= 3
        # Явный маркер раздела ПОБЕЖДАЕТ повторяемость. Заголовки разделов тоже повторяются —
        # их на странице восемь, — и первая версия из-за этого записала все восемь в карточки.
        first_here = id(h) in _first_in_block
        if in_section_title:
            sections.append(h)
        elif in_item or repeated:
            cards.append(h)          # карточка листинга — никогда не якорь
        elif first_here:
            sections.append(h)       # первый заголовок своего блока = заголовок раздела
        else:
            cards.append(h)          # всё, что идёт следом внутри того же блока

    # Страница БЕЗ заголовков разделов — это блог: список статей и виджеты, никаких «Health»
    # и «Lifestyle» над ними. Тогда разделами становятся сами заголовки статей: якорю больше
    # не к чему цепляться, а «нет секций» означает выпотрошенное меню.
    # (sanjhapunjab: девять <h2> статей в div.post и ни одного категорийного заголовка.)
    # Спросить агента — он видит СМЫСЛ: «Health» это рубрика, «Northborne Partners Advises…» это
    # материал. Эвристика этого не отличает и уже дала несколько регрессов подряд.
    try:
        sys.path.insert(0, str(Path(__file__).parent / "site_studio"))
        import semantics
        if semantics.available():
            _txt_heads = [h for h in heads if h.get_text(strip=True)][:30]
            roles = semantics.classify_heading_roles([
                {"i": i, "tag": h.name, "text": h.get_text(" ", strip=True),
                 "cls": " ".join(h.get("class") or []),
                 "parent_cls": " ".join((h.parent.get("class") or []) if h.parent else []),
                 "siblings": len(h.parent.find_all(h.name)) if h.parent else 0}
                for i, h in enumerate(_txt_heads)])
            if roles:
                ai_sec = {id(_txt_heads[i]) for i, v in roles.items() if v == "section"}
                ai_card = {id(_txt_heads[i]) for i, v in roles.items() if v == "card"}
                if ai_sec:  # пустой ответ игнорируем - он бесполезен, а не информативен
                    # Членство считается по ТОЖДЕСТВУ. Оператор `in` у bs4 сравнивает РАЗМЕТКУ, а не
                    # объект: два разных заголовка с одинаковой вёрсткой считаются одним и тем же,
                    # и заголовок молча попадает не в тот список.
                    _sec_ids = {id(h) for h in sections}
                    sections = [h for h in heads if id(h) in ai_sec or
                                (id(h) not in ai_card and id(h) in _sec_ids)]
                    _keep = {id(h) for h in sections}
                    cards = [h for h in heads if id(h) not in _keep]
    except Exception:  # noqa: BLE001 - без ключа и при сбое работает детерминированный путь
        pass

    # Виджеты сайдбара (Search, Tags, Recent Posts, Archives) разделами не считаются — их всё равно
    # отсеет следующий шаг. Без этой проверки счётчик разделов был завышен (24 вместо 2), запасное
    # правило «разделов мало — повысить заголовки статей» не срабатывало, и на блоге не оставалось
    # ни одной цели для якоря.
    _WIDGETISH = re.compile(r"(?:^|[\s_-])(?:widget|sidebar|side-bar|secondary|aside|search|"
                            r"archives?|categor|recent|tags?|calendar|meta|blogroll|subscribe|"
                            r"social|share|advert|banner|promo)", re.I)

    def _widgetish(h):
        for anc in [h, *list(h.parents)[:5]]:
            if not hasattr(anc, "get"):
                break
            ident = " ".join(anc.get("class") or []) + " " + (anc.get("id") or "")
            if re.search(r"(?:^|[\s_-])section(?:[\s_-]|$)", ident, re.I):
                return False
            if _WIDGETISH.search(ident) or getattr(anc, "name", "") == "aside":
                return True
        return False

    # Виджет убирается ТОЛЬКО из разделов. Если убрать его и из карточек, он не попадёт ни в один
    # список — и тогда его заголовок не понизится до h3: у firsttalk так стало 42 <h2> вместо 9.
    _wid = [h for h in sections if _widgetish(h)]
    sections = [h for h in sections if not _widgetish(h)]
    cards = cards + _wid

    _real = [h for h in sections if h.get_text(strip=True)]
    # Порог именно 3, а не 2: у sanjhapunjab нашлись ровно два <h1> (заголовки статей), условие
    # «меньше двух» не срабатывало, и девять <h2> статей так и оставались карточками — цеплять
    # якоря было не к чему. Два раздела на странице это ещё не навигация.
    if len(_real) < 3 and cards:
        promoted = [h for h in cards if h.get_text(strip=True)]
        # берём самый крупный уровень среди карточек - это и есть заголовки статей,
        # а не подписи внутри них
        if promoted:
            # Самый МНОГОЧИСЛЕННЫЙ уровень — это и есть заголовки статей. Брать самый верхний
            # неверно: у sanjhapunjab два <h1> (два поста наверху) и девять <h2> (остальные), и
            # верхний уровень дал бы только две цели вместо одиннадцати.
            by_lvl = Counter(int(h.name[1]) for h in promoted)
            top = max(by_lvl, key=lambda lv: (by_lvl[lv], -lv))
            moved = [h for h in promoted if int(h.name[1]) == top]
            if len(moved) >= 2:
                sections = sections + moved
                _moved_ids = {id(h) for h in moved}   # by identity: `in` у bs4 сравнивает разметку
                cards = [h for h in cards if id(h) not in _moved_ids]
    try:
        setattr(soup, _HEADING_CACHE_ATTR, (len(heads), list(sections), list(cards)))
    except Exception:  # noqa: BLE001 - кэш это ускорение, а не гарантия
        pass
    return sections, cards


def normalize_heading_levels(soup, report):
    """Make the document's heading outline have NO skipped levels: after an h2 the next-deeper
    heading is h3, never h4. Deterministic, no AI, changes only the tag LEVEL (never the text).

    Walk headings in document order keeping a stack of the ancestors' ORIGINAL levels; a heading's
    output level is its depth in that tree, offset from the first heading's own level so the top of
    the page keeps its level (a page that starts at h2 stays h2, its children become h3, h4, ...).
    Siblings share a level; going shallower pops back up. Capped at h6."""
    body = soup.find("body") or soup
    headings = body.find_all(_HEADING_RE)
    if not headings:
        return
    # The outline must start at <h1> and step down one level at a time: h1 -> h2 -> h3. Keeping the
    # first heading's ORIGINAL level (the old behaviour) meant a page whose top heading was an <h3>
    # shipped with no <h1> at all - every site in the set had none. Exactly one <h1>: the first
    # heading is the page title; everything below it starts at <h2>, so a listing page of thirty
    # article titles does not become thirty <h1>.
    # Заголовки карточек в листинге обязаны быть НА УРОВЕНЬ НИЖЕ заголовка своего раздела.
    # На новостной странице «Health» это раздел (h2), а двадцать статей под ним — карточки, и они
    # тоже приходили как h2. Для читателя и для поисковика это выглядит так, будто на странице
    # двадцать равноправных разделов, а не один с двадцатью материалами.
    _sections_h, _cards_h = classify_headings(soup)
    _card_ids = {id(x) for x in _cards_h}

    stack = []
    # Карточка обязана быть на уровень ниже СВОЕГО раздела, а не ниже жёсткой константы. Раньше
    # стоял пол `max(3, ...)`, и на блоге, где единственный раздел — это <h1> заголовка страницы,
    # все карточки падали на h3 и перепрыгивали h2 (sanjhapunjab: h1 + 34×h3, ни одного h2).
    # Теперь запоминаем уровень последнего РАЗДЕЛА и ставим карточку ровно под него.
    last_section_out = 1
    for idx, h in enumerate(headings):
        lvl = int(h.name[1])
        while stack and stack[-1] >= lvl:
            stack.pop()
        stack.append(lvl)
        out = 1 if idx == 0 else min(6, max(2, len(stack)))
        # Первый заголовок страницы — это её заголовок, он ВСЕГДА h1 и никогда не понижается как
        # карточка. Без этой оговорки листинг, начинающийся прямо с заголовка статьи, уезжал на h2
        # и страница оставалась вообще без h1 (firsttalk: h2×35 + h3×106, ни одного h1).
        if idx == 0:
            last_section_out = 1
        elif id(h) in _card_ids:
            out = min(6, max(2, last_section_out + 1))
        else:
            last_section_out = out
        new_name = f"h{out}"
        if h.name != new_name:
            report.headings_normalized.append(f"{h.name} -> {new_name}")
            h.name = new_name


def _role_candidates(soup, limit=12):
    """Blocks worth considering for the header/footer roles, with just enough signal for a model
    to judge them: class, link count and a text sample."""
    body = soup.find("body")
    if body is None:
        return []
    out, seen = [], set()
    pool = [el for el in body.find_all(["header", "footer", "nav", "div", "section", "center"])
            if el.find_parent(["header", "footer"]) is None]
    for el in pool:
        links = len([a for a in el.find_all("a") if a.get_text(strip=True)])
        text = el.get_text(" ", strip=True)
        if links < 2 and not _COPYRIGHT_RE.search(text[:400]):
            continue
        if len(text) > 4000:
            continue  # a whole page column, not a bar
        key = (text[:80], links)
        if key in seen:
            continue
        seen.add(key)
        out.append({"el": el, "tag": el.name, "cls": _sem_cls(el), "links": links, "text": text})
    out.sort(key=lambda c: len(c["text"]))
    return out[:limit]


def verify_landmark_roles(soup, report):
    """Let the model check WHICH block got the header/footer role - the one question rules keep
    getting wrong (an off-canvas drawer ending in a copyright line reads exactly like a footer;
    a sidebar of 'Archives / Select month' links reads exactly like a nav).

    Strictly a corrective layer on top of the deterministic pass: it can only MOVE a role to a
    better block or drop an obviously wrong one. Whether the page ends up with a header at all
    stays a deterministic guarantee (site_edit rebuilds one if this drops it), so behaviour with
    no API key is unchanged."""
    try:
        sys.path.insert(0, str(Path(__file__).parent / "site_studio"))
        import semantics
    except Exception:  # noqa: BLE001
        return
    if not semantics.available():
        return
    cands = _role_candidates(soup)
    if not cands:
        return
    cur_hdr, cur_ftr = soup.find("header"), soup.find("footer")
    picks = {"header": None, "footer": None}
    payload = []
    for i, c in enumerate(cands):
        if c["el"] is cur_hdr:
            picks["header"] = i
        if c["el"] is cur_ftr:
            picks["footer"] = i
        payload.append({"i": i, "tag": c["tag"], "cls": c["cls"],
                        "links": c["links"], "text": c["text"][:150]})
    try:
        verdict = semantics.verify_landmarks(picks, payload)
    except Exception:  # noqa: BLE001 - the model is an improvement, never a dependency
        return
    if not verdict:
        return
    # NOT "main": candidates here are capped at ~4000 chars so the model can read them, which means
    # the only <main> this pass could ever pick is a small one - it kept overwriting a correctly
    # assembled <main> with a 3% scrap. The content region comes from assemble_main_from_plan.
    for role, cur in (("header", cur_hdr), ("footer", cur_ftr)):
        want = verdict.get(role)
        chosen = cands[want]["el"] if want is not None and 0 <= want < len(cands) else None
        if chosen is cur:
            continue
        # MOVE only, never merely drop. The model returning null for a role it can't identify must
        # not cost the page a landmark it already had - danvanhaiphong went from header/main/footer
        # to no footer that way, because nothing downstream rebuilds a footer.
        if chosen is None:
            continue
        if chosen.name not in ("div", "section", "center", "nav"):
            continue
        # Same plausibility bar the deterministic paths use. Without it this pass could install a
        # 2-link breadcrumb as <header> while demoting the real 40-link menu, or turn a mid-page
        # block into <footer> - and everything inside a header/footer stops counting as content.
        chosen_links = len([a for a in chosen.find_all("a") if a.get_text(strip=True)])
        chosen_text = chosen.get_text(" ", strip=True)
        body_el = soup.find("body")
        body_len = len(body_el.get_text(" ", strip=True)) if body_el else 1
        if cur is not None and (cur is chosen or chosen in cur.parents or cur in chosen.descendants):
            continue  # never hand the role to an ancestor/descendant of the current holder
        if role == "header":
            cur_links = len([a for a in cur.find_all("a") if a.get_text(strip=True)]) if cur is not None else 0
            if not can_be_header(chosen) or chosen_links < cur_links:
                continue
        if role == "footer":
            if not chosen_text or len(chosen_text) > max(body_len * 0.4, 1):
                continue
            if _MOBILE_PANEL_RE.search(_sem_cls(chosen)):
                continue
        if cur is not None and cur.name == role:
            cur.name = "div"
            report.semantic_tags_applied.append(f"агент: снял ошибочный <{role}>")
        chosen.name = role
        report.semantic_tags_applied.append(f'агент: <{role}> -> class="{_clip_cls(chosen)}"')


def _clip_cls(el):
    return (" ".join(el.get("class") or []) or el.name)[:40]


CONTRACT_RULES = """Контракт на результат очистки (проверяется на КАЖДОЙ странице):
  1. ровно один <header>, один <main>, один <footer>
  2. внутри <main> нет header/footer/nav-лендмарков
  3. <main> держит >=25% текста страницы
  4. ровно один <h1>, уровни заголовков без скачков
  5. меню не выпотрошено (если в исходнике было >=3 пункта — осталось >=2)
  6. не осталось вебархивных ссылок и абсолютного <base href>
  7. служебные метки разметки не утекли в HTML"""


def verify_output_contract(soup, report, original_html=None):
    """Проверить готовую страницу по списку правил, одинаковому для ВСЕХ сайтов.

    Почему это важнее регресс-набора: тесты покрывают те сайты, что у меня есть, а пользователь
    берёт новые домены пачками. Правило, зашитое в скрипт, ловит нарушение на архиве, которого я
    никогда не видел, - и делает это в момент очистки, а не через неделю жалобой «опять криво».

    Каждый пункт здесь стоит потому, что уже случался: два <header> (второй появлялся посреди
    статьи), <main> с 3% текста, меню, выпотрошенное до одного пункта, страница без единого <h1>,
    служебные data-wb-role в готовом HTML."""
    bad = []
    body = soup.find("body")
    if body is None:
        # No <body> at all: a <frameset> document, or a capture that saved only the shell. Returning
        # an empty list here declared such a page FULLY COMPLIANT while it had zero landmarks and
        # zero content - the contract's worst possible answer.
        if soup.find("frameset") is not None:
            bad.append("<frameset>: контент в отдельных фреймах, страница не собрана — нужен ручной разбор")
        else:
            bad.append("нет <body> — страница пустая или это не HTML-документ")
        report.audit_warnings = list(getattr(report, "audit_warnings", [])) + [
            "НАРУШЕН КОНТРАКТ: " + b for b in bad]
        return bad
    total = len(body.get_text(" ", strip=True)) or 1

    # Count only PAGE-LEVEL landmarks. WordPress themes put <header class="entry-header"> and
    # <footer class="entry-footer"> inside every <article>; counting those made the contract shout
    # on every blog capture. A contract that cries wolf gets ignored, and then it hides the real
    # violations - which is worse than having no contract at all.
    def _page_level(tag):
        return [el for el in soup.find_all(tag)
                if el.find_parent(["article", "aside"]) is None
                and el.find_parent(tag) is None]

    hdrs, mains, ftrs = _page_level("header"), _page_level("main"), _page_level("footer")
    if len(hdrs) != 1:
        bad.append(f"<header> должен быть ровно один, найдено {len(hdrs)}")
    if len(mains) != 1:
        bad.append(f"<main> должен быть ровно один, найдено {len(mains)}")
    if len(ftrs) != 1:
        bad.append(f"<footer> должен быть ровно один, найдено {len(ftrs)}")

    if mains:
        m = mains[0]
        inner = m.find(["header", "footer"])
        if inner is not None:
            bad.append(f"<{inner.name}> лежит ВНУТРИ <main> (контент станет невидим для меню)")
        share = len(m.get_text(" ", strip=True)) / total
        if share < 0.25:
            bad.append(f"<main> держит всего {share:.0%} текста — выбран не тот блок")
        # Секции снаружи <main>. Этот пункт добавлен ПОСЛЕ РЕЦИДИВА: дефект уже чинили, он вернулся
        # и прошёл незамеченным, потому что контракт проверял «лендмарк ВНУТРИ main», но не обратное.
        # Секция снаружи невидима для привязки меню - её якорь ведёт в никуда, и пункты тихо теряются.
        # ВАЖНО: сравнивать по РОДИТЕЛЮ, а не через `in m.descendants`. В bs4 оператор `in`
        # сравнивает теги по РАЗМЕТКЕ, а не по объекту, поэтому две одинаковые секции считаются
        # одной и той же - проверка молча пропускала дефект, ради которого её и писали.
        # Тот же счёт, что и у чинилки: расхождение этих двух правил и было дефектом.
        outside = sections_outside_main(soup)
        if outside:
            bad.append(f"секций СНАРУЖИ <main>: {len(outside)} — они невидимы для привязки меню")
        # A page whose whole body is a few words passes any RATIO check trivially: 25% of nothing is
        # still nothing. An iframe shell or a capture that lost its content looks compliant without
        # an absolute floor.
        if total < 200 and soup.find(["iframe", "frame", "frameset"]) is not None:
            bad.append("на странице почти нет текста, контент остался во фрейме/iframe")

    lv = [int(h.name[1]) for h in soup.find_all(_HEADING_RE)]
    if lv:
        if lv.count(1) != 1:
            bad.append(f"<h1> должен быть ровно один, найдено {lv.count(1)}")
        skips = [(a, b) for a, b in zip(lv, lv[1:]) if b > a + 1]
        if skips:
            bad.append(f"скачки уровней заголовков: {skips[:3]}")

    if hdrs:
        menu_now = len([a for a in hdrs[0].find_all("a") if a.get_text(strip=True)])
        if original_html:
            try:
                before = BeautifulSoup(original_html, PARSER)
                menu_was = 0
                for el in before.find_all(["nav", "ul", "div"]):
                    ident = " ".join(el.get("class") or []) + " " + (el.get("id") or "")
                    if re.search(r"nav|menu", ident, re.I) and el.find_parent("footer") is None:
                        menu_was = max(menu_was, len([a for a in el.find_all("a") if a.get_text(strip=True)]))
                if menu_was >= 3 and menu_now < 2:
                    bad.append(f"меню выпотрошено: было {menu_was} пунктов, осталось {menu_now}")
            except Exception:  # noqa: BLE001
                pass

    html_now = str(soup)
    if re.search(r"/web/\d{8,}", html_now):
        bad.append("остались вебархивные ссылки /web/<timestamp>/")
    if re.search(r"<base[^>]*href=[\"']\s*(?:https?:)?//", html_now, re.I):
        bad.append("остался <base href> с абсолютным адресом (убьёт все стили)")
    if _ROLE_ATTR in html_now:
        bad.append(f"служебные метки {_ROLE_ATTR} утекли в готовый HTML")
    # NB: when this runs inside clean_html_file the marks have already been stripped from the SAME
    # soup object, so the check above can only ever fire when the contract is handed a document
    # re-read from disk - which is exactly how site_edit calls it at the end of auto_link_menu.

    if bad:
        report.audit_warnings = list(getattr(report, "audit_warnings", [])) + [
            "НАРУШЕН КОНТРАКТ: " + b for b in bad]
    return bad


def repair_landmarks_in_main(soup, report):
    """Lift a <header>/<footer> that ended up INSIDE <main> back out to page level.

    Detecting this and shipping it anyway is pointless: everything inside a header/footer stops
    counting as content for the menu logic, so those sections silently disappear from the menu and
    their links get pruned. It happens whenever the real menu sits nested inside a content block -
    <main> is then wrapped around it (tuonggohungthinh: <header id="wapper_menu"> inside <main>).

    A small bar is MOVED out (header before <main>, footer after it). A big one is not a bar at all,
    so it is demoted back to <div> rather than dragged across the page."""
    main = soup.find("main")
    body = soup.find("body")
    if main is None or body is None:
        return 0
    total = len(body.get_text(" ", strip=True)) or 1
    fixed = 0
    for tag in ("header", "footer"):
        for el in list(main.find_all(tag)):
            share = len(el.get_text(" ", strip=True)) / total
            if share > 0.25:
                el.name = "div"  # too big to be a bar - it was mislabelled, not misplaced
                report.semantic_tags_applied.append(f"{tag} внутри main был слишком большим -> div")
            elif soup.find(tag) is not el and soup.find(tag) is not None and soup.find(tag) is not el:
                el.name = "div"  # a duplicate: the page already has this landmark elsewhere
                report.semantic_tags_applied.append(f"дубль <{tag}> внутри main -> div")
            else:
                if tag == "header":
                    main.insert_before(el.extract())
                else:
                    main.insert_after(el.extract())
                report.semantic_tags_applied.append(f"<{tag}> вынесен из <main> на уровень страницы")
            fixed += 1
    return fixed


def hoist_theme_landmarks(soup, report):
    """Поднять УЖЕ СУЩЕСТВУЮЩИЕ лендмарки темы <header>/<main>/<footer> на уровень <body>.

    WordPress/Astra заворачивает страницу в `div#page.hfeed.site`, а внутри — `<header id=masthead>`,
    `<div#content> … <main>`, `<footer id=colophon>`. Чистильщик иначе переименовывает эту обёртку в
    `<section>` и оставляет все три лендмарка ВЛОЖЕННЫМИ, так что `<header>/<main>/<footer>` не на
    верхнем уровне. Владелец руками выносит их на уровень body, и ничего не ломается — это и есть
    настоящие лендмарки страницы (bambooship: header#masthead/main#main/footer#colophon уже в теме).

    Срабатывает ТОЛЬКО когда все три лендмарка лежат под ОДНОЙ обёрткой-ребёнком body и ни один ещё
    не поднят. Страницы, где лендмарки уже наверху (их собрал сам чистильщик), не трогаются.
    """
    body = soup.find("body")
    if body is None:
        return 0
    header, main, footer = soup.find("header"), soup.find("main"), soup.find("footer")
    if header is None or main is None or footer is None:
        return 0
    if header.parent is body and main.parent is body and footer.parent is body:
        return 0  # уже наверху — нечего делать

    def _top_wrapper(el):
        w = el
        while w.parent is not None and w.parent is not body:
            w = w.parent
        return w if (w is not None and w.parent is body) else None

    wh, wm, wf = _top_wrapper(header), _top_wrapper(main), _top_wrapper(footer)
    if wh is None or not (wh is wm and wm is wf):
        return 0  # не под одной общей обёрткой body-уровня — не наш случай, не рискуем
    wrapper = wh
    # Лендмарк не должен быть вложен в другой лендмарк (иначе перенос ломает вложенность).
    if header.find_parent("main") or footer.find_parent("main") or main.find_parent(("header", "footer")):
        return 0
    # header — перед обёрткой, footer — после; обёртка с контентом становится единственным <main>.
    wrapper.insert_before(header.extract())
    wrapper.insert_after(footer.extract())
    for _nested in wrapper.find_all("main"):
        _nested.name = "div"          # один <main> на страницу: вложенный демотируем в div (id/class целы)
    wrapper.name = "main"             # обёртка (бывш. div#page) и есть основной контент
    report.semantic_tags_applied.append("лендмарки темы подняты на уровень body (header > main > footer)")
    return 1


def relocate_hidden_svg_defs(soup, report):
    """Скрытые svg-symbol-листы (`<svg style="visibility:hidden;position:absolute">` с `<defs>`/
    `<symbol>`) утащить из НАЧАЛА body в самый конец. Elementor/иконочные темы кладут их вверху, и
    они стоят ПЕРЕД <header>, ломая «первый элемент body = header». Не удаляем (на них ссылаются
    `<use href="#…">`), только переносим — положение в DOM для `<use>` не важно, а вид не меняется."""
    body = soup.find("body")
    if body is None:
        return 0
    moved = 0
    for ch in list(body.find_all("svg", recursive=False)):
        style = (ch.get("style") or "").replace(" ", "").lower()
        hidden = "visibility:hidden" in style or "display:none" in style or "position:absolute" in style
        is_defs = ch.find(["defs", "symbol"]) is not None and not ch.get_text(strip=True)
        if hidden and is_defs:
            body.append(ch.extract())
            moved += 1
    if moved:
        report.semantic_tags_applied.append(f"скрытых svg-symbol-листов перенесено вниз body: {moved}")
    return moved


def sections_outside_main(soup):
    """ЕДИНСТВЕННЫЙ ответ на вопрос «какие секции лежат снаружи <main>».

    Раньше на него отвечали двое и по-разному: контракт брал ЛЮБУЮ <section> без родителя <main>,
    а чинилка ходила только по `main.next_siblings` — то есть не видела ни секций ПЕРЕД <main>, ни
    вложенных в обёртку. Контракт исправно печатал «секций СНАРУЖИ <main>: 4», чинилка столь же
    исправно не находила ни одной, и дефект жил между ними: секции есть, но привязка меню смотрит
    только внутрь <main>, и карта сайта теряла разделы.

    Секции внутри <header>/<footer> сюда не попадают — это часть лендмарка, а не потерянный контент.
    """
    main = soup.find("main")
    if main is None:
        return []
    out = []
    for s in soup.find_all("section"):
        if s.find_parent("main") is not None:
            continue
        if s.find_parent(["header", "footer"]) is not None:
            continue
        # Секция, ВНУТРИ которой лежит <main>, — это обёртка страницы, а не потерянный блок.
        # Формально она тоже «снаружи main», и попытка её перенести вырезает страницу целиком
        # вместе с самим <main>: bambooship остался без заголовков, main, header и footer разом.
        # Сравнение по ТОЖДЕСТВУ: `main in s.descendants` сверял бы разметку, а не объект.
        if any(d is main for d in s.descendants):
            continue
        # Вложенная секция переезжает вместе с родителем — отдельно её трогать нельзя.
        if any(p.name == "section" for p in s.parents if getattr(p, "name", None)):
            continue
        out.append(s)
    return out


def pull_sections_into_main(soup, report):
    """Move any <section> that ended up as a SIBLING of <main> inside it.

    Owner's rule: strictly header > main > footer, with every section inside <main>. A section left
    outside is not a cosmetic problem - the menu logic only looks at content inside the content
    area, so such a section is invisible to anchoring: its anchor leads nowhere and the menu quietly
    loses items. Seen as `main > section section` closing early, with two more sections after it."""
    main = soup.find("main")
    body = soup.find("body")
    if main is None or body is None:
        return 0
    moved = 0
    # Сначала соседи <main>, целыми блоками — так сохраняется обёртка вместе с её оформлением.
    for sib in list(main.next_siblings):
        if getattr(sib, "name", None) is None:
            continue
        if sib.name in ("footer", "script", "style", "link", "meta", "noscript", "header"):
            continue
        if sib.find_parent("main") is not None:
            continue
        if sib.find(["header", "footer"]) is not None:
            continue  # holds a landmark - leave it, repair_landmarks_in_main deals with that
        if sib.name == "section" or sib.find("section") is not None:
            main.append(sib.extract())
            moved += 1
    # Затем всё, что осталось снаружи по ЛЮБОЙ причине — секции ПЕРЕД <main> и упрятанные в обёртку.
    # Порядок сохраняется: то, что шло до <main>, встаёт в его начало, остальное — в конец.
    for s in sections_outside_main(soup):
        if s.find_parent("main") is not None:      # мог уехать вместе с родителем на прошлом шаге
            continue
        before = False
        for prev in main.previous_elements:
            if prev is s:
                before = True
                break
        if before:
            main.insert(0, s.extract())
        else:
            main.append(s.extract())
        moved += 1
    if moved:
        report.semantic_tags_applied.append(f"секций возвращено внутрь <main>: {moved}")
    return moved


def audit_against_original(original_html, soup, report, html_path=None):
    """Compare the CLEANED page against the archive it came from and shout when the cleanup lost
    something. This is the regression net for the whole tool.

    Every structural bug this project has hit was of one shape - the output silently lost something
    the input had - and none of them showed up in a per-phase report saying "ok":
      * the site's font replaced by a preset, or its @font-face rules deleted;
      * a menu that went from 8 items to 1 because nothing could be anchored;
      * <header>/<main>/<footer> missing entirely;
      * images or headings disappearing with a failed asset recovery.
    Checking input against output catches all of those at once, including bugs not yet imagined,
    and needs no API key. Findings go to report.audit_warnings so a bad clean announces itself.
    """
    warn = []
    try:
        before = BeautifulSoup(original_html, PARSER)
    except Exception:  # noqa: BLE001 - never let the audit itself break a clean
        return warn

    def _nav_links(s):
        best = 0
        for el in s.find_all(["nav", "ul", "div"]):
            ident = " ".join(el.get("class") or []) + " " + (el.get("id") or "")
            if not re.search(r"nav|menu", ident, re.I):
                continue
            if el.find_parent("footer") is not None:
                continue
            best = max(best, len([a for a in el.find_all("a") if a.get_text(strip=True)]))
        return best

    # 1) landmarks. Only <main> is final at this point: the header (and a generated one) is wired
    # afterwards by site_edit.auto_link_menu, so demanding it here just cries wolf on every site
    # whose header is generated - gigaworks reported "НЕТ <header>" while ending up with one.
    main = soup.find("main")
    if main is None:
        warn.append("НЕТ <main> в результате")
    else:
        # A <main> holding a sliver of the page means the content region was mis-chosen, not that
        # the page is short - sylhetcitycorporation shipped a <main> wrapping only its
        # "Developed by" credit while the whole site sat outside it.
        body_now = soup.find("body")
        share = len(main.get_text(" ", strip=True)) / max(
            len((body_now or soup).get_text(" ", strip=True)), 1)
        if share < 0.25:
            warn.append(f"<main> держит всего {share:.0%} текста — выбран не тот блок")

    # 2) the menu must not be gutted
    nb, na = _nav_links(before), _nav_links(soup)
    if nb >= 3 and na < max(2, nb // 2):
        warn.append(f"меню сжалось: было {nb} пунктов, стало {na}")

    # 3) the site's own font must survive (the archive's font is the one we keep)
    # A declaration is a STACK ("georgia, palatino, serif") - compare the individual families, or
    # the whole stack reads as one exotic missing font and every page cries "пропали шрифты".
    def fam(s):
        out = set()
        for m in FONT_FAMILY_RE.finditer(str(s)):
            for part in m.group(1).split(","):
                part = part.strip().strip("'\"").lower()
                if part:
                    out.add(part)
        return out
    lost = fam(before) - fam(soup)
    # Generic keywords and the OS default stacks are not "the site's font" - losing `impact` or
    # `palatino` says nothing, losing `Inter` says everything.
    real_lost = {f for f in lost if f and not re.match(
        r"^(inherit|initial|unset|serif|sans-serif|monospace|cursive|var\(|arial|helvetica|"
        r"times|times new roman|georgia|verdana|tahoma|impact|palatino|courier|geneva|"
        r"lucida|monaco|menlo|consolas|segoe|system-ui|-apple-system|blinkmacsystemfont)", f)}
    if real_lost and html_path is not None:
        css_text = ""
        for p in html_path.parent.glob("*.css"):
            try:
                css_text += p.read_text(encoding="utf-8", errors="replace")
            except OSError:
                pass
        still = {f for f in real_lost if f in css_text.lower()}
        gone = real_lost - still
        if gone:
            warn.append(f"пропали шрифты сайта: {', '.join(sorted(gone)[:4])}")

    # 4) content must not evaporate
    hb, ha = len(before.find_all(_HEADING_RE)), len(soup.find_all(_HEADING_RE))
    if hb >= 3 and ha < hb * 0.6:
        warn.append(f"заголовков стало меньше: {hb} -> {ha}")
    ib, ia = len(before.find_all("img")), len(soup.find_all("img"))
    if ib >= 3 and ia < ib * 0.5:
        warn.append(f"картинок стало меньше: {ib} -> {ia}")
    tb = len(before.get_text(" ", strip=True))
    ta = len(soup.get_text(" ", strip=True))
    if tb > 500 and ta < tb * 0.6:
        warn.append(f"текста стало меньше: {tb} -> {ta} символов")

    if warn:
        report.audit_warnings = warn
    return warn


_ROLE_ATTR = "data-wb-role"


def _layout_root(body):
    """The level whose children are the page's top-level blocks (past the single wrappers that old
    and modern themes alike put around everything)."""
    root = body
    for _ in range(6):
        kids = [c for c in root.find_all(recursive=False)
                if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "br", "noscript")]
        if len(kids) == 1 and kids[0].name in ("div", "center", "form", "section", "main", "table", "tbody"):
            root = kids[0]
            continue
        # A page wrapper is often NOT an only child (off-canvas panel + overlay sit beside it).
        # Without this, the model was handed "blocks" one of which was the entire page, and its
        # labels were useless through no fault of its own.
        if 2 <= len(kids) <= 6:
            weight = lambda k: len(k.get_text(" ", strip=True)) + 40 * len(k.find_all("a"))
            ranked = sorted(((weight(k), k) for k in kids), key=lambda x: -x[0])
            biggest, others = ranked[0], sum(w for w, _ in ranked[1:])
            if (biggest[1].name in ("div", "center", "form", "section", "main", "table", "tbody")
                    and biggest[0] >= 3 * max(others, 1)
                    and not _looks_like_nav_block(biggest[1])):
                root = biggest[1]
                continue
        if len(kids) >= 2:
            break
        if not kids:
            break
        root = kids[0]
    return root


def plan_layout(soup, report):
    """Ask the model what each top-level block IS, before anything is assembled, and stamp the
    answer on the elements as data-wb-role.

    Assembly used to guess ("everything after the header up to the footer is <main>") and every
    unusual archive broke that guess in a new way - a comments block wrapped around the footer, a
    logo inside an <h1>, a services list mistaken for the menu. Each repair broke the previous
    site. Labels remove the guessing: header on top, content into <main>, footer at the bottom.
    Silent no-op without an API key, so the deterministic path is unchanged."""
    body = soup.find("body")
    if body is None:
        return {}
    try:
        sys.path.insert(0, str(Path(__file__).parent / "site_studio"))
        import semantics
    except Exception:  # noqa: BLE001
        return {}
    if not semantics.available():
        return {}
    root = _layout_root(body)
    blocks = [c for c in root.find_all(recursive=False)
              if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "br", "noscript")]
    if len(blocks) < 2:
        return {}
    if len(blocks) > 40:
        # A flat old-school body with 60 top-level nodes is exactly the page that needs labelling
        # most; skipping it was backwards. Label the substantial blocks and leave the rest alone.
        blocks = sorted(blocks, key=lambda b: -len(b.get_text(" ", strip=True)))[:40]
        blocks.sort(key=lambda b: len(list(b.previous_elements)))
    payload = []
    for i, b in enumerate(blocks):
        heading = b.find(_SEM_HEADINGS)
        text = b.get_text(" ", strip=True)
        payload.append({
            "i": i, "tag": b.name, "cls": _sem_cls(b), "id": b.get("id") or "",
            "links": len([a for a in b.find_all("a") if a.get_text(strip=True)]),
            "chars": len(text), "pos": f"{i+1}/{len(blocks)}",
            "heading": heading.get_text(" ", strip=True) if heading else "",
            "text": text[:120],
        })
    try:
        roles = semantics.label_blocks(payload)
    except Exception:  # noqa: BLE001 - the model is an improvement, never a dependency
        return {}
    if not roles or len(roles) != len(blocks):
        return {}
    roles = validate_plan(blocks, roles, body)
    if roles is None:
        report.semantic_tags_applied.append(
            "разметка агента отклонена проверкой — сборка идёт детерминированно")
        return {}
    plan = {}
    for b, role in zip(blocks, roles):
        b[_ROLE_ATTR] = role
        plan[id(b)] = role
    report.semantic_tags_applied.append(
        "агент разметил блоки до сборки: " + ", ".join(f"{r}" for r in roles))
    return plan


def assemble_main_from_plan(soup, report):
    """Build <main> from the labelled blocks, BEFORE anything else may invent one.

    The older AI tag-pass can hand "main" to a single nested block; once that exists, the
    plan-driven builder skipped itself ("main already present") and sanjhapunjab shipped a <main>
    holding 3% of the page while 57% sat outside it. Deciding here, right after labelling, means
    the landmark reflects the labels and nothing downstream has to guess."""
    body = soup.find("body")
    if body is None or soup.find("main") is not None:
        return 0
    root = _layout_root(body)
    planned = [c for c in root.find_all(recursive=False)
               if getattr(c, "name", None) and _role_of(c)]
    wanted = [c for c in planned if _role_of(c) in ("hero", "content", "comments", "sidebar")]
    if not wanted:
        return 0
    # Take a CONTIGUOUS run only. Extracting scattered blocks and appending them one after another
    # physically moves later content above whatever sat between them - the page would render in a
    # different order than the archive, which is the one thing this tool must never do.
    first = planned.index(wanted[0])
    run = []
    for c in planned[first:]:
        if _role_of(c) in ("hero", "content", "comments", "sidebar"):
            run.append(c)
        elif _role_of(c) == "ignore" and not c.get_text(" ", strip=True):
            continue  # an empty spacer between content blocks doesn't break the run
        else:
            break
    if not run:
        return 0
    body_len = len(body.get_text(" ", strip=True)) or 1
    if sum(len(c.get_text(" ", strip=True)) for c in run) < body_len * 0.25:
        return 0  # the run is a scrap - let the deterministic builder decide instead
    main = soup.new_tag("main")
    run[0].insert_before(main)
    n = 0
    for c in run:
        main.append(c.extract())
        n += 1
    report.semantic_tags_applied.append(f"main собран по разметке агента: {n} блоков")
    return n


def validate_plan(blocks, roles, body):
    """Sanity-check the model's labelling as a WHOLE before a single tag is applied.

    Guarding one field at a time (header must have links, footer must have text, footer must not be
    huge...) is endless, because each new archive breaks a different assumption. This checks the
    plan the way a person would look at it: is there exactly one header and does it hold a menu, is
    the footer a closing bar rather than the page, did anything with real content get thrown away,
    is the answer degenerate. A plan that fails is DISCARDED whole - the deterministic path then
    runs exactly as it does with no API key, which is a known-good outcome rather than a gamble.

    Returns the (possibly corrected) roles, or None to discard the plan entirely.
    """
    if not roles or len(roles) != len(blocks):
        return None
    total = len(body.get_text(" ", strip=True)) or 1
    size = [len(b.get_text(" ", strip=True)) for b in blocks]
    links = [len([a for a in b.find_all("a") if a.get_text(strip=True)]) for b in blocks]
    roles = list(roles)

    # Degenerate answers: every block the same role, or nothing labelled as content at all.
    if len(set(roles)) <= 1:
        return None

    # Nothing may be discarded while it holds real content - "ignore" on a quarter of the page is
    # the model mis-reading a layout, not a decoration.
    for i, r in enumerate(roles):
        if r == "ignore" and size[i] > total * 0.25:
            roles[i] = "content"

    # A footer is a closing bar. If the labelled one carries most of the page, the label is wrong.
    for i, r in enumerate(roles):
        if r == "footer" and size[i] > total * 0.4:
            roles[i] = "content"

    # A header sits at the TOP of the page. Without this, a pair of CTA buttons in the middle of a
    # hero band ("Get In Touch / Connect") satisfied "2+ links" and became gigaworks' <header>,
    # inside <main>, while its real 6-item menu was lost.
    for i, r in enumerate(roles):
        if r == "header" and i > 2:
            roles[i] = "content"

    # Exactly one header, and it has to actually carry navigation.
    heads = [i for i, r in enumerate(roles) if r == "header"]
    if len(heads) > 1:
        best = max(heads, key=lambda i: links[i])
        for i in heads:
            if i != best:
                roles[i] = "ignore" if size[i] == 0 else "content"
        heads = [best]
    # A header is a bar, not a third of the page - the footer rule had this bound, the header
    # didn't, so a huge block with two links could turn a third of the site into chrome.
    if heads and size[heads[0]] > total * 0.4:
        roles[heads[0]] = "content"
        heads = []
    if heads and not can_be_header(blocks[heads[0]], body):
        roles[heads[0]] = "content" if size[heads[0]] else "ignore"

    # At most one footer - keep the last, later blocks are more plausibly the closing bar.
    feet = [i for i, r in enumerate(roles) if r == "footer"]
    for i in feet[:-1]:
        roles[i] = "content"

    # Something must remain as the page content, otherwise <main> would be empty.
    if not any(r in ("hero", "content", "comments", "sidebar") for r in roles):
        return None
    kept = sum(size[i] for i, r in enumerate(roles)
               if r in ("hero", "content", "comments", "sidebar"))
    if kept < total * 0.3:
        return None  # the plan would strand most of the page outside <main>
    return roles


def _role_of(el):
    return (el.get(_ROLE_ATTR) or "") if hasattr(el, "get") else ""


def _strip_role_marks(soup):
    for el in soup.find_all(attrs={_ROLE_ATTR: True}):
        del el[_ROLE_ATTR]


def _content_host(body, hdr=None, ftr=None):
    """The single element that actually holds the page's content, for pages whose landmarks are
    buried in old <center>/<table> scaffolding. Picked as the deepest element still carrying almost
    all of the body text: going deeper than that would start cutting content away."""
    best, body_len = None, len(body.get_text(" ", strip=True)) or 1
    node = body
    for _ in range(8):
        nxt = None
        for c in node.find_all(recursive=False):
            if getattr(c, "name", None) in (None, "script", "style", "link", "meta", "br", "noscript"):
                continue
            if c is hdr or c is ftr or c.name in ("header", "footer", "main"):
                continue
            if len(c.get_text(" ", strip=True)) >= body_len * 0.7:
                nxt = c
                break
        if nxt is None:
            # A two-column page (content 65% / sidebar 35%) clears no 70% bar. Giving up here left
            # the page with NO <main> at all, because the scrap one had already been removed.
            if best is None:
                cands = [c for c in node.find_all(recursive=False)
                         if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "br", "noscript")
                         and c is not hdr and c is not ftr
                         and c.name not in ("header", "footer", "main")]
                if cands:
                    best = max(cands, key=lambda c: len(c.get_text(" ", strip=True)))
            break
        best, node = nxt, nxt
    return best


def _section_container(root, hdr=None, ftr=None):
    """The element whose DIRECT children are the page's content blocks.

    Sectioning used to look only at root's own children, but real themes bury the content two to
    four wrappers deep (`.site > .page-content > .content > .style-189 > [sections]`), so almost
    nothing qualified: sanjhapunjab had 36 headings and produced ONE section, bikenfoot 16 headings
    and one. With no sections there is nothing for the header anchors to point at, which is what
    made the menu-linking pass strip menus down to a single item.

    Breadth-first, so the SHALLOWEST container holding 2+ heading-bearing blocks wins: page areas
    sit high in the tree, while lists of cards/articles sit deeper - picking the deepest match would
    turn every news card into a <section>."""
    queue = [(root, 0)]
    while queue:
        el, depth = queue.pop(0)
        if depth > 6:
            continue
        # Old sites put every content block in a <td>/<tr>/<center>; a div-only whitelist could not
        # even traverse them, which is why table layouts produced zero sections and their menus were
        # then stripped for having nothing to anchor to.
        kids = [c for c in el.find_all(recursive=False)
                if getattr(c, "name", None) in ("div", "section", "article", "td", "tr", "tbody",
                                                "table", "center", "li", "ul", "main")]
        good = [c for c in kids
                if c is not hdr and c is not ftr
                and not _SEM_HERO_RE.search(_sem_cls(c))
                and c.find(_SEM_HEADINGS) is not None
                and not (hdr is not None and hdr in c.descendants)
                and not (ftr is not None and ftr in c.descendants)]
        if len(good) >= 2:
            return el
        queue.extend((c, depth + 1) for c in kids)
    return None


def ensure_landmarks(soup, report):
    """Deterministic HTML5 landmarks (runs WITH OR WITHOUT the AI key): wrap the content region in
    <main> and turn top-level content <div>s (those carrying a heading = a real section) into
    <section>. section/main are generic blocks exactly like div, and classes are preserved, so this
    is box-model-neutral and doesn't change the look (verified: renders identical). Skips header/
    footer/nav. Idempotent: only adds <main> if none exists; only renames heading-bearing divs."""
    body = soup.find("body")
    if body is None:
        return
    root = body
    for _ in range(4):  # descend through a single generic wrapper (old <center>/<div>/<form> layouts)
        kids = [c for c in root.find_all(recursive=False)
                if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "br", "noscript")]
        if len(kids) == 1 and kids[0].name in ("div", "center", "form", "section", "main"):
            root = kids[0]
        else:
            break
    hdr, ftr = soup.find("header"), soup.find("footer")
    renamed = 0
    sec_root = _section_container(root, hdr, ftr) or root
    for c in [c for c in sec_root.find_all(recursive=False) if getattr(c, "name", None)]:
        if c is hdr or c is ftr or c.name != "div":
            continue
        if _SEM_HERO_RE.search(_sem_cls(c)) or c.find(_SEM_HEADINGS) is None:
            continue  # only a heading-bearing content block becomes a <section>
        if hdr is not None and hdr in c.descendants:
            continue
        if ftr is not None and ftr in c.descendants:
            continue
        c.name = "section"
        renamed += 1
    wrapped = 0
    # PLAN-DRIVEN ASSEMBLY: when the model labelled the blocks, <main> is simply "the run of blocks
    # that are page content" - no guessing about what sits between the header and the footer, which
    # is the guess that kept breaking (a comments block wrapped around the footer, a credit line
    # mistaken for the whole content region).
    planned = [c for c in _layout_root(body).find_all(recursive=False)
               if getattr(c, "name", None) and _role_of(c)]
    if planned and soup.find("main") is None:
        wanted = [c for c in planned if _role_of(c) in ("hero", "content", "comments", "sidebar")]
        if wanted:
            main = soup.new_tag("main")
            wanted[0].insert_before(main)
            for c in wanted:
                main.append(c.extract())
                wrapped += 1
            body_len_f = len(body.get_text(" ", strip=True)) or 1
            for c in planned:
                if _role_of(c) != "footer" or c.name == "footer":
                    continue
                if soup.find("footer") is not None:
                    break  # a footer already exists - a second one is never right
                txt_f = c.get_text(" ", strip=True)
                if not txt_f or len(txt_f) > body_len_f * 0.4:
                    continue
                c.name = "footer"
            report.semantic_tags_applied.append(f"main собран по разметке агента: {wrapped} блоков")

    if soup.find("main") is None:
        # <main> must NOT depend on a <header> existing. It used to, and that quietly voided the
        # structural guarantee on every site whose header is generated later by header_gen inside
        # auto_link_menu: at this point there is no <header> yet, so no <main> was ever created and
        # the page shipped with no content landmark at all (bikenfoot, sylhet: 0/0/0; gigaworks: no
        # <main>). Wrap whatever sits between the header and the footer - or simply everything, when
        # the page has neither.
        kids = [c for c in root.find_all(recursive=False) if getattr(c, "name", None)]
        # <main> is a flow element - never build one inside a table, it would be hoisted out by the
        # parser and wreck an old table layout.
        if root.name in ("table", "tbody", "thead", "tfoot", "tr"):
            kids = []
        # The header is not always a direct child here (it can be promoted from a nav buried in a
        # wrapper). Start after the LAST child that is - or contains - the header, so <main> always
        # begins past it. Bailing out on a nested header instead, as an earlier version did, meant
        # danvanhaiphong ended up with no <main> at all.
        start = 0
        for i, c in enumerate(kids):
            if c is hdr or (hdr is not None and hdr in c.descendants):
                start = i + 1
        content = []
        for c in kids[start:]:
            # Stop at the footer (or the block holding it) - keeps <main> a contiguous run, so
            # nothing is reordered on the page.
            if c is ftr or (ftr is not None and ftr in c.descendants):
                break
            if c.name in ("script", "style", "link", "meta", "noscript"):
                continue
            content.append(c)
        if content:
            main = soup.new_tag("main")
            content[0].insert_before(main)
            for c in content:
                main.append(c.extract())
                wrapped += 1
            # <main> is supposed to hold the page's content. When the header sits deep inside a
            # wrapper, "everything after it" can be almost nothing - on sylhetcitycorporation
            # <main> ended up wrapping just the "Developed by" line while all 27 menu links and
            # the entire body text stayed outside it. If what we wrapped is a scrap of the page,
            # undo it and wrap the biggest content container instead.
            body_len = len(body.get_text(" ", strip=True)) or 1
            if len(main.get_text(" ", strip=True)) < body_len * 0.25:
                for c in list(main.find_all(recursive=False)):
                    main.insert_before(c.extract())
                main.decompose()
                wrapped = 0
                host = _content_host(body, hdr, ftr)
                if host is not None:
                    main = soup.new_tag("main")
                    host.insert_before(main)
                    main.append(host.extract())
                    wrapped = 1

        # Whatever content still sits AFTER <main> belongs inside it. The contiguous run stops at
        # the footer, which left sanjhapunjab with its comments block - 30% of the page - stranded
        # as a sibling of <main>. Absorb every following block that is not, and does not hold, the
        # header or the footer.
        main = soup.find("main")
        if main is not None:
            for sib in list(main.next_siblings):
                if getattr(sib, "name", None) is None:
                    continue
                if sib.name in ("script", "style", "link", "meta", "noscript", "header", "footer"):
                    continue
                if hdr is not None and hdr in sib.descendants:
                    continue
                if sib.find("header") is not None:
                    continue
                # A trailing content block that HAPPENS to contain the footer must not stay outside
                # <main> just because of that - sanjhapunjab's comments block (30% of the page) sits
                # around the footer, so skipping it stranded a third of the content. Lift the footer
                # out to body level first, then the block itself can be absorbed.
                inner_ftr = sib.find("footer")
                if inner_ftr is not None:
                    if inner_ftr is sib or inner_ftr.parent is None:
                        continue
                    sib.insert_after(inner_ftr.extract())
                    ftr = inner_ftr
                if not sib.get_text(" ", strip=True) and sib.find(["img", "svg", "video"]) is None:
                    continue  # empty spacer div, nothing to move
                main.append(sib.extract())
                wrapped += 1
    if renamed or wrapped:
        report.semantic_tags_applied.append(f"детерминированно: div→section {renamed}, main-обёртка {wrapped} блоков")


# A block containing any of these is a page WRAPPER - it can never itself be nav/header/footer.
_LANDMARK_TAGS = ("main", "header", "footer", "nav")


def repair_landmark_nesting(soup, report=None):
    """Heal structurally impossible landmark nesting, whoever produced it (AI pass, a weird theme,
    an older run of this tool). A <nav>/<header>/<footer>/<aside> that CONTAINS <main> (or a
    header+footer pair) is a mislabelled page wrapper: it makes every section invisible to the
    menu-anchor logic, so the restored page ends up with a menu that links nowhere. Demote such a
    wrapper back to a neutral <div>, keeping every attribute and child untouched (tag rename only -
    box-model neutral, no CSS selector on class/id breaks). Runs deterministically, no AI needed."""
    fixed = []
    for tag in soup.find_all(["nav", "header", "footer", "aside"]):
        if tag.find("main") is not None or (tag.find("header") is not None and tag.find("footer") is not None):
            cls = " ".join(tag.get("class", [])[:2])
            fixed.append(f"<{tag.name}{('.' + cls.split()[0]) if cls else ''}> wrapped page landmarks -> div")
            tag.name = "div"
    if fixed and report is not None:
        report.landmarks_repaired = fixed
    return fixed


def apply_semantic_tags(soup, report):
    """Recover HTML5 semantics on a bad-markup export. Two passes, neither restructures content:
      1) DETERMINISTIC header - the top nav bar becomes (or is wrapped in) <header>; a hero band
         is never the header (see _promote_header). Done first so it's fixed before the AI runs.
      2) Haiku suggests a landmark for each remaining top-level <div>/<section>, GUARDED so it
         never turns a hero/intro/banner band into a landmark and never duplicates an existing
         <main>/<header>/<footer> (those fall back to <section>).
    Runs early so the later deterministic menu/section logic sees real landmarks. Best-effort."""
    sem = _semantics()
    if sem is None or not sem.available():
        return
    body = soup.find("body")
    if body is None:
        return
    # Same reason as above: scanning inside <main> invents a second <header> within it.
    root = body

    # 1) Deterministic page header from the real top nav (never a hero).
    old_hdr = _promote_header(soup, root)
    if old_hdr is not None:
        report.semantic_tags_applied.append(f"{old_hdr} -> header (top nav)")

    # 2) AI pass over the remaining generic top-level blocks.
    children = [c for c in root.find_all(recursive=False)
                if getattr(c, "name", None) and c.name in _SEM_RENAMEABLE]
    if not children:
        return
    n = len(children)
    sigs = []
    for idx, ch in enumerate(children):
        h = ch.find(_SEM_HEADINGS)
        sigs.append({
            "tag": ch.name,
            "cls": _sem_cls(ch),
            "id": ch.get("id", "") or "",
            "heading": h.get_text(" ", strip=True) if h else "",
            "links": len(ch.find_all("a")),
            "pos": "first" if idx == 0 else ("last" if idx == n - 1 else "middle"),
        })
    tags = sem.semantic_tags(sigs)
    if not tags:
        return
    # Landmarks already present that we must not duplicate (main is unique; a second header/footer
    # is sloppy). header may have just been created in pass 1.
    taken = {t for t in ("main", "header", "footer") if soup.find(t) is not None}
    for ch, sig, newtag in zip(children, sigs, tags):
        cls = sig["cls"]
        # A hero/intro/banner band is content: force it to <section>, never a landmark - this is
        # exactly the "hero got tagged <header>" case the deterministic pass avoids for the header.
        if _SEM_HERO_RE.search(cls) and newtag in ("header", "footer", "nav", "main", "aside"):
            newtag = "section"
        # STRUCTURAL INVARIANT: a block that CONTAINS other landmarks is a page wrapper, never a
        # landmark itself. Without this the model can label the outer wrapper <nav> (it holds the
        # whole site, so it holds every link) and produce <nav><header><main><footer></nav> - which
        # is not just wrong semantics: _content_sections treats everything inside a nav/header/
        # footer as chrome, so EVERY section disappears and no menu anchor can attach. Seen on
        # danvanhaiphong ("div -> nav" over 838 descendants). Applies to any model output.
        if newtag in ("nav", "header", "footer", "aside") and ch.find(_LANDMARK_TAGS) is not None:
            newtag = "section" if ch.name == "div" else "keep"
        if newtag == "keep" or newtag == ch.name:
            continue
        # Plausibility, the same bar the deterministic passes use. Without it this pass could name
        # an empty spacer <footer>, hand <main> to a 20-character block (after which the real
        # builder skips itself because "main already exists"), or turn a whole content column into
        # <nav> - and everything inside a nav/header/footer stops counting as content.
        _txt = ch.get_text(" ", strip=True)
        _body_len = len((soup.find("body") or soup).get_text(" ", strip=True)) or 1
        if newtag in ("header", "footer", "nav", "main", "aside") and not _txt:
            newtag = "keep"
        elif newtag in ("header", "footer", "nav") and len(_txt) > _body_len * 0.4:
            newtag = "section" if ch.name == "div" else "keep"
        elif newtag == "main" and len(_txt) < _body_len * 0.25:
            newtag = "section" if ch.name == "div" else "keep"
        if newtag == "keep" or newtag == ch.name:
            continue
        if newtag in taken:  # don't create a duplicate main/header/footer
            newtag = "section"
        if newtag == "section" and newtag == ch.name:
            continue
        if newtag in ("main", "header", "footer"):
            taken.add(newtag)
        old = ch.name
        ch.name = newtag
        first_cls = ("." + cls.split()[0]) if cls else ""
        report.semantic_tags_applied.append(f"{old}{first_cls} -> {newtag}")


def clean_html_file(
    html_path,
    fonts_param,
    dry_run,
    backup,
    keep_contact_info=True,
    favicon=None,
    recover_images=True,
    domain_override=None,
    progress=None,
    cancelled=None,
    normalize_headings=True,
):
    # Install this run's cancel hook for the whole thread, so even the low-level network fetches
    # (font/icon downloads, archive recovery) abort promptly - not just the phase boundaries.
    # Overwritten at the start of every call, so parallel cleanups (each its own thread) and
    # repeated CLI calls never see a stale hook.
    _set_cancel_check(cancelled)
    _reset_recovery_state()  # fresh archive-recovery circuit breaker per run

    # IDEMPOTENCY: always clean from the TRUE original. The first run saves the raw download into
    # <file>.bak; a re-run ("Очистить снова") must clean THAT raw again, not the already-cleaned
    # index.html - otherwise it double-processes (re-extracts inline <style> over the theme CSS,
    # duplicates our injected <link>s) and a good page comes out white/unstyled. So: if .bak exists,
    # parse it; and .bak is written ONCE (below) so it stays the real raw forever.
    bak_path = html_path.with_suffix(html_path.suffix + ".bak")
    source_path = bak_path if (backup and bak_path.is_file()) else html_path
    original_text = read_text_safe(source_path)
    # A stray element between <html> and <head> (e.g. wayback/YUI's
    # <div id="yui3-css-stamp">) makes some parsers misplace <head>'s
    # content into <body> - strip it before parsing.
    preclean_text = PRECLEAN_STRAY_TAG_RE.sub("", original_text)
    soup = BeautifulSoup(preclean_text, PARSER)
    normalize_charset_meta(soup)
    report = Report()

    def _p(pct, msg):
        # optional live-progress callback (site_studio streams this to the UI); no-op for CLI.
        # Also a cancellation checkpoint: a cancelled job aborts at the next phase boundary.
        _raise_if_cancelled(cancelled)
        if progress:
            progress(pct, msg)

    _p(4, "Читаю и разбираю страницу")

    content_domain = get_site_domain(soup)
    site_domain = domain_override or _domain_from_folder_name(html_path) or content_domain
    old_domain = None
    if content_domain and site_domain:
        bare_content = content_domain[4:] if content_domain.lower().startswith("www.") else content_domain
        bare_site = site_domain[4:] if site_domain.lower().startswith("www.") else site_domain
        if bare_content.lower() != bare_site.lower():
            old_domain = content_domain

    _p(9, "Восстанавливаю недостающие картинки из архива")
    if recover_images:
        recover_missing_local_images(soup, html_path, report, dry_run=dry_run)

    check_and_fix_redirect(soup, site_domain, report)
    check_and_fix_noindex(soup, report)

    _p(28, "Снимаю обёртки веб-архива, тулбар и мусор")
    strip_wayback_comment_and_html_attrs(soup)
    strip_wayback_toolbar(soup)
    strip_base_href(soup, report)  # kill <base href> - else all relative CSS/JS/img break locally
    strip_sri_attrs(soup, report)  # kill integrity/crossorigin - else SRI blocks our local copies
    strip_blocking_meta(soup, report)  # kill <meta CSP> - else it blocks every local resource
    unwayback_all_attrs(soup)
    clean_scripts(soup, report)
    clean_stylesheet_links(soup, report, site_domain)
    strip_cms_meta_links(soup, report)
    _p(44, "Вырезаю скрипты, мету и следы владельца")
    strip_owner_traces(soup, update_year=True, report=report)
    clean_head_styles(soup, report, html_path)
    promote_src(soup)
    add_lazy_loading(soup, report)
    clean_data_and_event_attrs(soup)
    # The reveal-animation JS is gone, so anything it left hidden would stay hidden forever.
    unhide_scroll_reveal(soup, report, html_path)
    # Same idea for sliders: without their JS they must still show slide 1, and a slider whose
    # slides were JS-generated must not leave a tall empty band.
    fix_static_sliders(soup, report)
    # DETERMINISTIC page header (runs WITH OR WITHOUT the AI key): make the site's real top nav -
    # even a bare <div>/<center> full of links with no nav class - the page <header>. Old exports
    # whose menu is just a link-heavy div were previously missed entirely; the AI pass below only
    # refines. (If there's genuinely no nav, auto_link_menu generates a header as the last step.)
    # The model labels the blocks FIRST, so header/footer/main below are placed by MEANING rather
    # than by position heuristics. No key -> returns {} and everything downstream behaves as before.
    plan_layout(soup, report)
    _hdr_root = soup.find("body")
    if _hdr_root is not None:
        # Header and footer FIRST, then <main>. The other way round, _promote_header searched
        # inside the freshly built <main> while the header block sat outside it, so the real menu
        # was invisible to it.
        # Search from BODY, never from <main>. Narrowing to <main> hid the real header whenever the
        # page had one - and any modern theme capture ships its own <main id="site-main">, so the
        # scan ran INSIDE it and wrapped a post-navigation or breadcrumb into a second <header>
        # mid-article: a fixed bar over the content, sections lost, article links deleted as if
        # they were menu items, copyright injected into the middle of the text.
        _old_hdr = _promote_header(soup, _hdr_root)
        if _old_hdr is not None:
            report.semantic_tags_applied.append(f"{_old_hdr} -> header (верхний нав, детерминированно)")
        _old_ftr = _promote_footer(soup, _hdr_root)
        if _old_ftr is not None:
            report.semantic_tags_applied.append(f"{_old_ftr} -> footer (детерминированно)")
        verify_landmark_roles(soup, report)
        assemble_main_from_plan(soup, report)
    # AI (Haiku) tag-semantics: promote top-level <div> soup into HTML5 landmarks, so the menu/
    # section logic below (and the final markup) sees real header/nav/main/section/footer. Skipped
    # in dry-run (no LLM spend on a preview) and whenever no ANTHROPIC_API_KEY is configured.
    if not dry_run:
        apply_semantic_tags(soup, report)
    # Deterministic safety net over the AI pass (rule: AI may only IMPROVE, never break the
    # structure). Demotes any nav/header/footer that swallowed the page's landmarks - that
    # otherwise hides every section from the menu-anchor step.
    repair_landmark_nesting(soup, report)
    # Heading tags. Opt-out (normalize_headings=False, the card switch "менять заголовки" off):
    # DON'T touch headings AT ALL - no level fix, no h1 promotion, not even dropping empty ones -
    # so they stay EXACTLY as in the archive. These are the ONLY two functions that modify h1-h6.
    if normalize_headings:
        drop_empty_headings(soup, report)
        normalize_heading_levels(soup, report)
    # Deterministic <main> + <section> landmarks (box-model-neutral, runs without the AI key too).
    ensure_landmarks(soup, report)
    # Empty fonts_param -> try to detect a font already used on this page (falls back
    # to a random preset if the page only declares generic/system-default fonts).
    _p(56, "Подключаю шрифт (скачиваю локально)")
    inject_google_fonts(soup, resolve_font_input(fonts_param, soup=soup, html_path=html_path), html_path=html_path)
    inject_image_object_fit_style(soup)
    _p(60, "Иконки: чиню и подключаю Font Awesome")
    ensure_icon_fonts(soup, html_path, report, dry_run=dry_run)
    clean_links_a(soup, site_domain, report, keep_contact_info=keep_contact_info)
    localize_media_refs(soup, html_path, site_domain, report, dry_run=dry_run, cancelled=cancelled)
    # Third-party images (paypal button, google thumbs): pull them local, or drop the element -
    # a restored page must not hot-link or beacon out to anyone.
    localize_external_media(soup, html_path, site_domain, report, dry_run=dry_run, cancelled=cancelled)
    if not keep_contact_info:
        strip_contact_info(soup, report, old_domain=old_domain)
    else:
        # Contacts are kept - but normalize their e-mail domains to THIS site's domain so a
        # restored page never keeps the old owner's address (info@old.com -> info@site.in).
        normalize_email_domains(soup, site_domain, report)
    clean_iframes(soup, report)
    # A kept script whose jQuery plugin never got archived can only throw on every page load.
    drop_scripts_with_missing_plugins(soup, html_path, report)
    # Whatever inline JS survives is real behaviour - get it out of <head> into a deferred file.
    externalize_inline_scripts(soup, html_path, report, dry_run=dry_run)
    # ...and if those survivors need jQuery while the export never shipped it, self-host it now
    # (last, so it sees the scripts that actually remain).
    ensure_jquery(soup, html_path, report, dry_run=dry_run)
    scan_content_flags(soup, report)
    detect_and_report_logo(soup, report)
    # Domain override given -> rebrand from it: generate a text wordmark logo named after the
    # domain (example.com -> "Example"), swap it in for the old image/text logo, and update
    # <title>/og. Opt-in: only fires when the user explicitly set a target domain.
    effective_brand = None
    if domain_override and site_domain:
        _p(64, "Ставлю логотип по домену")
        derived = apply_auto_logo(soup, html_path, site_domain, report, dry_run=dry_run)
        effective_brand = derived or _brand_name_from_domain(site_domain) or "Site"
        apply_brand_text(soup, effective_brand, report)
    _p(70, "Ссылки, контакты, favicon, SEO-файлы")
    favicon_brand_hint = effective_brand or (_bare_domain(site_domain).split(".")[0] if site_domain else None)
    ensure_favicon(soup, html_path, favicon, report, dry_run=dry_run, brand_hint=favicon_brand_hint)
    ensure_local_seo_files(html_path, site_domain, report, dry_run=dry_run, overwrite=True)
    ensure_htaccess(html_path, report, dry_run=dry_run, overwrite=True, site_domain=site_domain)
    # AI (Haiku) meta: write a real <title> + <meta description> from the page content, just
    # before the head is reordered. Skipped in dry-run and without an ANTHROPIC_API_KEY.
    if not dry_run:
        apply_semantic_meta(soup, site_domain, report)
    ensure_canonical(soup, html_path, site_domain, report, dry_run=dry_run)
    ensure_html_lang(soup, report)   # <html lang> - auto-detected; also drives footer-copyright localisation
    strip_head_to_essentials(soup, report)  # в head оставить только title/description/canonical/иконку
    dedupe_head_meta(soup, report)   # drop duplicate og:/name metas piled up by the CMS export
    _reorder_head_seo(soup)  # head order: <title> -> <meta description> -> <link canonical> -> fonts
    check_internal_link_targets(soup, html_path, report)
    strip_empty_style_declarations(soup)
    # QA self-check: any <link rel=stylesheet> that's really an HTML page (would silently lose those
    # styles) is recovered or dropped; missing local stylesheets are flagged. Runs before the write
    # so a removed dead <link> lands in the saved HTML.
    if not dry_run:
        _audit_stylesheets(soup, html_path, site_domain, report)

    hoist_theme_landmarks(soup, report)     # готовые header/main/footer темы — на уровень body
    relocate_hidden_svg_defs(soup, report)  # скрытые svg-symbol-листы — из начала body в конец
    pull_sections_into_main(soup, report)   # секции обязаны лежать внутри <main>
    repair_landmarks_in_main(soup, report)  # лендмарк внутри <main> — чиним, а не только сообщаем
    _strip_role_marks(soup)  # служебные метки разметки не должны уехать в готовый HTML
    _p(82, "Записываю страницу")
    new_text = collapse_blank_lines(str(soup))

    print(f"\n### {html_path} (site domain detected: {site_domain or 'unknown'}) ###")
    print(report.render())

    if dry_run:
        print("[dry-run] no files written")
        return report.render()

    if backup and not bak_path.is_file():
        bak_path.write_text(original_text, encoding="utf-8")  # save the raw ONCE; never overwrite it
    html_path.write_text(new_text, encoding="utf-8")

    _p(88, "Чищу CSS/JS и восстанавливаю ассеты из архива")
    clean_local_linked_files(html_path, report=report, site_domain=site_domain, cancelled=cancelled)
    recover_corrupted_local_assets(html_path, report, site_domain=site_domain, cancelled=cancelled)
    move_orphaned_wayback_assets(html_path, new_text, report)
    local_css_texts = []
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href", "")
        if href and not is_external(href):
            css_path = (html_path.parent / href).resolve()
            if css_path.is_file() and css_path.suffix.lower() == ".css":
                local_css_texts.append(read_text_safe(css_path))
    _p(96, "Убираю неиспользуемые файлы")
    remove_unused_local_assets(html_path, new_text, report, dry_run=dry_run, extra_texts=local_css_texts)
    strip_dead_css_urls(html_path, report)
    drop_dangling_local_media(html_path, report)  # HTML twin of strip_dead_css_urls: no broken <img>
    _audit_final_output(html_path, report)  # universal "did it come out broken?" self-check
    # ...and the input-vs-output net: everything the archive had must still be here.
    for _w in audit_against_original(original_text, soup, report, html_path):
        print(f"[audit] ВНИМАНИЕ: {_w}")
    # Контракт: одинаковые правила для ЛЮБОГО сайта, включая те, которых я никогда не видел.
    for _w in verify_output_contract(soup, report, original_text):
        print(f"[contract] НАРУШЕНО: {_w}")

    report_path = html_path.with_name(html_path.name + ".cleanup-report.txt")
    rendered = report.render()
    report_path.write_text(rendered, encoding="utf-8")
    return rendered  # caller reports 100% after any post-steps (e.g. menu auto-link)


def check_url_live(url):
    """PBN checklist items 1-5 & 39-43: reachability/HTTPS/redirect/robots/sitemap, on a live URL."""
    import urllib.error
    import urllib.request

    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    lines = ["=== live-site check ===", f"target: {url}", ""]
    blockers = []

    def _get(u):
        req = urllib.request.Request(u, headers={"User-Agent": "Mozilla/5.0 (pbn-checklist-bot)"})
        return urllib.request.urlopen(req, timeout=15)

    try:
        resp = _get(url)
        final_url = resp.geturl()
        status = resp.status
        lines.append(f"status: {status}")
        lines.append(f"final URL after redirects: {final_url}")
        if not final_url.startswith("https://"):
            blockers.append("сайт не открывается по HTTPS")
        if domain_of(final_url) != domain_of(url):
            blockers.append(f"редирект на другой домен: {domain_of(final_url)}")
        if status != 200:
            blockers.append(f"главная не отдаёт 200 (получили {status})")
    except urllib.error.HTTPError as e:
        lines.append(f"status: {e.code} (HTTPError)")
        blockers.append(f"главная недоступна: HTTP {e.code}")
    except Exception as e:  # noqa: BLE001 - report any network failure as a blocker
        lines.append(f"error: {e}")
        blockers.append(f"сайт не открывается: {e}")

    for fname in ("robots.txt", "sitemap.xml"):
        check_url = url.rstrip("/") + "/" + fname
        try:
            r = _get(check_url)
            lines.append(f"{fname}: reachable (status {r.status})")
        except Exception as e:  # noqa: BLE001
            lines.append(f"{fname}: NOT reachable ({e})")

    lines.append("")
    lines.append(f"BLOCKER: {'; '.join(blockers) if blockers else 'нет'}")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Clean a Wayback-Machine-saved static site export.")
    parser.add_argument(
        "target", nargs="?", help="Path to a site folder (recurses for *.html) or a single .html file"
    )
    parser.add_argument(
        "--fonts",
        default=None,
        help="Google Fonts families to link in. Left unset, the cleanup tries to detect a font "
        "already used on the page (its own <style>/linked CSS) and keep that; falls back to a "
        "random Jost/Montserrat preset if nothing usable is found.",
    )
    parser.add_argument("--favicon", help="Copy this file in and set it as the favicon")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument(
        "--strip-contact-info",
        action="store_true",
        help="Opt in to removing mailto:/tel: links and redacting email/phone/address text "
             "(KEPT by default - cleanup never touches contact info unless you ask)",
    )
    parser.add_argument(
        "--check-url",
        help="Standalone live-site check (HTTPS/redirect/status/robots/sitemap) - no folder needed",
    )
    parser.add_argument(
        "--no-image-recovery",
        action="store_true",
        help="Don't re-download missing local images from web.archive.org (on by default)",
    )
    parser.add_argument(
        "--domain",
        help="Override the detected site domain (used in robots.txt/sitemap.xml/relative-link checks)",
    )
    args = parser.parse_args()

    if args.check_url:
        print(check_url_live(args.check_url))
        return

    if not args.target:
        parser.error("target is required unless --check-url is given")

    target = Path(args.target)
    if not target.exists():
        print(f"error: path not found: {target}")
        sys.exit(1)

    html_files = [target] if target.is_file() else sorted(target.rglob("*.html"))
    if not html_files:
        print("no .html files found")
        sys.exit(1)

    for html_path in html_files:
        clean_html_file(
            html_path,
            args.fonts,
            args.dry_run,
            backup=not args.no_backup,
            keep_contact_info=not args.strip_contact_info,
            favicon=args.favicon,
            recover_images=not args.no_image_recovery,
            domain_override=args.domain,
        )


if __name__ == "__main__":
    main()

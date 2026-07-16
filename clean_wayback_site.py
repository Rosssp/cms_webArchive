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
import os
import random
import re
import shutil
import subprocess
import sys
import threading
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        _stream.reconfigure(encoding="utf-8", errors="replace")

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
PRESET_FONTS = ("Jost", "Montserrat")
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


WAYBACK_PREFIX_RE = re.compile(
    r"(?:(?:https?:)?//web\.archive\.org)?/web/\d{1,14}[a-zA-Z_]*/(?=https?://|//)"
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
        netloc = urlsplit(url).netloc.lower()
    except ValueError:
        return ""
    if netloc.startswith("www."):
        netloc = netloc[4:]
    return netloc


def is_external(url):
    parts = urlsplit(url)
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
    parts = urlsplit(url)
    path = parts.path or "/"
    rebuilt = urlunsplit(("", "", path, parts.query, parts.fragment))
    return rebuilt or "/"


class Report:
    def __init__(self):
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

    def render(self):
        lines = ["=== cleanup report ===", ""]
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
    if src and Path(urlsplit(src).path).stem.lower() in FOUNDATIONAL_JS_STEMS:
        return "keep"
    if any(k in combined for k in SCRIPT_DROP_KEYWORDS):
        return "drop"
    if any(k in combined for k in SCRIPT_KEEP_KEYWORDS):
        return "keep"
    if src:
        stem = Path(urlsplit(src).path).stem
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
        fname = Path(urlsplit(clean_href).path).name
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


_FONT_FACE_RULE_RE = re.compile(r"@font-face\s*\{[^{}]*\}", re.I)
_TYPEKIT_RULE_RE = re.compile(r"@import[^;]*typekit[^;]*;", re.I)


def _strip_font_loading_rules(css):
    """Remove the site's OWN webfont loading (@font-face rules + typekit @imports) from a CSS blob,
    KEEPING everything else. A CMS/Blogger skin block mixes dozens of @font-face rules straight into
    ~90KB of layout CSS, so dropping the whole <style> (the old behaviour) nuked the theme and left
    the page unstyled. We inject our own font, so only the font rules need to go."""
    css = _FONT_FACE_RULE_RE.sub("", css)
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
    carries ':wght@...' it's kept (just case-normalized); empty -> the default font."""
    name = (name or "").strip()
    if not name:
        return DEFAULT_GOOGLE_FONTS
    if ":" in name:
        return normalize_font_family(name)
    base = name.split(":")[0].strip()
    return normalize_font_family(f"{base}:wght@{FONT_WEIGHTS}")


def random_preset_font_param():
    """A random preset font (Jost/Montserrat) at the standard weights - what the cleanup
    pass drops in automatically so restored sites don't all share one typeface."""
    return font_param_from_name(random.choice(PRESET_FONTS))


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

    for name in tokens:
        key = name.lower()
        if key in GENERIC_FONT_KEYWORDS or key in SYSTEM_FONT_NAMES or key in ICON_FONT_NAMES:
            continue
        if any(hint in key for hint in ICON_FONT_HINTS):
            continue
        # A blocklist can't enumerate every OS-default/icon-font name a theme might
        # use - ask Google Fonts itself whether this family actually exists there
        # before committing to it (avoids repeating the Glyphicons/Menlo mistake for
        # whatever the next unlisted one turns out to be).
        if _google_font_exists(name):
            return font_param_from_name(name)
    return None


def _google_font_exists(name):
    """True if Google Fonts' css2 API actually serves this family - a made-up/system/
    icon-font name 400s or comes back without any @font-face rule."""
    import urllib.error
    import urllib.parse
    import urllib.request

    try:
        url = f"https://fonts.googleapis.com/css2?family={urllib.parse.quote(name)}&display=swap"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200 and b"@font-face" in resp.read(500)
    except (urllib.error.URLError, OSError):
        return False


def resolve_font_input(raw, soup=None, html_path=None):
    """UI/CLI helper: empty -> try to detect a font already used on the page (needs
    soup+html_path; falls back to a random preset if nothing usable is found, or if
    no soup/html_path was given at all); a bare name ('Jost') -> that name at the 3
    standard weights; an explicit 'Family:wght@...' -> kept as typed."""
    raw = (raw or "").strip()
    if raw:
        return font_param_from_name(raw)
    if soup is not None and html_path is not None:
        detected = detect_site_font(soup, html_path)
        if detected:
            return detected
    return random_preset_font_param()


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
        data = _fetch_url_bytes(url)
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


def inject_google_fonts(soup, fonts_param, html_path=None):
    fonts_param = normalize_font_family(fonts_param)
    head = soup.find("head")
    if not head:
        return

    for tag in head.find_all(attrs={"data-site-studio-font": True}):
        tag.decompose()

    # Any OLD font connection still left in <head> - not just ones this tool added
    # before - gets dropped too, so the page never ends up double-loading two font
    # services at once (the original theme's own Google Fonts/Typekit/etc <link>,
    # which clean_stylesheet_links leaves alone because fonts.googleapis.com/
    # fonts.gstatic.com are "library" domains, plus any leftover preconnect hints).
    # This also covers a LOCAL mirror - wayback often saves the Google Fonts CSS
    # *response* itself as a local file (named just "css", no extension - the source
    # URL is "fonts.googleapis.com/css?family=..." with no ".css" in the path) and
    # rewrites the <link> to point at that local copy instead of the live URL, so a
    # plain href substring check for "fonts.googleapis.com" never catches it.
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
        # nothing. Retry with a known-good preset instead, so the page still gets a real,
        # self-hosted webfont.
        for preset in PRESET_FONTS:
            cand = normalize_font_family(f"{preset}:wght@{FONT_WEIGHTS}")
            local_css = _try_selfhost(cand)
            if local_css:
                fonts_param = cand
                primary = _primary_font_family(cand)
                break

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


def promote_src(soup):
    for tag in soup.find_all(["img", "source", "video", "audio"]):
        if not tag.get("src") and tag.get("data-src"):
            tag["src"] = tag["data-src"]


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
_ARCHIVE_FETCH_LOCK = threading.Semaphore(1)

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


def _fetch_url_bytes(url, timeout=RECOVERY_FETCH_TIMEOUT, retries=RECOVERY_FETCH_RETRIES):
    import time
    import urllib.request

    # Archive fetches are SERIALIZED (see _ARCHIVE_FETCH_LOCK), so every wasted request costs the
    # whole run ~1.5s on a good day and the full timeout when throttled. The same dead URL gets
    # asked for again and again (the same missing sprite referenced from several stylesheets, the
    # same font from every page), so remember what already failed and fail those instantly. This
    # is the single biggest cleanup speed-up on asset-heavy sites - no behaviour is lost, we just
    # stop re-asking for things we already know aren't there.
    with _FETCH_FAIL_LOCK:
        if url in _FETCH_FAILED:
            raise _FETCH_FAILED[url]

    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (image-recovery-bot)"})
    use_lock = "archive.org" in url  # only the throttle-sensitive archive host is serialized
    last_err = None
    for attempt in range(retries + 1):
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
        except Exception as e:  # noqa: BLE001 - retry any transient network/HTTP failure
            last_err = e
            if attempt < retries:
                time.sleep(0.5)
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
        rows = json.loads(_fetch_url_bytes(api).decode("utf-8", "replace"))
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


def recover_asset_bytes(original_url, timestamp=None):
    """Try to fetch the real bytes of `original_url` from the Wayback Machine - the
    exact-timestamp raw fetch first if a timestamp hint is available, then the CDX
    best-match snapshot of that same URL either way. Returns (bytes, ext) on success,
    or (None, ext) if nothing could be recovered. Shared by the CSS asset recovery
    below and the broken-resource auto-cleanup in site_edit.py."""
    ext = Path(urlsplit(original_url).path).suffix.lower()
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
            return data, ext
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
    name = Path(urlsplit(original_url).path).name or "asset"
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

    print(f"[css-recovery] fetching {original_url} (referenced in {css_path.name})...")
    data, _ext = recover_asset_bytes(original_url, timestamp)
    if data is not None:
        print(f"[css-recovery] ok: {original_url}")
        assets_dir = css_path.parent / f"{css_path.stem}_recovered"
        assets_dir.mkdir(parents=True, exist_ok=True)
        dest = assets_dir / name
        i = 1
        while dest.exists() and dest.read_bytes() != data:
            dest = assets_dir / f"{Path(name).stem}_{i}{Path(name).suffix}"
            i += 1
        if not dest.exists():
            dest.write_bytes(data)
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
            def _sub_css_url(m):
                _raise_if_cancelled(cancelled)  # a big framework CSS = many url() fetches; stay abortable per-url
                quote, raw_url = m.group(1), m.group(2)
                new_ref = _recover_css_asset(raw_url, path, report, site_domain)
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

    def _localize(url):
        """(new_ref, dead) for a single ref - only touches same-domain absolute URLs."""
        u = unwayback((url or "").strip())
        if not u or not (is_external(u) and matches_suffix(domain_of(u), {bare})):
            return url, False  # not a same-domain absolute ref - leave it
        name = Path(urlsplit(u.split("?")[0].split("#")[0]).path).name or "asset"
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
                dest = recovered_dir / name
                dest.write_bytes(data)
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
        data = _fetch_url_bytes(url)
        if not data:
            report.external_media_removed.append(f"{url} (unreachable)")
            tag.decompose()
            continue
        name = re.sub(r"[^\w.\-]+", "_", urlsplit(url).path.rsplit("/", 1)[-1] or "img")[:80]
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
        parts = urlsplit(href)
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
    return " ".join(el.get("class", [])) if hasattr(el, "get") else ""


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
    if root.find("header") is not None:
        return None
    children = [c for c in root.find_all(recursive=False) if getattr(c, "name", None)]
    for ch in children[:4]:
        cls = _sem_cls(ch)
        if _SEM_HERO_RE.search(cls):
            continue  # a hero/intro/banner is content - skip past it, keep looking for the nav
        is_nav = ch.name == "nav" or bool(_SEM_NAVBAR_RE.search(cls)) or ch.find("nav") is not None
        if not is_nav:
            # hit real content (a heading-bearing block) before any nav -> this page has no top
            # nav bar to promote; stop rather than reaching deep down the page.
            if ch.name in _SEM_RENAMEABLE and ch.find(_SEM_HEADINGS):
                break
            continue
        if ch.name == "nav":
            # a bare top <nav> -> wrap it (plus an immediately-preceding logo-only sibling) in
            # a fresh <header>, so the menu bar sits inside the page header where it belongs.
            header = soup.new_tag("header")
            prev = ch.find_previous_sibling(lambda t: getattr(t, "name", None) is not None)
            ch.insert_before(header)
            if (prev is not None and prev.name in ("a", "div")
                    and prev.find("img") and not prev.get_text(strip=True)):
                header.append(prev.extract())
            header.append(ch.extract())
            return "nav"
        # a navbar-classed container (or any block wrapping a <nav>) -> rename it to <header>,
        # and make sure its inner link list is a <nav>.
        old = ch.name
        ch.name = "header"
        if ch.find("nav") is None:
            for sub in ch.find_all("div", recursive=True):
                if len([a for a in sub.find_all("a") if a.get_text(strip=True)]) >= 2:
                    sub.name = "nav"
                    break
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
    root = body.find("main") or body

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
):
    # Install this run's cancel hook for the whole thread, so even the low-level network fetches
    # (font/icon downloads, archive recovery) abort promptly - not just the phase boundaries.
    # Overwritten at the start of every call, so parallel cleanups (each its own thread) and
    # repeated CLI calls never see a stale hook.
    _set_cancel_check(cancelled)

    original_text = read_text_safe(html_path)
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
    # AI (Haiku) tag-semantics: promote top-level <div> soup into HTML5 landmarks, so the menu/
    # section logic below (and the final markup) sees real header/nav/main/section/footer. Skipped
    # in dry-run (no LLM spend on a preview) and whenever no ANTHROPIC_API_KEY is configured.
    if not dry_run:
        apply_semantic_tags(soup, report)
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
    _reorder_head_seo(soup)  # head order: <title> -> <meta description> -> <link canonical> -> fonts
    check_internal_link_targets(soup, html_path, report)
    strip_empty_style_declarations(soup)

    _p(82, "Записываю страницу")
    new_text = collapse_blank_lines(str(soup))

    print(f"\n### {html_path} (site domain detected: {site_domain or 'unknown'}) ###")
    print(report.render())

    if dry_run:
        print("[dry-run] no files written")
        return report.render()

    if backup:
        html_path.with_suffix(html_path.suffix + ".bak").write_text(original_text, encoding="utf-8")
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

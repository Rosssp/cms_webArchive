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
  - removes mailto:/tel: links entirely and redacts stray email addresses / phone
    numbers left in visible text or attributes (title/alt/content/placeholder/
    aria-label/value). Disable with --keep-contact-info.
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
    --keep-contact-info                      Do not strip mailto:/tel:/email/phone.
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
import random
import re
import shutil
import subprocess
import sys
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
# ----------------------------------------------------------
# 5) Сжатие gzip (HTML, CSS, JS, шрифты и т.п.)
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
# 6) Перезапись .js -> .js.gz, если доступно
# ----------------------------------------------------------
<IfModule mod_rewrite.c>
    RewriteEngine On
    RewriteCond %{HTTP:Accept-encoding} gzip
    RewriteCond %{REQUEST_FILENAME}.gz -f
    RewriteRule ^(.*)\\.js$ $1\\.js\\.gz [QSA,L]
</IfModule>

# ----------------------------------------------------------
# 7) Правильные заголовки для .js.gz
# ----------------------------------------------------------
<IfModule mod_headers.c>
    <FilesMatch "\\.js\\.gz$">
        Header set Content-Encoding gzip
        Header set Content-Type "application/javascript"
    </FilesMatch>
</IfModule>

# ----------------------------------------------------------
# 8) Добавляем типы шрифтов и webp
# ----------------------------------------------------------
AddType font/woff2 .woff2
AddType font/woff .woff
AddType font/ttf .ttf
AddType font/otf .otf
AddType image/webp .webp
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


WAYBACK_PREFIX_RE = re.compile(r"(?:https?:)?//web\.archive\.org/web/\d{1,14}[a-zA-Z_]*/")
FONT_FAMILY_RE = re.compile(r"font-family\s*:\s*([^;{}]+)", re.IGNORECASE)
PRECLEAN_STRAY_TAG_RE = re.compile(r'<div id="yui3-css-stamp"[^>]*>\s*</div>', re.IGNORECASE)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
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
        self.removed_a_external = []
        self.removed_a_social = []
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
        self.removed_iframes = []
        self.flagged_adult = []
        self.flagged_garbage_text = []
        self.logo_candidates = []
        self.logo_replaced = 0
        self.brand_text_replaced = 0
        self.brand_body_replacements = 0
        self.removed_owner_traces = []
        self.owner_trace_year_updates = 0
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
        lines.append(f"social <a> links removed: {len(self.removed_a_social)}")
        for s in self.removed_a_social:
            lines.append(f"  - {s}")
        lines.append(f"other external <a> links removed: {len(self.removed_a_external)}")
        for s in self.removed_a_external[:50]:
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
        lines.append("")
        lines.append(self.verdict())
        return "\n".join(lines)

    def verdict(self):
        """PBN-restoration-checklist-style BLOCKER/MEDIUM/MINOR verdict."""
        blockers = []
        if self.removed_a_external or self.removed_a_social:
            blockers.append("внешние ссылки/соцсети были на странице (уже вычищены - проверь визуально)")
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


def recover_missing_local_images(soup, html_path, report, dry_run=False):
    """PBN checklist item 9/10: no missing key images. Wayback/Archivarix exports very
    often reference local image files that were never actually saved (Squarespace's
    responsive srcset only captures some sizes). The wayback-archived absolute URL is
    still sitting in data-image/data-src/src at this point in the pipeline (must run
    BEFORE unwayback_all_attrs/clean_data_and_event_attrs strip it) - use it to
    re-download the exact bytes that were live at capture time."""
    import urllib.error
    import urllib.request

    cache = {}
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
        try:
            if candidate not in cache:
                req = urllib.request.Request(candidate, headers={"User-Agent": "Mozilla/5.0 (image-recovery-bot)"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    cache[candidate] = resp.read()
            data = cache[candidate]
            if not _looks_like_image_bytes(data):
                report.failed_image_recovery.append(f"{src}: downloaded content isn't a valid image (got {candidate})")
                continue
            local_path.parent.mkdir(parents=True, exist_ok=True)
            local_path.write_bytes(data)
            report.recovered_images.append(src)
        except (urllib.error.URLError, OSError) as e:
            report.failed_image_recovery.append(f"{src}: {e}")


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


def clean_stylesheet_links(soup, report):
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
            if matches_suffix(d, FONT_SERVICE_DOMAINS) or d not in LIBRARY_DOMAINS:
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


def clean_head_styles(soup, report, html_path):
    head = soup.find("head")
    if not head:
        return
    kept_css_chunks = []
    for style_tag in head.find_all("style"):
        css_text = unwayback(style_tag.get_text())
        for m in FONT_FAMILY_RE.finditer(css_text):
            report.font_families_found.add(m.group(1).strip().strip('"\''))
        if "@font-face" in css_text or "use.typekit.net" in css_text:
            style_tag.decompose()
            continue
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


def resolve_font_input(raw):
    """UI helper: empty -> a random preset; a bare name ('Jost') -> that name at the 3
    standard weights; an explicit 'Family:wght@...' -> kept as typed."""
    raw = (raw or "").strip()
    if not raw:
        return random_preset_font_param()
    return font_param_from_name(raw)


def _primary_font_family(fonts_param):
    """"Jost:wght@400;700,Inter" -> "Jost" - the family name typed in, GF API params
    stripped off, first family only (that's the one meant to actually apply site-wide)."""
    first = fonts_param.split(",")[0].strip()
    return first.split(":")[0].strip()


def inject_google_fonts(soup, fonts_param):
    fonts_param = normalize_font_family(fonts_param)
    head = soup.find("head")
    if not head:
        return

    for tag in head.find_all(attrs={"data-site-studio-font": True}):
        tag.decompose()

    preconnect1 = soup.new_tag("link", rel="preconnect", href="https://fonts.googleapis.com")
    preconnect1["data-site-studio-font"] = "link"
    preconnect2 = soup.new_tag("link", rel="preconnect", href="https://fonts.gstatic.com")
    preconnect2["crossorigin"] = ""
    preconnect2["data-site-studio-font"] = "link"
    families = "&".join(f"family={fam.strip()}" for fam in fonts_param.split(",") if fam.strip())
    font_link = soup.new_tag(
        "link", rel="stylesheet", href=f"https://fonts.googleapis.com/css2?{families}&display=swap"
    )
    font_link["data-site-studio-font"] = "link"
    head.append(preconnect1)
    head.append(preconnect2)
    head.append(font_link)

    # Loading the font isn't enough - the site's own CSS still references whatever
    # font-family the original theme used, so nothing would actually render in the
    # new font without a global override.
    primary = _primary_font_family(fonts_param)
    if primary:
        force_style = soup.new_tag("style")
        force_style["data-site-studio-font"] = "force"
        force_style.string = f"* {{ font-family: '{primary}', sans-serif !important; }}"
        head.append(force_style)


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


def clean_data_and_event_attrs(soup):
    for tag in soup.find_all(True):
        for attr in list(tag.attrs.keys()):
            if attr.startswith("on"):
                del tag[attr]
                continue
            if attr.startswith("data-"):
                if not any(hint in attr for hint in DATA_ATTR_KEEP_HINTS):
                    del tag[attr]


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


def clean_links_a(soup, site_domain, report):
    for a in soup.find_all("a", href=True):
        raw_href = a["href"]
        href = unwayback(raw_href)
        if href.startswith(("mailto:", "tel:")):
            report.removed_contact_links.append(href)
            _remove_a_and_empty_parent(a)
            continue
        if href.startswith(("#", "javascript:")) or not is_external(href):
            a["href"] = href
            report.kept_a_internal += 1
            continue
        d = domain_of(href)
        if site_domain and matches_suffix(d, {site_domain}):
            a["href"] = to_relative(href, site_domain)
            report.kept_a_internal += 1
            continue
        if matches_suffix(d, SOCIAL_DOMAINS):
            report.removed_a_social.append(href)
            _remove_a_and_empty_parent(a)
            continue
        if d in LIBRARY_DOMAINS:
            a["href"] = href
            report.kept_a_internal += 1
            continue
        report.removed_a_external.append(href)
        _remove_a_and_empty_parent(a)


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


def remove_unused_local_assets(html_path, cleaned_html_text, report, dry_run=False, extra_texts=None):
    """Any file left in the assets folder that the final cleaned HTML - or any of its
    linked local stylesheets (extra_texts), e.g. a CSS background-image - no longer
    references anywhere (old responsive-size duplicates, orphaned CMS bundles not on
    the wayback-specific list, leftovers from earlier experiments, ...) is unused
    clutter - quarantine it into _unused_removed rather than deleting outright."""
    assets_dir = html_path.with_name(html_path.stem + "_files")
    if not assets_dir.is_dir():
        return
    haystack = cleaned_html_text + "\n" + "\n".join(extra_texts or [])
    trash_dir = assets_dir / "_unused_removed"
    quarantine_dirs = (trash_dir, assets_dir / "_wayback_removed")
    for f in sorted(assets_dir.rglob("*")):
        if not f.is_file() or any(q in f.parents for q in quarantine_dirs):
            continue
        if f.name in haystack:
            continue
        rel = f.relative_to(assets_dir)
        report.removed_unused_assets.append(str(rel))
        if not dry_run:
            dest = trash_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(f), str(dest))


def clean_local_css_files(html_path, soup):
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href", "")
        if is_external(href):
            continue
        css_path = (html_path.parent / href).resolve()
        if css_path.is_file() and css_path.suffix == ".css":
            text = read_text_safe(css_path)
            new_text = unwayback(text)
            if new_text != text:
                css_path.write_text(new_text, encoding="utf-8")


def clean_iframes(soup, report):
    """PBN checklist item 24/25: no iframes/embeds to external resources, at all."""
    for iframe in soup.find_all("iframe"):
        src = unwayback(iframe.get("src", ""))
        if src and is_external(src) and domain_of(src) not in IFRAME_LIBRARY_DOMAINS:
            report.removed_iframes.append(src)
            iframe.decompose()


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
COPYRIGHT_YEAR_RE = re.compile(r"(©|copyright)\s*\d{4}(?:\s*[-–—]\s*\d{4})?", re.IGNORECASE)


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
            new_val, n = COPYRIGHT_YEAR_RE.subn(lambda m: f"{m.group(1)} {current}", str(node))
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


def ensure_htaccess(html_path, report, dry_run=False, overwrite=False):
    """Every restored PBN site gets the same standard .htaccess (gzip compression,
    .js.gz serving, font/webp mime types). Written unless one is already present, or
    always (overwrite=True, the cleanup pass) - the block is a fixed standard, so
    replacing a leftover export .htaccess with it is the intended behaviour."""
    site_root = html_path.parent
    htaccess_path = site_root / ".htaccess"
    report.htaccess_present = htaccess_path.is_file()
    if dry_run or (report.htaccess_present and not overwrite):
        return
    htaccess_path.write_text(DEFAULT_HTACCESS, encoding="utf-8")
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
    loc = f"https://{site_domain}/" if rel == "index.html" else f"https://{site_domain}/{rel}"

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
            if is_external(target) and not matches_suffix(domain_of(target), {site_domain} if site_domain else set()):
                report.external_redirect_found = target
        meta.decompose()

    for script in soup.find_all("script"):
        text = script.string or script.get_text() or ""
        m = JS_REDIRECT_RE.search(text)
        if m:
            target = unwayback(m.group(1))
            if is_external(target) and not matches_suffix(domain_of(target), {site_domain} if site_domain else set()):
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


def clean_html_file(
    html_path,
    fonts_param,
    dry_run,
    backup,
    keep_contact_info=False,
    logo_image=None,
    brand_text=None,
    favicon=None,
    recover_images=True,
    domain_override=None,
    auto_logo=False,
    auto_logo_color=None,
    brand_old_name=None,
):
    original_text = read_text_safe(html_path)
    # A stray element between <html> and <head> (e.g. wayback/YUI's
    # <div id="yui3-css-stamp">) makes some parsers misplace <head>'s
    # content into <body> - strip it before parsing.
    preclean_text = PRECLEAN_STRAY_TAG_RE.sub("", original_text)
    soup = BeautifulSoup(preclean_text, PARSER)
    normalize_charset_meta(soup)
    report = Report()

    content_domain = get_site_domain(soup)
    site_domain = domain_override or _domain_from_folder_name(html_path) or content_domain
    old_domain = None
    if content_domain and site_domain:
        bare_content = content_domain[4:] if content_domain.lower().startswith("www.") else content_domain
        bare_site = site_domain[4:] if site_domain.lower().startswith("www.") else site_domain
        if bare_content.lower() != bare_site.lower():
            old_domain = content_domain

    if recover_images:
        recover_missing_local_images(soup, html_path, report, dry_run=dry_run)

    check_and_fix_redirect(soup, site_domain, report)
    check_and_fix_noindex(soup, report)

    strip_wayback_comment_and_html_attrs(soup)
    strip_wayback_toolbar(soup)
    unwayback_all_attrs(soup)
    clean_scripts(soup, report)
    clean_stylesheet_links(soup, report)
    strip_cms_meta_links(soup, report)
    strip_owner_traces(soup, update_year=True, report=report)
    clean_head_styles(soup, report, html_path)
    promote_src(soup)
    add_lazy_loading(soup, report)
    clean_data_and_event_attrs(soup)
    inject_google_fonts(soup, fonts_param)
    inject_image_object_fit_style(soup)
    clean_links_a(soup, site_domain, report)
    if not keep_contact_info:
        strip_contact_info(soup, report, old_domain=old_domain)
    clean_iframes(soup, report)
    scan_content_flags(soup, report)
    detect_and_report_logo(soup, report)
    effective_brand = brand_text
    if auto_logo:
        derived = apply_auto_logo(
            soup, html_path, site_domain, report, brand_text=brand_text, dry_run=dry_run,
            color=parse_color(auto_logo_color),
        )
        effective_brand = brand_text or derived or _brand_name_from_domain(site_domain) or "Site"
        apply_brand_text(soup, effective_brand, report)
    else:
        if logo_image:
            apply_logo_image(soup, html_path, logo_image, report, dry_run=dry_run)
        if brand_text:
            apply_brand_text(soup, brand_text, report)
    # Sweep the OLD brand name out of the running body text/attrs too (headings, footer,
    # alts, ...) - apply_brand_text above only touches the logo/title/social-meta spots.
    if brand_old_name:
        new_brand = effective_brand or brand_text
        if new_brand:
            replace_brand_in_text(soup, brand_old_name, new_brand, report)
    favicon_brand_hint = effective_brand or (site_domain.split(".")[0] if site_domain else None)
    ensure_favicon(soup, html_path, favicon, report, dry_run=dry_run, brand_hint=favicon_brand_hint)
    ensure_local_seo_files(html_path, site_domain, report, dry_run=dry_run, overwrite=True)
    ensure_htaccess(html_path, report, dry_run=dry_run, overwrite=True)
    ensure_canonical(soup, html_path, site_domain, report, dry_run=dry_run)
    check_internal_link_targets(soup, html_path, report)

    new_text = str(soup)

    print(f"\n### {html_path} (site domain detected: {site_domain or 'unknown'}) ###")
    print(report.render())

    if dry_run:
        print("[dry-run] no files written")
        return

    if backup:
        html_path.with_suffix(html_path.suffix + ".bak").write_text(original_text, encoding="utf-8")
    html_path.write_text(new_text, encoding="utf-8")

    clean_local_css_files(html_path, soup)
    move_orphaned_wayback_assets(html_path, new_text, report)
    local_css_texts = []
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href", "")
        if href and not is_external(href):
            css_path = (html_path.parent / href).resolve()
            if css_path.is_file() and css_path.suffix.lower() == ".css":
                local_css_texts.append(read_text_safe(css_path))
    remove_unused_local_assets(html_path, new_text, report, dry_run=dry_run, extra_texts=local_css_texts)

    report_path = html_path.with_name(html_path.name + ".cleanup-report.txt")
    report_path.write_text(report.render(), encoding="utf-8")


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
    parser.add_argument("--fonts", default=DEFAULT_GOOGLE_FONTS, help="Google Fonts families to link in")
    parser.add_argument("--logo-image", help="Replace every detected logo image with this file")
    parser.add_argument("--brand-text", help="Replace every detected text-logo/title/meta with this string")
    parser.add_argument(
        "--brand-old",
        help="Old brand/company name to sweep out of the running body text and alt/title/aria "
        "attributes as well (replaced with --brand-text). apply_brand_text only touches the "
        "logo/title/social-meta; this reaches headings, footer (©...), image alts, etc.",
    )
    parser.add_argument(
        "--auto-logo",
        action="store_true",
        help=(
            "Don't ask for a logo file - remove the detected image logo and generate a text "
            "wordmark instead, named after the domain (or --brand-text if given) and auto-sized "
            "to fit the box the original logo occupied. Overrides --logo-image."
        ),
    )
    parser.add_argument(
        "--auto-logo-color",
        help="Color for --auto-logo's wordmark: 'white', 'black', '#rrggbb', or 'r,g,b'. "
        "Default is a deterministic color derived from the name.",
    )
    parser.add_argument("--favicon", help="Copy this file in and set it as the favicon")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-backup", action="store_true")
    parser.add_argument(
        "--keep-contact-info",
        action="store_true",
        help="Do not strip mailto:/tel: links or redact email/phone text (removed by default)",
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
            keep_contact_info=args.keep_contact_info,
            logo_image=args.logo_image,
            brand_text=args.brand_text,
            favicon=args.favicon,
            recover_images=not args.no_image_recovery,
            domain_override=args.domain,
            auto_logo=args.auto_logo,
            auto_logo_color=args.auto_logo_color,
            brand_old_name=args.brand_old,
        )


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-
"""
wayback_download.py
===================

Download a full page from a Wayback Machine URL into a local site folder the cleaner
(clean_wayback_site.py) can then process:

  - loads the archived page in a real headless browser (Playwright),
  - scrolls it all the way to the bottom so lazy-loaded content/images actually load,
  - captures every asset the browser fetched (css/js/img/font/media) and saves them
    locally, rewriting the page's references to those local files,
  - writes index.html + index_files/ into <dest>/<domain>/.

The result is a normal "wayback export" folder - the cleaner unwraps whatever's left,
strips the toolbar and recovers anything still missing.
"""

import re
import sys
from pathlib import Path
from urllib.parse import urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clean_wayback_site as cw  # noqa: E402
import image_providers  # noqa: E402

_WB_RE = re.compile(r"/web/(\d+)[a-z_]*/(https?://.+)$", re.I)
_ASSET_TYPES = {"stylesheet", "image", "font", "script", "media"}


def _original_url(archive_url):
    m = _WB_RE.search(archive_url or "")
    return m.group(2) if m else (archive_url or "")


def _sanitize_name(url):
    path = urlsplit(cw.unwayback(url)).path
    name = unquote(Path(path).name) or "asset"
    name = re.sub(r"[^\w.\-]+", "_", name)[:120].strip("._") or "asset"
    return name


_WB_STRIP_JS = r"""
() => {
  const rm = el => { if (el) el.remove(); };

  // 1) The Wayback toolbar itself (the black bar) + its print/stamp siblings - exactly as if
  //    the user had clicked its close (X) button.
  ['wm-ipp-base','wm-ipp-print','wm-ipp','yui3-css-stamp','donato'].forEach(id => rm(document.getElementById(id)));

  // 2) #topnav is archive.org's top bar HERE, but some real sites also use that id - only drop
  //    it when it actually holds archive.org navigation. MUST run before step 3, which removes
  //    the media-menu/primary-nav children this check looks for.
  const topnav = document.getElementById('topnav');
  if (topnav && topnav.querySelector('primary-nav, media-menu, a[href*="archive.org"]')) rm(topnav);

  // 3) archive.org's OWN site chrome, pulled into the live DOM by its runtime header JS
  //    (custom elements + banner wrappers). These tag/class names are archive.org-universal
  //    and never appear in a real restored site, so they're safe to remove wholesale.
  document.querySelectorAll(
    'primary-nav, media-menu, media-subnav, ia-topnav, ia-banner, ia-banners,' +
    ' .ia-banners, .ia-topnav, #ia-banners, #wayback-fixed-toolbar'
  ).forEach(rm);

  // 4) archive.org scripts/stylesheets.
  document.querySelectorAll('script[src], link[href]').forEach(n => {
    const u = (n.getAttribute('src') || n.getAttribute('href') || '').toLowerCase();
    if (u.includes('archive.org') || /wombat|bundle-playback|athena|ruffle|wm\.js/.test(u)) n.remove();
  });
  document.querySelectorAll('script:not([src])').forEach(n => {
    if (/__wm\.|wombat|wmipp|archive_analytics/i.test(n.textContent || '')) n.remove();
  });

  // 5) Safety net: any stray anchor into archive.org's site chrome (details/web/upload/etc.).
  document.querySelectorAll(
    'a[href*="archive.org/details"], a[href*="archive.org/web/"],' +
    ' a[href="https://archive.org/"], a[href="https://web.archive.org"]'
  ).forEach(a => rm(a));
}
"""


def _strip_wayback_dom(page):
    try:
        page.evaluate(_WB_STRIP_JS)
    except Exception:
        pass  # best-effort; the cleaner strips the toolbar again later anyway


def _scroll_to_bottom(page):
    """Scroll the page to the very bottom in steps, waiting between each, until its height
    stops growing - so infinite-scroll / lazy-loaded images/sections actually load."""
    prev = -1
    for _ in range(40):
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(500)
        try:
            h = page.evaluate("() => document.body.scrollHeight")
        except Exception:
            break
        if h == prev:
            break
        prev = h
    page.evaluate("window.scrollTo(0, 0)")
    page.wait_for_timeout(400)


def download_wayback_site(archive_url, dest_root, progress=None):
    """Download `archive_url` (a web.archive.org page URL) into <dest_root>/<domain>/. Calls
    progress(percent, message) as it goes. Returns {'site_dir', 'domain'}."""
    def _p(pct, msg):
        if progress:
            progress(int(pct), msg)

    image_providers._ensure_playwright()
    from playwright.sync_api import sync_playwright

    original = _original_url(archive_url)
    domain = cw.domain_of(original) or "site"
    site_dir = Path(dest_root) / domain
    files_dir = site_dir / "index_files"
    files_dir.mkdir(parents=True, exist_ok=True)

    responses = []
    _p(5, "Открываю браузер")
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.on("response", lambda r: responses.append(r))
            _p(15, "Загружаю страницу архива")
            try:
                page.goto(archive_url, wait_until="networkidle", timeout=90000)
            except Exception:
                pass  # networkidle can time out on chatty archive pages - keep what loaded
            _p(45, "Прокручиваю страницу до конца")
            _scroll_to_bottom(page)
            page.wait_for_timeout(1200)
            _strip_wayback_dom(page)  # remove the wayback toolbar/runtime before shot + save
            try:
                page.screenshot(path=str(site_dir / "_preview.png"))  # thumbnail for the card
            except Exception:
                pass
            _p(62, "Снимаю готовую страницу")
            html = page.content()
            _p(70, "Собираю ассеты")
            assets = {}
            for r in responses:
                try:
                    if r.status == 200 and r.request.resource_type in _ASSET_TYPES:
                        assets[r.url] = r.body()
                except Exception:
                    pass
        finally:
            browser.close()

    _p(80, "Сохраняю ассеты локально")
    url_to_local, used = {}, set()
    for url, data in assets.items():
        if not data:
            continue
        name = _sanitize_name(url)
        stem, dot, ext = name.rpartition(".")
        cand, i = name, 1
        while cand in used:
            cand = (f"{stem}_{i}.{ext}" if dot else f"{name}_{i}")
            i += 1
        used.add(cand)
        (files_dir / cand).write_bytes(data)
        rel = f"index_files/{cand}"
        url_to_local[url] = rel
        unwrapped = cw.unwayback(url)
        if unwrapped != url:
            url_to_local[unwrapped] = rel

    _p(90, "Переписываю ссылки на локальные")
    for url in sorted(url_to_local, key=len, reverse=True):
        html = html.replace(url, url_to_local[url])

    (site_dir / "index.html").write_text(html, encoding="utf-8")
    _p(100, "Готово")
    return {"site_dir": str(site_dir), "domain": domain}

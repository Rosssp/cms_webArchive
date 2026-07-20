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
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlsplit, unquote

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import clean_wayback_site as cw
from clean_wayback_site import safe_urlsplit, safe_urljoin  # noqa: E402
import image_providers  # noqa: E402

_CSS_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+?)['\"]?\s*\)", re.I)
# web.archive.org throttles hard when stampeded (a 1.5s fetch turns into a 10s timeout), so keep
# concurrency low and polite - 4 is ~4x faster than serial while staying under the throttle.
_CSS_FETCH_WORKERS = 4
_CSS_FETCH_TIMEOUT = 8

_WB_RE = re.compile(r"/web/(\d+)[a-z_]*/(https?://.+)$", re.I)
_ASSET_TYPES = {"stylesheet", "image", "font", "script", "media"}
_ASSET_EXT_RE = re.compile(
    r"\.(?:css|js|png|jpe?g|gif|svg|woff2?|ttf|otf|eot|ico|webp|avif|mp4|webm|bmp)$", re.I)


def _original_url(archive_url):
    m = _WB_RE.search(archive_url or "")
    return m.group(2) if m else (archive_url or "")


def _sanitize_name(url):
    path = safe_urlsplit(cw.unwayback(url)).path
    name = unquote(Path(path).name) or "asset"
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "asset"
    # Cap the length. Blogspot/Google asset names are 100+ char hashes; combined with a deep
    # download folder they blow past Windows' 260-char MAX_PATH and the write CRASHES the whole
    # download (puratoni/mikatoronen). Keep a short readable stem + an md5 tag so distinct long
    # names stay distinct, and preserve the extension.
    if len(name) > 64:
        import hashlib
        stem, dot, ext = name.rpartition(".")
        if not dot:
            stem, ext = name, ""
        tag = hashlib.md5(name.encode("utf-8")).hexdigest()[:8]
        name = f"{stem[:40]}_{tag}" + (f".{ext[:8]}" if ext else "")
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

  // 5) Safety net: stray anchors into archive.org's OWN site chrome (details pages, its home).
  //    CAREFUL: do NOT match the generic "archive.org/web/" - in the live archive DOM EVERY
  //    internal site link is wrapped as web.archive.org/web/<ts>/http://thesite/... , so that
  //    pattern would delete the site's whole menu + all its internal links (leaving empty <li>).
  //    The toolbar (removed in steps 1-3) already took its date-nav links with it; the wrapped
  //    site links are unwrapped to real relative links by the cleaner later, so keep them.
  document.querySelectorAll(
    'a[href*="archive.org/details"], a[href*="archive.org/account/"],' +
    ' a[href="https://archive.org/"], a[href="https://web.archive.org/"], a[href="https://web.archive.org"]'
  ).forEach(a => rm(a));
}
"""


def _strip_wayback_dom(page):
    try:
        page.evaluate(_WB_STRIP_JS)
    except Exception:
        pass  # best-effort; the cleaner strips the toolbar again later anyway


def _has_real_content(html):
    """True if `html` is a genuinely loaded page, not an empty/redirect interstitial. A blank
    archive result is ~39 bytes (`<html><head></head><body></body></html>`); a real page carries
    lots of markup. Guards the download from silently saving an empty 'site'."""
    return bool(html) and len(html) > 1200 and html.count("<") > 15


def _scroll_to_bottom(page):
    """Scroll the page to the very bottom in steps, waiting between each, until its height
    stops growing - so infinite-scroll / lazy-loaded images/sections actually load.

    Every page.evaluate here is wrapped: an archived page often JS-redirects or the archive
    itself navigates to the canonical snapshot mid-scroll, which destroys the JS execution
    context ("Execution context was destroyed, most likely because of a navigation"). That must
    NOT kill the whole download - we just stop scrolling and keep whatever loaded."""
    prev = -1
    for _ in range(40):
        try:
            page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        except Exception:
            break  # navigated/reloaded mid-scroll - stop, keep what we have
        page.wait_for_timeout(500)
        try:
            h = page.evaluate("() => document.body.scrollHeight")
        except Exception:
            break
        if h == prev:
            break
        prev = h
    try:
        page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass
    page.wait_for_timeout(400)


def _fetch_one(url, retries=1):
    """One archive asset. Retries once - a throttled archive drops the first connection ("SSL:
    UNEXPECTED_EOF" / timeout) but usually answers the retry."""
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                              "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            })
            with urllib.request.urlopen(req, timeout=_CSS_FETCH_TIMEOUT) as r:
                if r.status == 200:
                    return r.read()
            return None
        except Exception:  # noqa: BLE001 - a missing asset is normal; just skip it
            if attempt >= retries:
                return None
            time.sleep(0.4)
    return None


def _fetch_css_assets(files_dir, css_items, url_to_local, used, log=None):
    """Pull in the images a stylesheet references but the BROWSER never requested - :hover
    backgrounds, sprites on blocks that didn't render, etc. They're invisible to the response
    capture, so without this they're simply missing (menu backgrounds vanish).

    Crucially this must happen HERE, at download time: each url(...) is resolved against the
    stylesheet's OWN archive URL, which we still know. Once everything is flattened into
    index_files/ that context is gone, and the cleaner's later recovery can't reconstruct the
    original URL at all - it just burns minutes on 404s. Fetched in parallel; each stylesheet is
    rewritten to reference the local file by bare name (CSS and images share index_files/)."""
    todo, per_css = {}, {}
    for css_url, css_path in css_items:
        try:
            text = css_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        refs = []
        for m in _CSS_URL_RE.finditer(text):
            ref = (m.group(1) or "").strip()
            if not ref or ref.startswith(("data:", "#", "http://", "https://", "//")):
                continue
            abs_url = safe_urljoin(css_url, ref)
            refs.append((ref, abs_url))
            if abs_url not in url_to_local:
                todo[abs_url] = None
        if refs:
            per_css[css_path] = (text, refs)
    if todo:
        urls = list(todo)
        with ThreadPoolExecutor(max_workers=_CSS_FETCH_WORKERS) as ex:
            for url, data in zip(urls, ex.map(_fetch_one, urls)):
                todo[url] = data
        for url, data in todo.items():
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
            url_to_local[url] = f"index_files/{cand}"
        if log:
            log(f"CSS assets: {sum(1 for v in todo.values() if v)}/{len(todo)} recovered")
    # rewrite every stylesheet to point at the local copies (bare name - same folder)
    for css_path, (text, refs) in per_css.items():
        new = text
        for ref, abs_url in refs:
            local = url_to_local.get(abs_url)
            if local:
                new = new.replace(ref, local.split("/")[-1])
        if new != text:
            try:
                css_path.write_text(new, encoding="utf-8")
            except OSError:
                pass


def download_wayback_site(archive_url, dest_root, progress=None, use_www=False):
    """Download `archive_url` (a web.archive.org page URL) into <dest_root>/<domain>/. Calls
    progress(percent, message) as it goes. Returns {'site_dir', 'domain'}. The folder name IS
    the canonical-domain switch downstream (canonical/robots/sitemap/.htaccess all follow it):
    use_www=True names it 'www.<domain>' so the whole restored site becomes www-canonical."""
    def _p(pct, msg):
        if progress:
            progress(int(pct), msg)

    image_providers._ensure_playwright()
    from playwright.sync_api import sync_playwright

    original = _original_url(archive_url)
    domain = cw.domain_of(original) or "site"  # domain_of() always strips a leading www.
    if use_www and domain != "site" and not domain.startswith("www."):
        domain = "www." + domain
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
            # An archive snapshot often redirects (http->https, to the canonical timestamp, or the
            # page JS-redirects) - the browser can end up on a BLANK/interstitial page and we grab a
            # ~39-byte empty document. Retry the whole load until we actually have real content, so a
            # flaky redirect doesn't silently produce an empty "site". `responses` accumulates across
            # attempts, so assets captured on any try are kept.
            html, _best = "", 0

            def _consider(h):
                # Keep the LARGEST real content seen. Grabbing both before AND after the scroll
                # matters: on an image-heavy gallery (mikatoronen) the scroll can trigger a
                # navigation that empties the DOM, so the pre-scroll grab is the one that survives.
                nonlocal html, _best
                if _has_real_content(h) and len(h) > _best:
                    html, _best = h, len(h)

            for attempt in range(5):
                try:
                    page.goto(archive_url, wait_until="domcontentloaded", timeout=90000)
                except Exception:
                    pass
                try:
                    page.wait_for_load_state("networkidle", timeout=25000)
                except Exception:
                    pass  # chatty archive pages never go idle - keep what loaded
                try:
                    _consider(page.content())  # BEFORE scroll - survives a scroll-triggered navigation
                except Exception:
                    pass
                if attempt == 0:
                    _p(45, "Прокручиваю страницу до конца")
                try:
                    _scroll_to_bottom(page)
                except Exception:
                    pass  # a mid-scroll navigation must never abort the whole download
                page.wait_for_timeout(1000)
                try:
                    _consider(page.content())  # AFTER scroll - lazy content now loaded
                except Exception:
                    pass
                if _best:
                    break
                # Empty result - usually archive.org throttling under load returning an interstitial.
                # Back off progressively before re-loading (longer each attempt), so a busy archive
                # gets time to answer instead of us hammering it into more empties.
                page.wait_for_timeout(1500 + attempt * 1500)

            _strip_wayback_dom(page)  # remove the wayback toolbar/runtime before shot + save
            try:
                page.screenshot(path=str(site_dir / "_preview.png"))  # thumbnail for the card
            except Exception:
                pass
            _p(62, "Снимаю готовую страницу")
            try:
                stripped = page.content()  # PREFER the toolbar-stripped page if it's still real
                if _has_real_content(stripped):
                    html = stripped
            except Exception:
                pass
            if not _has_real_content(html):
                raise RuntimeError("архив вернул пустую страницу (снапшот редиректит/не открывается)")
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
    url_to_local, used, css_items = {}, set(), []
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
        try:
            (files_dir / cand).write_bytes(data)
        except OSError:
            continue  # a single un-writable asset (still-too-long path, bad name) never aborts the run
        rel = f"index_files/{cand}"
        url_to_local[url] = rel
        unwrapped = cw.unwayback(url)
        if unwrapped != url:
            url_to_local[unwrapped] = rel
        # The DOM frequently references an asset by the ROOT-RELATIVE wayback form (host stripped):
        #   <link href="/web/20180601184817cs_/https://site/css/x.css">
        # The response URL we keyed on is the ABSOLUTE archive URL, so that href never matched and
        # the stylesheet stayed pointing at a dead wayback path -> the file WAS saved but the page
        # rendered unstyled (ariyalur & co). Register the root-/protocol-relative forms too so the
        # rewrite below catches them.
        for host in ("https://web.archive.org", "http://web.archive.org", "//web.archive.org"):
            if url.startswith(host):
                url_to_local[url[len(host):]] = rel  # -> "/web/<ts>cs_/https://site/..."
                break
        if cand.lower().endswith(".css"):
            css_items.append((url, files_dir / cand))

    # Backgrounds/sprites the browser never asked for (hover states, hidden blocks) - fetch them
    # now, while we still know each stylesheet's original URL to resolve them against.
    _p(86, "Дотягиваю фоновые картинки из CSS")
    _fetch_css_assets(files_dir, css_items, url_to_local, used, log=lambda m: _p(86, m))

    # Assets the DOM references but the browser NEVER handed us (an old snapshot serving CSS via a
    # redirect/non-200, a lazily-requested file, ...) would otherwise stay pointing at a dead wayback
    # URL -> page renders unstyled. sylhet's own new_style.css was simply absent from the browser's
    # responses. Second pass: for every wayback-wrapped ASSET ref still in the HTML that we don't
    # have locally, fetch it directly and save it. Generic - closes the "browser didn't give us the
    # stylesheet/script" class for ANY archive.
    _p(88, "Дотягиваю недостающие стили/скрипты")
    _new_css = []
    for m in re.finditer(
        r'''(?:src|href)\s*=\s*["']((?:(?:https?:)?//web\.archive\.org)?/web/\d+[a-z_]*/[^"'<>]+)["']''', html):
        ref = m.group(1)
        if ref in url_to_local:
            continue
        base = ref.split("#")[0].split("?")[0].rsplit("/", 1)[-1]
        if not _ASSET_EXT_RE.search(base):
            continue  # a page link, not an asset - the cleaner unwraps it later
        abs_url = (ref if ref.startswith("http")
                   else "https:" + ref if ref.startswith("//") else "https://web.archive.org" + ref)
        data = _fetch_one(abs_url)
        if not data:
            continue
        name = _sanitize_name(abs_url)
        stem, dot, ext = name.rpartition(".")
        cand, i = name, 1
        while cand in used:
            cand = (f"{stem}_{i}.{ext}" if dot else f"{name}_{i}")
            i += 1
        used.add(cand)
        try:
            (files_dir / cand).write_bytes(data)
        except OSError:
            continue
        rel = f"index_files/{cand}"
        url_to_local[ref] = rel
        url_to_local[abs_url] = rel
        if cand.lower().endswith(".css"):
            _new_css.append((abs_url, files_dir / cand))
    # a directly-fetched stylesheet has its own url() backgrounds - pull those in too
    if _new_css:
        _fetch_css_assets(files_dir, _new_css, url_to_local, used, log=lambda m: _p(88, m))

    _p(90, "Переписываю ссылки на локальные")
    for url in sorted(url_to_local, key=len, reverse=True):
        html = html.replace(url, url_to_local[url])

    # When the DOM used the wayback-WRAPPED form of an asset URL but only its inner (unwrapped)
    # URL matched a saved asset, the replace above rewrites just that inner part and leaves the
    # wayback prefix in front of the now-local path, e.g.
    #   https://web.archive.org/web/20160216123517cs_/index_files/reset.css
    # That's a broken absolute URL - the local CSS/img never loads, so the whole page renders
    # unstyled. index_files/ is OUR local folder and never appears in the real archive, so any
    # wayback prefix sitting in front of it is always junk: strip it. (Fixes old table/WP themes
    # whose many stylesheets otherwise all fail -> blank page.)
    # The prefix comes in three flavours - absolute, protocol-relative and ROOT-relative:
    #   https://web.archive.org/web/<ts>cs_/index_files/reset.css
    #   //web.archive.org/web/<ts>/index_files/all.js
    #   /web/<ts>im_/index_files/headerSP.jpg      <- no host at all (the archive's own rewrite)
    # All three are junk in front of our local folder, and the last one silently broke the
    # header banner (the file WAS downloaded, the ref just pointed nowhere).
    html = re.sub(r"(?:(?:https?:)?//web\.archive\.org)?/web/\d+[a-z_]*/(?=index_files/)", "", html)

    # Robust fallback: ANY wayback-wrapped ref still left (root-relative like
    # "/web/<ts>cs_/https://site/css/alucss.css", a redirect whose final URL didn't match the DOM
    # href, etc.) is relinked to the local file BY BASENAME if we saved one. This is what actually
    # rescues stylesheets on sites where the URL-keyed rewrite missed - the .css WAS downloaded, the
    # <link> just still pointed at a dead wayback path, so the page rendered unstyled. Non-asset
    # page links whose basename we didn't save are left untouched (the cleaner unwraps them later).
    _saved_by_name = {}
    for p in files_dir.iterdir():
        if p.is_file():
            _saved_by_name.setdefault(p.name.lower(), p.name)

    def _relink(m):
        ref = m.group(0)
        base = ref.split("?")[0].split("#")[0].rstrip("/").rsplit("/", 1)[-1]
        real = _saved_by_name.get(base.lower())
        return f"index_files/{real}" if real else ref

    html = re.sub(r"(?:(?:https?:)?//web\.archive\.org)?/web/\d+[a-z_]*/[^\s\"'()>]+", _relink, html)

    (site_dir / "index.html").write_text(html, encoding="utf-8")
    _p(100, "Готово")
    return {"site_dir": str(site_dir), "domain": domain}

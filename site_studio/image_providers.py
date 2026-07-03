# -*- coding: utf-8 -*-
"""
image_providers.py
===================

Keyless image search for site_studio, via a real (headless) browser session
instead of raw HTTP scraping.

Google Images itself requires JS to render results and outright blocks headless
automation (redirects to a /sorry/ CAPTCHA wall - confirmed by testing, no header
tweak gets around it). Bing Images tolerates it, but only when the request looks
like a real browser: a bare urllib GET gets served random unrelated "decoy" content
under repeated use (confirmed by testing - a bicycle-themed Cyrillic query returned
NFT art, airplanes, Egyptian temples...). A genuine Playwright session - real JS
execution, cookies, a proper page load - gets treated as legitimate traffic and
returns real, relevant results.

Flow: find_candidates() launches a headless browser, searches Bing Images, and
returns a pool of candidate image URLs for the caller to show in a picker (no
downloading yet). download_images() then fetches only the URLs the user actually
picked.
"""

import importlib
import re
import subprocess
import sys
import urllib.parse
import urllib.request

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
_CYRILLIC_RE = re.compile(r"[Ѐ-ӿ]")


class ProviderError(Exception):
    pass


def _ensure_playwright():
    try:
        import playwright  # noqa: F401
    except ImportError:
        print("[setup] installing missing dependency: playwright ...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "playwright"])
        importlib.invalidate_caches()

    try:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            browser.close()
    except Exception:
        print("[setup] installing Playwright's Chromium browser (one-time, ~100MB)...")
        subprocess.check_call([sys.executable, "-m", "playwright", "install", "chromium"])


def find_candidates(query, limit=24):
    """Search Bing Images for `query` via a real headless browser session and
    return up to `limit` candidate image URLs, most-relevant first - nothing is
    downloaded yet, this is just for showing the user a picker."""
    query = (query or "").strip()
    if not query:
        raise ProviderError("query is required")

    _ensure_playwright()
    from playwright.sync_api import sync_playwright

    locale = "ru-RU" if _CYRILLIC_RE.search(query) else "en-US"
    urls = []
    seen = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            context = browser.new_context(user_agent=_UA, viewport={"width": 1600, "height": 1000}, locale=locale)
            page = context.new_page()
            params = urllib.parse.urlencode({"q": query, "form": "HDRSC2"})
            try:
                page.goto(f"https://www.bing.com/images/search?{params}", timeout=30000, wait_until="networkidle")
            except Exception as e:
                raise ProviderError(f"image search request failed: {e}")
            page.wait_for_timeout(800)

            for a in page.query_selector_all("a.iusc"):
                m_attr = a.get_attribute("m")
                if not m_attr:
                    continue
                try:
                    import json

                    data = json.loads(m_attr)
                except Exception:
                    continue
                murl = data.get("murl")
                if murl and murl not in seen and murl.startswith("http"):
                    seen.add(murl)
                    urls.append(murl)
                if len(urls) >= limit:
                    break
        finally:
            browser.close()

    if not urls:
        raise ProviderError(f"no image results for '{query}'")
    return urls


def _download(url):
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Referer": "https://www.bing.com/"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return resp.read()


def download_images(urls):
    """Download exactly the given URLs (as picked by the user), skipping any that
    fail (dead link, hotlink-blocked, timeout). Raises if none could be downloaded."""
    out = []
    for u in urls:
        try:
            out.append(_download(u))
        except Exception:
            continue
    if not out:
        raise ProviderError("none of the selected images could be downloaded (dead links / hotlink-blocked)")
    return out


def search_images(query, count):
    """Convenience wrapper: find candidates and download the top `count` that
    actually succeed - used where there's no picker UI in front of the caller."""
    candidates = find_candidates(query, limit=max(count * 4, 20))
    out = []
    for u in candidates:
        if len(out) >= count:
            break
        try:
            out.append(_download(u))
        except Exception:
            continue
    if not out:
        raise ProviderError(
            f"found {len(candidates)} result(s) for '{query}' but none could be downloaded "
            f"(dead links / hotlink-blocked sources) - try a different query"
        )
    return out

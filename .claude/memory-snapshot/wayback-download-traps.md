---
name: wayback-download-traps
description: "Two wayback_download.py bugs that broke asset URLs and deleted the site menu — don't reintroduce"
metadata: 
  node_type: memory
  type: project
  originSessionId: d2977795-0bd5-4502-a65e-166b52726951
---

`site_studio/wayback_download.py` had two bugs (fixed 2026-07-13) that together turned a normal old site (e.g. sanjhapunjab.net, a 2016 WordPress theme) into a blank, unstyled, menu-less page after download+clean. Don't reintroduce them:

1. **Partial URL rewrite left a wayback prefix.** The naive `html.replace(url, local)` matched only the inner (unwrapped) URL inside a wayback-WRAPPED attribute, leaving `https://web.archive.org/web/<ts>cs_/index_files/reset.css` — a broken absolute URL, so every CSS/img failed and the page rendered unstyled. Fix: after the replace loop, strip any wayback prefix sitting in front of a local path: `re.sub(r"(?:https?:)?//web\.archive\.org/web/\d+[a-z_]*/(?=index_files/)", "", html)`. `index_files/` is our local folder and never appears in the real archive, so the prefix is always junk.

2. **`_strip_wayback_dom` step 5 deleted the whole site menu.** It removed `a[href*="archive.org/web/"]` as a "safety net", but in the LIVE archive DOM EVERY internal link is wrapped as `web.archive.org/web/<ts>/http://thesite/...` — so it wiped the site's entire menu and internal links, leaving empty `<li>`s (which then made `_find_header_nav` fail → the cleaner generated a duplicate header). Fix: narrow step 5 to archive.org's OWN chrome only (`/details`, `/account/`, the archive home), NOT the generic `/web/`. The toolbar's own nav links are already gone with the toolbar (steps 1-3); wrapped site links get unwrapped by the cleaner later.

Symptom to recognize: cleaned site is blank/unstyled and/or the real menu vanished and a generated header appeared. Check the raw download FIRST (render `index.html.bak`) — if the raw is already broken, it's the download, not the cleaner.

Two more cleaner traps found on Blogger/CMS sites (2026-07-15), both in `clean_wayback_site.py`:
3. **`clean_head_styles` dropped a whole `<style>` block on any `@font-face`.** A Blogger skin puts ~56 @font-face rules INTO one 90KB block of layout CSS, so the page rendered unstyled. Fix: `_strip_font_loading_rules` removes only the @font-face/typekit rules, keeps the layout. When a cleaned site is unstyled, compare raw-vs-cleaned inline-CSS byte count first.
4. **30-min clean = `clean_local_linked_files` recovering webfonts from the archive.** A Google-Fonts mirror CSS (`all.min.css`) referenced 58 `.woff2` by bare name → 58 serialized CDX lookups at ~11s each (throttled). Fix: `_recover_css_asset` now skips ALL webfont exts (.woff2/.woff/.ttf/.otf/.eot) — the cleaner re-injects its own font with `!important` anyway, so the original never renders. 785s → 26s. To diagnose clean slowness: monkeypatch `_fetch_url_bytes` to record URLs (return None, no wait) and print a Counter of what it fetches. Related: [[nav-linking-rules]], [[generated-header-feature]], [[fix-script-not-file-restart-server]].

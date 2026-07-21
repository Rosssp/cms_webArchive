---
name: nav-linking-rules
description: Header/footer auto-linking rules in site_edit.auto_link_menu — hero link + footer=full sitemap
metadata: 
  node_type: memory
  type: project
  originSessionId: d2977795-0bd5-4502-a65e-166b52726951
---

Owner-agreed rules for how `site_studio/site_edit.py::auto_link_menu` wires a restored single-page site's nav. Full spec lives in `docs/CLEANER.md (глава ЯКОРЯ, МЕНЮ, ХЕДЕР, ФУТЕР)` — read it before touching nav code. These are product requirements; the owner got angry when they were wrong. Two rules that MUST hold:

1. **HEADER must always contain a link to the HERO (the first/top block).** The hero is NOT skipped — `_content_sections` includes it, flagged `is_hero` (first section + hero/intro/banner-classed ones). Header stays curated/capped at 4.

2. **FOOTER = the FULL sitemap: an in-page anchor for ABSOLUTELY EVERY section**, unlike the capped header. `_rebuild_footer_sitemap` rebuilds the footer's link block from the section list (reuse slots, clone with dividers, remove surplus), labels by section name, strips `target=_blank`. Whatever the original footer links were (dead policy/PDF links) becomes the section list. **Never leave an empty `#` anchor** — that empty-anchor bug is exactly what triggered this.

3. **Real CMS menus are LEFT ALONE** (`_is_cms_menu`: 3+ `<li class="menu-item/nav-item">`, WordPress &c). No cap/relabel/footer-rebuild/generated-header — that's the site's real multi-page nav; rewriting it guts it (a 7-item Punjabi menu became one "Snakes town" link before this guard). A Bootstrap navbar (no menu-item classes) still gets normal wiring.

Related: [[wayback-download-traps]], [[ai-semantics-feature]] (the Haiku tag-semantics that turns div-soup into header/section/footer feeds these rules). The deterministic header comes from the real top `<nav>` — a hero band is never the header.

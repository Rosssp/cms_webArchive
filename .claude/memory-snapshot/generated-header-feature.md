---
name: generated-header-feature
description: "Auto-generated header for headerless archived sites — 6 variants, colors from site, auto light/dark, CSS burger"
metadata: 
  node_type: memory
  type: project
  originSessionId: d2977795-0bd5-4502-a65e-166b52726951
---

`site_studio/header_gen.py` generates + injects a header for archived sites that have NONE (added 2026-07-13). Wired into `site_edit.auto_link_menu`: when `_find_header_nav` is None and there's no `<header>`, it builds one.

- **6 variants**: v1 Line, v2 Solid, v3 Glass, v4 Pill, v5 Minimal, v6 Centered. Picked deterministically per domain (`variant_for_domain`) — stable per site, varied across sites.
- **Colours from the site itself** = the "uniqueness": accent pulled from CSS `--primary/accent/brand` vars, else hero background, else a per-brand hue. `extract_palette` also builds a full light + dark palette.
- **Theme is anchored to the SITE, not the OS.** `extract_palette` reads the html/body background (`_site_background`) and the most-used saturated colour (`_dominant_accent` — a Bootstrap theme states #337ab7 on borders, never in a `--var`); a light site keeps a light bar even when the viewer's OS is dark. `prefers-color-scheme` is only emitted for a site that IS dark. Owner's rule: "по цветам сайта" — a black bar on a #f8f8f8 page is the bug, not a feature.
- **`position:fixed` + `.wbh-spacer`, NOT sticky.** Old themes shrink-to-fit an injected child (neerajgangwar.in has `body{display:table}` → the bar landed in an anonymous table cell, `width:100%` = 440px). `!important` can't fix that (formatting context, not specificity) and `100vw` just made the table 1473px wide → h-scroll. fixed + left/right:0 sizes against the viewport (minus scrollbar) and leaves the page's layout untouched; the spacer reserves the height (per-variant, `VARIANT_HEIGHT`).
- **Site CSS bleeds onto our markup** — `.wbh__toggle`/`.wbh__burger` need `display:none!important` (a stray checkbox + black bar showed up on desktop otherwise), and the mobile `display:flex` must be `!important` too or the burger never returns.
- **Logo** = a baked wordmark PNG (`_gen_logo`, cropped to content), coloured with the site accent. Baked per context to cover what one colour can't: a light-accent + dark-accent pair swapped by prefers-color-scheme, or a single white one on the solid-accent variant (v2). Text-wordmark fallback if Pillow fails. IMPORTANT: the header CSS force-resets `.wbh img{opacity/visibility/animation/transform:...!important}` — restored templates often ship `img{opacity:0}`+JS scroll-reveal that never fires (JS stripped), which hides the logo otherwise. (An injected header must be isolated from the site's own CSS bleed.)
- **Burger** = CSS-only checkbox (no JS, survives script stripping). Omitted entirely when there are no nav items.
- **Works with ZERO sections** — deliberately NOT gated on `sections`. Old table-layout sites (one `<table>`, `<p>`s, and not a single h1-h6) give `_content_sections` nothing, since the fallback is heading-based. Owner's rule: "хеддер в любом случае нужен" — there we ship the logo bar alone (no anchor nav, no burger; there is genuinely nothing to link to).
- Colour extraction must also read **old HTML attributes**: `<body bgcolor="#F6DAAC">` (table sites carry no CSS at all). And do NOT re-lighten an accent that already reads — forcing every accent to L=0.42 turned a site's real navy `#000080` into a garish invented `#0000d6`.
- First nav item is always **Home** → hero; rest are the sections.

No new runtime deps (colorsys/hashlib/re + reuses clean_wayback_site helpers). Injected as first `<body>` child + one scoped `<style>` in `<head>`; idempotent via the `data-wb-header` marker.

Spec: `site_studio/NAV_LINKING.md`. Remember [[fix-script-not-file-restart-server]]: this only reaches the studio after a server RESTART. Related: [[nav-linking-rules]], [[ai-semantics-feature]].

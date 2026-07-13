# Navigation linking spec (header / footer / menu)

How `site_edit.auto_link_menu` must wire up a restored single-page site's navigation. These are
**product rules** (agreed with the owner), not incidental behavior — getting them wrong has bitten
us before, so change them only on an explicit request.

## Sections
`_content_sections(soup)` returns every real content section in document order (a `<section>`, or
a heading-bearing top-level block on bad-markup exports). **The hero / first block IS a section** —
it is *not* skipped. Each section is flagged `is_hero` (the first section, plus any hero/intro/
banner/masthead/welcome/cover-classed one). The AI pass may attach a clean `kind` + `ai_label`.

## HEADER — curated, capped, hero-guaranteed
- A **curated** menu: keep at most `_HEADER_MAX_LINKS` (4) links, drop the rest.
- Each broken link (`#`, `/`, empty, `javascript:…`) → the best-matching section by word overlap
  (heading/class/id/eyebrow), else the next unused section. Short label (`About`, `How`…).
- **MUST always contain a link to the HERO (the first/top block).** Resolving broken links already
  sends the first one there; if the header had nothing broken to resolve, its first menu item is
  forced onto the hero anchor. There is always a "top" link.
- Real external / real sibling-page links are left alone.

## FOOTER — the FULL sitemap
- **Unlike the header, the footer lists ABSOLUTELY EVERY section**, one in-page anchor each.
- The footer nav block is **rebuilt** from the section list (`_rebuild_footer_sitemap`): existing
  link slots are reassigned, extras are cloned (dividers included), surplus removed — so the
  footer's own styling/separators survive. Labels come from the section name (`ai_label` → short
  heading). `target="_blank"` is stripped (in-page anchors open in place).
- Whatever the original footer links pointed at (dead `privacy-policy.pdf` etc.) is irrelevant —
  the result is always "every section, anchored + labeled". **Never leave an empty `#`.**

## Anchors & logo
- Section anchor id = existing id, else the AI `kind` (`#services`), else a ≤3-word slug from the
  heading/class. Set once, reused by header + footer + mobile.
- The LOGO link = the absolute canonical-domain home URL (`https://<domain>/`). Every other nav
  href is a relative in-page anchor — never an absolute URL.

## Mobile nav
Duplicate/burger menus mirror the header's resolved href+label onto links with matching text.

## Real CMS menus are LEFT ALONE (`_is_cms_menu`)
If the header or footer nav is a genuine CMS menu — 3+ `<li>` with a `menu-item`/`nav-item` class
(WordPress &c) — auto_link_menu does NOT touch it: no cap-to-4, no relabel-to-section, no
footer-sitemap rebuild, no generated header. That's the site's real multi-page navigation, already
curated and richly labelled; rewriting it guts it (a 7-item Punjabi menu became a single "Snakes
town" section link before this guard). A hand-rolled Bootstrap navbar (no menu-item classes) is NOT
a CMS menu, so it still gets the normal header wiring. Its dead sub-page links are just neutered to
`#` by the cleaner (labels preserved), so the menu still reads like the archive.

## Dead-link sweep (final pass)
After header/footer/mobile, a final sweep catches every link STILL pointing nowhere (`#`, `/`,
empty, `javascript:`) that those passes didn't touch — hero CTA buttons ("Get Started"/"Sign In"),
stray placeholders, icon-only social links. Rule:
- **link WITH visible text** (a CTA/button) → a section anchor: contextual word-match, else the
  first/any section. Never left dead.
- **icon-only link, no text** (social icon) → the dead `href` is **removed** entirely; the empty
  `<a>` stays (so it stops looking clickable), per owner preference.
- real dropdown toggles (`data-toggle`) that legitimately use `#` are left alone.

## Generated header (sites with NO header) — header_gen.py
When a page has no header at all (`_find_header_nav` is None, no `<header>`, AND `_has_site_menu`
is false), `auto_link_menu` GENERATES one via `site_studio/header_gen.py`.

**`_has_site_menu` guard**: never generate a header on top of a site that ALREADY has a menu, even
one `_find_header_nav` can't wire — e.g. an old WordPress `<ul id="navigation" class="dropdown
menu">` of image/text-less links. It matches nav/menu hints in the id OR class and needs 2+ links
or 2+ `.menu-item` `<li>`s. Stacking a generated header on such a menu is the "cleaner grew a
second header" bug. (Related download bug: `wayback_download._strip_wayback_dom` step 5 must NOT
remove `a[href*="archive.org/web/"]` — every archived internal link is wrapped that way, so it
would delete the whole menu and leave empty `<li>`s, which is what made the menu undetectable.)

Otherwise it builds:
- **logo + simple in-page anchor nav + CSS-only burger** (no JS, so it survives script stripping).
  No theme-toggle button.
- **Colour comes from the SITE**: the accent is pulled from the site's own CSS `--primary/accent/
  brand` custom properties, else the hero section's background, else a deterministic per-brand hue
  (so a greyscale site still gets a unique tint). That's the "uniqueness".
- **Auto light/dark**: ships both palettes, switches on `prefers-color-scheme` (+ honours
  `[data-theme]`). No button.
- **Auto logo**: a baked wordmark PNG (`_gen_logo`, cropped to content), coloured with the site
  accent. To cover what a single baked colour can't, it's baked per context: a light-accent +
  dark-accent pair swapped by `prefers-color-scheme`, or a single white one on the solid-accent
  variant (v2). Falls back to a CSS text wordmark if Pillow fails. NOTE the header CSS force-resets
  `.wbh img` (`opacity/visibility/animation/transform !important`) — restored templates often ship
  `img{opacity:0}` + a JS scroll-reveal that never fires once JS is stripped, which would otherwise
  hide the logo.
- **6 variants** (v1 Line, v2 Solid, v3 Glass, v4 Pill, v5 Minimal, v6 Centered); one is picked
  deterministically per domain (`variant_for_domain`) so a site is stable but sites vary.
- First nav item is always **Home** (→ hero); the rest are the sections (labels via AI/`_section_label`).
- Injected as the first child of `<body>` + one scoped `<style>` in `<head>`; idempotent
  (`data-wb-header` marker — a re-clean replaces the old one).

## E-mail domains (cleaner, not this file)
Separately, `clean_wayback_site.normalize_email_domains` rewrites every kept e-mail to the site's
own (folder-named, bare) domain — `info@old.com` on a `gigaworks.in` site → `info@gigaworks.in` —
so contacts match the restored domain. Runs only when contacts are kept.

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

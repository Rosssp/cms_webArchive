# -*- coding: utf-8 -*-
"""
header_gen.py
=============

Generate and inject a simple, universal <header> for archived sites that HAVE NONE
(auto_link_menu couldn't find a top nav). Design goals (owner spec):

  - logo + simple in-page anchor nav + a working burger on mobile. No theme-toggle button.
  - AUTO light/dark: the header ships both palettes and switches on prefers-color-scheme
    (and honours a [data-theme] override).
  - UNIQUENESS comes from COLOR: the accent is pulled from the SITE itself (its CSS
    --primary/accent/brand variables, else the hero section's background), so every site's
    header is tinted with that site's own colour.
  - The logo is auto-generated (no logo file needed) via the cleaner's existing wordmark
    generator, coloured with that same site accent.
  - 6 visual variants (v1..v6); one is picked deterministically per domain so a site always
    gets the same header, but different sites vary.

Everything is self-contained: one <style> in <head> (scoped by the .wbh class) + the header
markup at the top of <body> + a generated logo PNG. No JS (CSS-only checkbox burger), so it
survives even the most aggressive script stripping.
"""

import colorsys
import hashlib
import re

import clean_wayback_site as cw

VARIANT_COUNT = 6
VARIANT_NAMES = {
    1: "Line", 2: "Solid", 3: "Glass", 4: "Pill", 5: "Minimal", 6: "Centered",
}
_NAV_MAX = 6


# --------------------------------------------------------------------------- #
# Colour helpers
# --------------------------------------------------------------------------- #

def _rgb_to_hex(rgb):
    return "#%02x%02x%02x" % tuple(max(0, min(255, int(c))) for c in rgb)


def _hls_rgb(h, l, s):
    r, g, b = colorsys.hls_to_rgb(h % 1.0, max(0.0, min(1.0, l)), max(0.0, min(1.0, s)))
    return (int(r * 255), int(g * 255), int(b * 255))


def _hls_hex(h, l, s):
    return _rgb_to_hex(_hls_rgb(h, l, s))


def _gen_logo(dest, brand, rgb):
    """Bake `brand` as a tight coloured wordmark PNG (reuses the cleaner's wordmark drawer, then
    crops the transparent margins so a short name isn't a tiny word in a big empty box)."""
    cw._generate_auto_logo_image(dest, brand, 620, 130, color=tuple(rgb))
    try:
        from PIL import Image
        im = Image.open(dest)
        bbox = im.getbbox()  # non-transparent bounds
        if bbox:
            l, t, r, b = bbox
            pad = 8
            im = im.crop((max(0, l - pad), max(0, t - pad),
                          min(im.width, r + pad), min(im.height, b + pad)))
            im.save(dest)
    except Exception:  # noqa: BLE001 - crop is cosmetic; keep the uncropped PNG on any failure
        pass


def _rgb_to_hls(rgb):
    r, g, b = (c / 255.0 for c in rgb)
    return colorsys.rgb_to_hls(r, g, b)


def _luminance(rgb):
    """Perceived luminance 0..1 (Rec. 601-ish) - used to decide light vs dark."""
    r, g, b = rgb
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


# color-ish CSS custom properties, best first: an explicit accent/brand wins over a plain
# primary, which wins over a secondary; background/text are matched separately.
_ACCENT_VAR_HINTS = ("accent", "brand", "primary", "main", "theme", "secondary", "highlight")
_BG_VAR_HINTS = ("background", "-bg", "bg-", "surface", "body")
_TEXT_VAR_HINTS = ("text", "foreground", "-fg", "ink", "body-color")

_CSS_VAR_RE = re.compile(r"--([a-z0-9\-]+)\s*:\s*([^;{}]+?)\s*(?:;|})", re.I)


def _css_custom_props(css):
    """Map of --name -> (r,g,b) for every custom property whose value is a literal colour
    (skips var()/gradients/urls - those aren't a single resolvable colour)."""
    out = {}
    for name, val in _CSS_VAR_RE.findall(css):
        val = val.strip()
        if "var(" in val or "gradient" in val or "url(" in val:
            continue
        rgb = cw.parse_color(val)
        if rgb:
            out[name.lower()] = rgb
    return out


def _pick_var(props, hints):
    for hint in hints:
        for name, rgb in props.items():
            if hint in name:
                return rgb
    return None


_SEL_BG_RE = re.compile(r"background(?:-color)?\s*:\s*([^;{}]+)", re.I)
_HEX_OR_RGB_RE = re.compile(r"#[0-9a-fA-F]{3,6}|rgba?\([^)]*\)", re.I)


def _first_color_in(value, props):
    """First resolvable colour in a CSS value: a literal hex/rgb, or a var(--x) we know."""
    for var in re.findall(r"var\(\s*--([a-z0-9\-]+)", value, re.I):
        if var.lower() in props:
            return props[var.lower()]
    m = _HEX_OR_RGB_RE.search(value)
    if m:
        return cw.parse_color(m.group(0))
    named = cw.parse_color(value.strip())
    return named


_BODY_BG_RE = re.compile(r"(?:^|[},])\s*(?:html|body)[^{}]*\{([^}]*)\}", re.I)
_ANY_HEX_RE = re.compile(r"#[0-9a-fA-F]{6}\b|#[0-9a-fA-F]{3}\b")


def _site_background(css, props, soup=None):
    """The page's REAL background colour: the html/body rule's background. This is what the header
    must sit on - picking anything else is how the header ended up black on a #f8f8f8 site.
    Old table-layout sites state it as <body bgcolor="#F6DAAC"> with no CSS at all, so check the
    attribute too."""
    for m in _BODY_BG_RE.finditer(css):
        bm = _SEL_BG_RE.search(m.group(1))
        if bm:
            c = _first_color_in(bm.group(1), props)
            if c:
                return c
    if soup is not None:
        body = soup.find("body")
        if body is not None:
            for attr in ("bgcolor", "background-color"):
                c = cw.parse_color(body.get(attr) or "")
                if c:
                    return c
    return None


def _dominant_accent(css):
    """The site's own accent: the most-used SATURATED colour across its stylesheets (Bootstrap
    themes state it as #337ab7 on borders/buttons rather than in a --var). Greys, near-white and
    near-black are skipped - they're chrome, not brand."""
    counts = {}
    for m in _ANY_HEX_RE.finditer(css):
        rgb = cw.parse_color(m.group(0))
        if not rgb:
            continue
        h, l, s = _rgb_to_hls(rgb)
        if s < 0.25 or l < 0.12 or l > 0.88:
            continue  # grey / almost black / almost white -> not an accent
        counts[rgb] = counts.get(rgb, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _hero_background(soup, sections, css, props):
    """Best-effort background colour of the hero (first) section: its inline style, else a CSS
    rule whose selector names the hero's class/id, else the body/root background."""
    if not sections:
        return None
    hero = sections[0]["el"]
    # inline style first
    inline = hero.get("style") or ""
    m = _SEL_BG_RE.search(inline)
    if m:
        c = _first_color_in(m.group(1), props)
        if c:
            return c
    # CSS rules mentioning the hero's class/id
    tokens = []
    for cls in (hero.get("class") or []):
        tokens.append(re.escape(cls))
    if hero.get("id"):
        tokens.append(re.escape(hero["id"]))
    tokens += ["body", ":root"]
    for tok in tokens:
        for m in re.finditer(r"[.#]?%s[^{}]*\{([^}]*)\}" % tok, css, re.I):
            bm = _SEL_BG_RE.search(m.group(1))
            if bm:
                c = _first_color_in(bm.group(1), props)
                if c:
                    return c
    return None


def _gather_css(soup, html_path):
    """All CSS the page carries: inline <style> blocks + local linked stylesheets."""
    chunks = []
    for st in soup.find_all("style"):
        chunks.append(st.get_text() or "")
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = (link.get("href") or "").split("?")[0].split("#")[0]
        if not href or href.startswith(("http://", "https://", "//", "data:")):
            continue
        p = (html_path.parent / href)
        try:
            if p.is_file():
                chunks.append(cw.read_text_safe(p))
        except OSError:
            pass
    return "\n".join(chunks)


def extract_palette(soup, html_path, sections, brand):
    """Study the site and return the header palette. accent = the site's own colour (CSS
    --primary/brand var, else hero background, else a deterministic per-brand colour so a
    grayscale site still gets a unique tint). Returns a dict of ready-to-use hex strings for
    both the light and dark header themes."""
    css = _gather_css(soup, html_path)
    props = _css_custom_props(css)

    # ACCENT, best source first: an explicit --accent/--brand var, else the colour the site
    # actually USES most (borders/buttons - a Bootstrap theme states #337ab7 there, never in a
    # --var), else the hero background, else a per-brand hue for a greyscale site.
    accent = (_pick_var(props, _ACCENT_VAR_HINTS)
              or _dominant_accent(css)
              or _hero_background(soup, sections, css, props)
              or cw._letter_to_bg_color((brand or "S")[0]))
    # BACKGROUND: the html/body background is what the header actually sits on, so it - and not
    # the visitor's OS - decides light vs dark. (A #f8f8f8 site was getting a black bar purely
    # because the viewer's OS was in dark mode.)
    site_bg = (_site_background(css, props, soup)
               or _pick_var(props, _BG_VAR_HINTS)
               or _hero_background(soup, sections, css, props))

    h, acc_l, s = _rgb_to_hls(accent)
    if s < 0.12:  # the "accent" we found is basically grey -> borrow a hue so there's a tint
        h, s = _rgb_to_hls(cw._letter_to_bg_color((brand or "S")[0]))[0], 0.6
        acc_l = 0.42

    site_is_dark = bool(site_bg) and _luminance(site_bg) < 0.4
    # Sit ON the site's own background instead of a generic white/black bar.
    site_bg_hex = _rgb_to_hex(site_bg) if site_bg else None

    # Accent lightness per theme - a darker accent reads on a light bar, a brighter one on a dark
    # bar. But KEEP the site's real colour when it's already in a readable range: forcing every
    # accent to L=0.42 turned this site's navy #000080 into a garish #0000d6, which is the exact
    # opposite of "take the colours from the site". Only pull it in when it genuinely wouldn't read.
    l_acc = (h, acc_l if 0.22 <= acc_l <= 0.58 else 0.42, max(0.55, s))
    d_acc = (h, acc_l if 0.5 <= acc_l <= 0.8 else 0.66, max(0.6, s))
    return {
        "hue": h, "sat": max(0.5, min(0.9, s)),
        "site_is_dark": site_is_dark,
        # LIGHT theme - on the site's own background when it has one (#f8f8f8, not a generic white)
        "l_bg": (site_bg_hex if (site_bg_hex and not site_is_dark) else "#ffffff"),
        "l_text": "#161616",
        "l_muted": "#5b5b5b",
        "l_accent": _hls_hex(*l_acc), "l_accent_rgb": _hls_rgb(*l_acc),
        "l_border": "rgba(0,0,0,.10)",
        # DARK theme - likewise the site's own dark background, else one tinted with its hue
        "d_bg": (site_bg_hex if (site_bg_hex and site_is_dark) else _hls_hex(h, 0.08, min(0.35, s))),
        "d_text": "#f3f3f5",
        "d_muted": "#a6a6ad",
        "d_accent": _hls_hex(*d_acc), "d_accent_rgb": _hls_rgb(*d_acc),
        "d_border": "rgba(255,255,255,.12)",
        "accent_contrast": "#ffffff", "accent_contrast_rgb": (255, 255, 255),
    }


# --------------------------------------------------------------------------- #
# CSS (base + 6 variant skins) - scoped under .wbh, themed via CSS variables
# --------------------------------------------------------------------------- #

def _base_css(pal):
    # The header is anchored to the SITE's theme, not the visitor's OS. A light site keeps a light
    # bar even in dark mode - flipping it produced a black bar on a #f8f8f8 page, which is exactly
    # what "pick the header colours from the site" must not do. The prefers-color-scheme override
    # is therefore only emitted for a site that IS dark (there it genuinely helps); [data-theme]
    # stays available either way for a site with its own switcher.
    dark = pal["site_is_dark"]
    pal = dict(pal)
    pal["base_bg"] = pal["d_bg"] if dark else pal["l_bg"]
    pal["base_text"] = pal["d_text"] if dark else pal["l_text"]
    pal["base_muted"] = pal["d_muted"] if dark else pal["l_muted"]
    pal["base_accent"] = pal["d_accent"] if dark else pal["l_accent"]
    pal["base_border"] = pal["d_border"] if dark else pal["l_border"]
    pal["dark_media"] = (
        "@media (prefers-color-scheme:dark){.wbh{--bg:%(d_bg)s;--tx:%(d_text)s;--mut:%(d_muted)s;"
        "--ac:%(d_accent)s;--bd:%(d_border)s}}\n" % pal
    ) if dark else ""
    # v6 stacks the logo above the nav, so it's taller than the single 64px bar the others use.
    pal["header_h"] = pal.get("header_h", "64px")
    # the baked logo follows the same anchor: hide the variant the site's own theme doesn't use
    pal["logo_off"] = "l-light" if dark else "l-dark"
    pal["logo_dark_media"] = (
        "@media (prefers-color-scheme:dark){.wbh__logo .l-light{display:none}"
        ".wbh__logo .l-dark{display:block}}\n"
    ) if dark else ""
    return """
/* === auto-generated site header (header_gen.py) === */
.wbh{--bg:%(base_bg)s;--tx:%(base_text)s;--mut:%(base_muted)s;--ac:%(base_accent)s;--bd:%(base_border)s;--acx:%(accent_contrast)s;--lg:var(--ac);
  /* Span the VIEWPORT, not whatever box the theme drops us in. Old layouts shrink-to-fit an
     injected child - neerajgangwar.in uses body{display:table}, so the bar landed in an anonymous
     table cell and width:100%% meant "440px of content". !important can't fix that (it's the
     formatting context, not specificity), and forcing 100vw instead just made the table itself
     1473px wide -> horizontal scroll. position:fixed takes us OUT of the parent's flow entirely:
     left/right:0 resolve against the viewport (excluding the scrollbar, so no overflow) and the
     page's own layout is left completely untouched. The spacer below reserves the space. */
  display:block!important;position:fixed!important;top:0!important;left:0!important;right:0!important;
  z-index:1000!important;width:auto!important;max-width:none!important;min-width:0!important;
  margin:0!important;padding:0!important;float:none!important;transform:none!important;
  font-family:inherit;-webkit-font-smoothing:antialiased;box-sizing:border-box}
%(dark_media)s:root[data-theme="dark"] .wbh{--bg:%(d_bg)s;--tx:%(d_text)s;--mut:%(d_muted)s;--ac:%(d_accent)s;--bd:%(d_border)s}
:root[data-theme="light"] .wbh{--bg:%(l_bg)s;--tx:%(l_text)s;--mut:%(l_muted)s;--ac:%(l_accent)s;--bd:%(l_border)s}
.wbh *{box-sizing:border-box}
/* The bar is position:fixed (see above), so it no longer occupies space - this reserves exactly
   its height so the page's first block isn't hidden underneath. Height is baked per variant. */
.wbh-spacer{display:block!important;width:100%%;height:%(header_h)s;flex:none}
.wbh__inner{max-width:1180px;margin:0 auto;padding:0 clamp(16px,4vw,32px);height:64px;
  display:flex;align-items:center;justify-content:space-between;gap:20px}
.wbh__logo{display:inline-flex;align-items:center;text-decoration:none;flex:0 0 auto}
/* Defend against the restored site's own CSS bleeding onto our header: many templates set
   img{opacity:0} + a scroll-reveal animation that never fires once JS is stripped, which would
   hide our logo. Force our images visible and un-animated regardless of the page's rules. */
.wbh img{opacity:1!important;visibility:visible!important;animation:none!important;
  transform:none!important;filter:none!important;max-width:none!important;max-height:none!important}
.wbh__logo img{display:block;height:28px;width:auto}
/* baked-logo theme swap - anchored to the SITE's theme like the bar itself: a light site always
   shows the light-accent PNG (its bar stays light even in dark mode), a dark site the dark one.
   Only a genuinely dark site follows prefers-color-scheme. */
.wbh__logo .%(logo_off)s{display:none}
%(logo_dark_media)s:root[data-theme="dark"] .wbh__logo .l-light{display:none}
:root[data-theme="dark"] .wbh__logo .l-dark{display:block}
:root[data-theme="light"] .wbh__logo .l-light{display:block}
:root[data-theme="light"] .wbh__logo .l-dark{display:none}
.wbh__logo-tx{font-weight:800;font-size:clamp(17px,2.4vw,21px);letter-spacing:-.02em;color:var(--lg);
  white-space:nowrap;line-height:1}
.wbh__nav{display:flex;align-items:center;gap:clamp(14px,2.2vw,30px)}
.wbh__nav a{position:relative;text-decoration:none;color:var(--tx);font-size:15px;font-weight:500;
  letter-spacing:.01em;white-space:nowrap;transition:color .18s ease;padding:6px 0}
.wbh__nav a:hover{color:var(--ac)}
/* !important: the restored theme's own CSS bleeds onto our markup (blanket input/label rules are
   common) and was un-hiding the burger checkbox + label on desktop - they showed up as a stray
   checkbox and a black bar inside the bar. Ours must win in both directions (see also .wbh img). */
.wbh__toggle{display:none!important}
.wbh__burger{display:none!important;flex-direction:column;justify-content:center;gap:5px;width:42px;height:42px;
  padding:9px;cursor:pointer;border-radius:9px;flex:0 0 auto}
.wbh__burger span{display:block;height:2px;width:100%%;background:var(--tx);border-radius:2px;
  transition:transform .25s ease,opacity .2s ease}
@media (max-width:820px){
  .wbh__burger{display:flex!important}  /* must also be !important - the desktop rule above is */
  .wbh__nav{position:absolute;left:0;right:0;top:64px;flex-direction:column;align-items:stretch;
    gap:0;background:var(--bg);border-bottom:1px solid var(--bd);
    max-height:0;overflow:hidden;transition:max-height .3s ease}
  .wbh__nav a{padding:15px clamp(16px,4vw,32px);border-top:1px solid var(--bd)}
  .wbh__toggle:checked ~ .wbh__nav{max-height:70vh;overflow:auto}
  .wbh__toggle:checked ~ .wbh__burger span:nth-child(1){transform:translateY(7px) rotate(45deg)}
  .wbh__toggle:checked ~ .wbh__burger span:nth-child(2){opacity:0}
  .wbh__toggle:checked ~ .wbh__burger span:nth-child(3){transform:translateY(-7px) rotate(-45deg)}
}
""" % pal


_VARIANT_CSS = {
    # v1 Line - clean bar, thin bottom border, accent underline grows on hover
    1: """.wbh--v1{background:var(--bg);border-bottom:1px solid var(--bd)}
.wbh--v1 .wbh__nav a::after{content:"";position:absolute;left:0;bottom:0;height:2px;width:0;
  background:var(--ac);transition:width .22s ease}
.wbh--v1 .wbh__nav a:hover::after{width:100%}""",
    # v2 Solid - the whole bar is the site accent, contrast text
    2: """.wbh--v2{background:var(--ac);--lg:var(--acx)}
.wbh--v2 .wbh__nav a,.wbh--v2 .wbh__burger span{color:var(--acx)}
.wbh--v2 .wbh__nav a{color:var(--acx);opacity:.9}
.wbh--v2 .wbh__nav a:hover{opacity:1;color:var(--acx)}
.wbh--v2 .wbh__burger span{background:var(--acx)}
@media (max-width:820px){.wbh--v2 .wbh__nav{background:var(--ac)}.wbh--v2 .wbh__nav a{border-color:rgba(255,255,255,.18)}}""",
    # v3 Glass - translucent, blurred, floats over the hero
    3: """.wbh--v3{background:var(--bg);background:color-mix(in srgb,var(--bg) 72%,transparent);
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px);border-bottom:1px solid var(--bd)}
.wbh--v3 .wbh__nav a:hover{color:var(--ac)}
@media (max-width:820px){.wbh--v3 .wbh__nav{background:var(--bg);background:color-mix(in srgb,var(--bg) 94%,transparent);
  -webkit-backdrop-filter:blur(12px);backdrop-filter:blur(12px)}}""",
    # v4 Pill - nav items are rounded pills that fill with accent on hover
    4: """.wbh--v4{background:var(--bg);border-bottom:1px solid var(--bd)}
.wbh--v4 .wbh__nav a{padding:8px 16px;border-radius:999px;transition:background .18s ease,color .18s ease}
.wbh--v4 .wbh__nav a:hover{background:var(--ac);color:var(--acx)}
@media (max-width:820px){.wbh--v4 .wbh__nav a{border-radius:0}}""",
    # v5 Minimal - no border, uppercase spaced nav, accent dot before each item
    5: """.wbh--v5{background:var(--bg)}
.wbh--v5 .wbh__nav a{text-transform:uppercase;font-size:12.5px;font-weight:600;letter-spacing:.14em}
.wbh--v5 .wbh__nav a::before{content:"";display:inline-block;width:5px;height:5px;border-radius:50%;
  background:var(--ac);margin-right:9px;vertical-align:middle;opacity:0;transition:opacity .18s ease}
.wbh--v5 .wbh__nav a:hover::before{opacity:1}
@media (max-width:820px){.wbh--v5 .wbh__nav{border-top:1px solid var(--bd)}}""",
    # v6 Centered - logo centered on top, nav centered below, accent top rule
    6: """.wbh--v6{background:var(--bg);border-top:3px solid var(--ac);border-bottom:1px solid var(--bd)}
.wbh--v6 .wbh__inner{flex-direction:column;height:auto;gap:6px;padding-top:14px;padding-bottom:14px}
.wbh--v6 .wbh__nav a:hover{color:var(--ac)}
@media (max-width:820px){.wbh--v6 .wbh__inner{flex-direction:row}.wbh--v6{border-top-width:3px}
  .wbh--v6 .wbh__nav{top:auto}}""",
}


# v6 stacks the logo above the nav; the rest are a single 64px bar. The fixed bar reserves its
# space with .wbh-spacer, so this height must match the variant's real height.
VARIANT_HEIGHT = {6: "92px"}


def variant_css(variant, pal):
    pal = dict(pal)
    pal["header_h"] = VARIANT_HEIGHT.get(variant, "64px")
    return _base_css(pal) + "\n" + _VARIANT_CSS.get(variant, _VARIANT_CSS[1])


# --------------------------------------------------------------------------- #
# Build + inject
# --------------------------------------------------------------------------- #

def _nav_items(sections, ensure_anchor, section_label):
    """(label, '#anchor') for up to _NAV_MAX sections. The hero (first) is the 'Home'/top link;
    a too-long hero heading falls back to 'Home' so the first item stays tidy."""
    items = []
    for i, sec in enumerate(sections[:_NAV_MAX]):
        anchor = ensure_anchor(sec)
        label = section_label(sec)
        # The hero's first link is the "top" link. Its heading is usually the brand name
        # (duplicating the logo) or a long tagline - so the hero item is always just "Home".
        if i == 0:
            label = "Home"
        items.append((label or "Section", "#" + anchor))
    return items


def variant_for_domain(domain):
    """Deterministic 1..VARIANT_COUNT per domain - stable for a site, varied across sites."""
    d = (domain or "site").encode("utf-8")
    return (int(hashlib.md5(d).hexdigest()[:8], 16) % VARIANT_COUNT) + 1


def build_header(soup, html_path, sections, site_domain, variant=None,
                 ensure_anchor=None, section_label=None):
    """Generate + inject a header into a page that has none. Idempotent: replaces any header we
    injected before.

    Works with NO sections too. Old table-layout sites (2000s markup: one <table>, <p>s, and not a
    single h1-h6) give _content_sections nothing to anchor to - but they still need a header. There
    we ship the logo bar alone: no anchor nav, no burger, because there is genuinely nothing to
    link to. A header is always better than no header."""
    from bs4 import BeautifulSoup  # (cw re-exports it, but keep this module import-light)

    sections = sections or []

    brand = cw._brand_name_from_domain(site_domain) or "Site"
    variant = variant or variant_for_domain(site_domain)
    pal = extract_palette(soup, html_path, sections, brand)

    home_url = f"https://{site_domain}/" if site_domain else "#"
    nav = _nav_items(sections, ensure_anchor, section_label)

    # --- markup (CSS-only checkbox burger, no JS) ---
    header = soup.new_tag("header", attrs={"class": f"wbh wbh--v{variant}"})
    header["data-wb-header"] = "1"
    inner = soup.new_tag("div", attrs={"class": "wbh__inner"})
    header.append(inner)

    # Logo: an auto-GENERATED wordmark PNG, baked in the site accent. To handle the two things a
    # single baked colour can't - light/dark themes and the solid-accent variant - we bake per
    # context: a light-accent + dark-accent pair swapped by prefers-color-scheme (v1/v3..v6), or a
    # single white one on the accent bar (v2). Falls back to a CSS text wordmark if Pillow fails.
    logo = soup.new_tag("a", attrs={"class": "wbh__logo", "href": home_url, "aria-label": brand})
    assets_dir = html_path.with_name(html_path.stem + "_files")
    made_img = False
    try:
        assets_dir.mkdir(parents=True, exist_ok=True)
        if variant == 2:  # solid accent bar -> one contrast (white) logo for both themes
            dest = assets_dir / "wb-header-logo.png"
            _gen_logo(dest, brand, pal["accent_contrast_rgb"])
            img = soup.new_tag("img", src=f"{assets_dir.name}/{dest.name}", alt=brand)
            img["class"] = "wbh__logo-img"
            logo.append(img)
        else:  # neutral bar -> light-accent + dark-accent PNGs, CSS shows one per theme
            dl, dd = assets_dir / "wb-header-logo-l.png", assets_dir / "wb-header-logo-d.png"
            _gen_logo(dl, brand, pal["l_accent_rgb"])
            _gen_logo(dd, brand, pal["d_accent_rgb"])
            il = soup.new_tag("img", src=f"{assets_dir.name}/{dl.name}", alt=brand)
            il["class"] = "wbh__logo-img l-light"
            idk = soup.new_tag("img", src=f"{assets_dir.name}/{dd.name}", alt="")
            idk["class"] = "wbh__logo-img l-dark"
            idk["aria-hidden"] = "true"
            logo.append(il)
            logo.append(idk)
        made_img = True
    except Exception:  # noqa: BLE001 - Pillow/font/write failure -> text wordmark instead
        made_img = False
    if not made_img:
        span = soup.new_tag("span", attrs={"class": "wbh__logo-tx"})
        span.string = brand
        logo.append(span)
    inner.append(logo)

    # Nav + burger only exist if there's somewhere to navigate to. On a heading-less table site
    # the bar is just the logo - an empty menu and a burger opening nothing would be worse.
    if nav:
        toggle = soup.new_tag("input", attrs={"class": "wbh__toggle", "type": "checkbox",
                                              "id": "wbh-burger", "aria-label": "Toggle menu"})
        inner.append(toggle)
        burger = soup.new_tag("label", attrs={"class": "wbh__burger", "for": "wbh-burger",
                                              "aria-hidden": "true"})
        for _ in range(3):
            burger.append(soup.new_tag("span"))
        inner.append(burger)

        navtag = soup.new_tag("nav", attrs={"class": "wbh__nav"})
        for label, href in nav:
            a = soup.new_tag("a", href=href)
            a.string = label
            navtag.append(a)
        inner.append(navtag)

    # --- inject: remove any previous auto-header (+ its spacer/style), then add fresh ---
    for old in soup.find_all(attrs={"data-wb-header": True}):
        old.decompose()
    for old in soup.find_all(attrs={"data-wb-header-spacer": True}):
        old.decompose()
    for old in soup.find_all("style", attrs={"data-wb-header-css": True}):
        old.decompose()

    body = soup.find("body")
    if body is None:
        return None
    body.insert(0, header)
    # the fixed bar takes no space of its own - reserve it so the page doesn't start underneath
    spacer = soup.new_tag("div", attrs={"class": "wbh-spacer"})
    spacer["data-wb-header-spacer"] = "1"
    header.insert_after(spacer)

    style = soup.new_tag("style")
    style["data-wb-header-css"] = "1"
    style.string = variant_css(variant, pal)
    head = soup.find("head")
    (head or body).append(style) if head is None else head.append(style)

    return {
        "variant": variant, "variant_name": VARIANT_NAMES.get(variant, str(variant)),
        "accent_light": pal["l_accent"], "accent_dark": pal["d_accent"],
        "logo": brand, "nav": [n[0] for n in nav], "brand": brand,
    }

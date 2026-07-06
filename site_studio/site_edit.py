# -*- coding: utf-8 -*-
"""
site_edit.py
============

Direct HTML mutation helpers used by site_studio's server: apply a Google Font by
name, and insert a batch of downloaded images into whichever <img> elements AND
CSS/inline background-image slots sit inside a given CSS selector's matches
("parent class -> fill its image children and its own hero background").

Both operations are idempotent-ish and always keep a one-time ".studio-bak" backup
of the file before the first mutation in a session.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from clean_wayback_site import BeautifulSoup, PARSER, _ensure_pillow, normalize_font_family, read_text_safe  # noqa: E402

FONT_MARKER = "site-studio-font"

_CSS_RULE_RE = re.compile(r"([^{}]+)\{([^{}]*)\}")
_BG_URL_RE = re.compile(r"background(?:-image)?\s*:\s*[^;]*?url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
_ANY_URL_RE = re.compile(r"url\(\s*['\"]?([^'\")]+)['\"]?\s*\)", re.IGNORECASE)
_RASTER_EXTS = {".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".tif"}


def _read_soup(html_path):
    text = read_text_safe(html_path)
    return BeautifulSoup(text, PARSER)


def remove_elements(html_path, selector):
    """Delete every element matching `selector` entirely - the click-to-delete
    counterpart to the image picker. Returns how many elements were removed."""
    soup = _read_soup(html_path)
    try:
        matches = soup.select(selector)
    except Exception as e:
        raise ValueError(f"'{selector}' is not a valid CSS selector: {e}")
    if not matches:
        raise ValueError(f"selector '{selector}' matched no elements on the page")
    count = len(matches)
    for tag in matches:
        tag.decompose()
    _write_soup(html_path, soup)
    return count


def _write_soup(html_path, soup):
    bak = html_path.with_suffix(html_path.suffix + ".studio-bak")
    if not bak.exists():
        shutil.copyfile(html_path, bak)
    html_path.write_text(str(soup), encoding="utf-8")


def apply_font(html_path, family_param):
    """family_param e.g. "Jost:wght@400;500;600;700" or "Jost:wght@400;700,Inter" """
    family_param = normalize_font_family(family_param)
    soup = _read_soup(html_path)
    head = soup.find("head")
    if not head:
        raise ValueError("this file has no <head>")

    # Remove any existing Google Fonts links, whether they carry our marker (from a
    # previous site_studio apply) or not (e.g. injected earlier by clean_wayback_site.py)
    # - the intent of "apply a font" is to replace whatever is currently set.
    for link in list(head.find_all("link")):
        href = link.get("href", "")
        if "fonts.googleapis.com" in href or "fonts.gstatic.com" in href:
            link.decompose()
    # Same for any prior force-override <style>, from either this tool or clean_wayback_site.py
    for style in list(head.find_all(attrs={"data-site-studio-font": True})):
        style.decompose()

    preconnect1 = soup.new_tag("link", rel="preconnect", href="https://fonts.googleapis.com")
    preconnect1["data-site-studio"] = FONT_MARKER
    preconnect2 = soup.new_tag("link", rel="preconnect", href="https://fonts.gstatic.com")
    preconnect2["crossorigin"] = ""
    preconnect2["data-site-studio"] = FONT_MARKER
    families = "&".join(f"family={f.strip()}" for f in family_param.split(",") if f.strip())
    font_link = soup.new_tag(
        "link", rel="stylesheet", href=f"https://fonts.googleapis.com/css2?{families}&display=swap"
    )
    font_link["data-site-studio"] = FONT_MARKER

    head.append(preconnect1)
    head.append(preconnect2)
    head.append(font_link)

    # Loading the font isn't enough on its own - the site's existing CSS still
    # references whatever font-family the original theme used, so force it.
    primary = family_param.split(",")[0].strip().split(":")[0].strip()
    if primary:
        force_style = soup.new_tag("style")
        force_style["data-site-studio-font"] = "force"
        force_style.string = f"* {{ font-family: '{primary}', sans-serif !important; }}"
        head.append(force_style)

    _write_soup(html_path, soup)
    return {"family": family_param}


def _ext_for(data):
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:4] == b"RIFF":
        return ".webp"
    return ".jpg"


def _local_css_files(soup, html_path):
    paths = []
    for link in soup.find_all("link", rel=lambda v: v and "stylesheet" in v):
        href = link.get("href", "")
        if not href or href.startswith(("http://", "https://", "//")):
            continue
        css_path = (html_path.parent / href).resolve()
        if css_path.is_file() and css_path.suffix.lower() == ".css":
            paths.append(css_path)
    return paths


def _selector_token_re(needle):
    return re.compile(r"(?<![\w-])" + re.escape(needle) + r"(?![\w-])")


def _bg_needles(tag):
    needles = []
    if tag.get("id"):
        needles.append(f"#{tag['id']}")
    for c in tag.get("class") or []:
        needles.append(f".{c}")
    return needles


def find_media_slots(soup, html_path, selector):
    """Every place inside `selector`'s matches that currently shows an image - <img>
    descendants, AND a background-image set inline or via a class/id rule in a linked
    local stylesheet - so hero-style sections that use a CSS background instead of an
    <img> tag can be picked and refilled too, not just plain image grids."""
    try:
        matches = soup.select(selector)
    except Exception as e:
        raise ValueError(f"'{selector}' is not a valid CSS selector: {e}")
    if not matches:
        raise ValueError(f"selector '{selector}' matched no elements on the page")

    img_tags = []
    for parent in matches:
        img_tags.extend(parent.find_all("img"))

    css_texts = {path: read_text_safe(path) for path in _local_css_files(soup, html_path)}

    bg_slots = []
    for tag in matches:
        style_val = tag.get("style") or ""
        m = _BG_URL_RE.search(style_val)
        if m:
            bg_slots.append({"kind": "inline", "tag": tag, "start": m.start(1), "end": m.end(1)})
            continue

        token_res = [_selector_token_re(n) for n in _bg_needles(tag)]
        if not token_res:
            continue
        slot_found = None
        for path, text in css_texts.items():
            for rule_m in _CSS_RULE_RE.finditer(text):
                if not any(tr.search(rule_m.group(1)) for tr in token_res):
                    continue
                bg_m = _BG_URL_RE.search(rule_m.group(2))
                if bg_m:
                    slot_found = {
                        "kind": "css",
                        "path": path,
                        "start": rule_m.start(2) + bg_m.start(1),
                        "end": rule_m.start(2) + bg_m.end(1),
                    }
                    break
            if slot_found:
                break
        if slot_found:
            bg_slots.append(slot_found)

    return img_tags, bg_slots


def count_img_slots(html_path, selector):
    """How many <img>/background-image slots the given selector's matches contain -
    used to auto-size an image search when the caller doesn't specify a count."""
    soup = _read_soup(html_path)
    img_tags, bg_slots = find_media_slots(soup, html_path, selector)
    count = len(img_tags) + len(bg_slots)
    if count == 0:
        raise ValueError(
            f"selector '{selector}' matched element(s), but none of them contain an <img> or a "
            f"background-image (inline or in a linked stylesheet) - point it at the gallery/card "
            f"wrapper or hero section, not an unrelated container"
        )
    return count


def apply_images(html_path, selector, image_bytes_list, batch_name="studio"):
    """Find every <img> and background-image slot inside elements matching `selector`
    and fill them with the downloaded images, cycling through image_bytes_list if
    there are more slots than images. Raises ValueError with a clear message if the
    selector matches nothing or matches elements with no fillable slots."""
    if not image_bytes_list:
        raise ValueError("no images were downloaded to insert")

    soup = _read_soup(html_path)
    img_tags, bg_slots = find_media_slots(soup, html_path, selector)
    total_slots = len(img_tags) + len(bg_slots)
    if total_slots == 0:
        raise ValueError(
            f"selector '{selector}' matched element(s), but none of them contain an <img> or a "
            f"background-image (inline or in a linked stylesheet) - point it at the gallery/card "
            f"wrapper or hero section, not an unrelated container"
        )

    assets_dir = html_path.with_name(html_path.stem + "_files") / f"_studio_{batch_name}"
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Numbering always started at img_000 - a second apply (different selector, same
    # batch_name) silently overwrote the FIRST apply's file on disk, so both ended up
    # showing whichever image was saved last even though their CSS/HTML references
    # were never touched. Continue numbering after whatever's already there instead.
    existing = [int(m.group(1)) for f in assets_dir.glob("img_*") if (m := re.match(r"img_(\d+)", f.stem))]
    start_i = max(existing, default=-1) + 1

    saved_rel_paths = []
    for offset, data in enumerate(image_bytes_list):
        dest = assets_dir / f"img_{start_i + offset:03d}{_ext_for(data)}"
        dest.write_bytes(data)
        saved_rel_paths.append(f"{assets_dir.relative_to(html_path.parent).as_posix()}/{dest.name}")

    slot_i = 0
    for tag in img_tags:
        rel = saved_rel_paths[slot_i % len(saved_rel_paths)]
        tag["src"] = rel
        for attr in list(tag.attrs.keys()):
            if attr.startswith("data-") or attr == "srcset":
                del tag[attr]
        slot_i += 1

    css_edits = {}
    for slot in bg_slots:
        rel = saved_rel_paths[slot_i % len(saved_rel_paths)]
        slot_i += 1
        if slot["kind"] == "inline":
            tag = slot["tag"]
            style_val = tag.get("style", "")
            tag["style"] = style_val[: slot["start"]] + rel + style_val[slot["end"] :]
        else:
            css_edits.setdefault(slot["path"], []).append((slot["start"], slot["end"], rel))

    for path, edits in css_edits.items():
        text = read_text_safe(path)
        for start, end, rel in sorted(edits, key=lambda e: e[0], reverse=True):
            css_rel = os.path.relpath(html_path.parent / rel, path.parent).replace(os.sep, "/")
            text = text[:start] + css_rel + text[end:]
        path.write_text(text, encoding="utf-8")

    _write_soup(html_path, soup)
    return {
        "selector": selector,
        "images_downloaded": len(image_bytes_list),
        "slots_filled": total_slots,
    }


def remove_unused_assets(html_path):
    """Sweep the site's `<name>_files` assets folder for anything no longer
    referenced by the HTML or any of its linked local stylesheets, and quarantine
    it into `_unused_removed/` (same convention clean_wayback_site.py uses) -
    leftover picker downloads, orphaned duplicates, whatever. Never touches
    `_wayback_removed/` or `_unused_removed/` themselves. Returns the list of
    files moved (relative to the assets folder)."""
    import clean_wayback_site as cw

    soup = _read_soup(html_path)
    html_text = read_text_safe(html_path)
    css_texts = [read_text_safe(p) for p in _local_css_files(soup, html_path)]

    report = cw.Report()
    cw.remove_unused_local_assets(html_path, html_text, report, dry_run=False, extra_texts=css_texts)
    return report.removed_unused_assets


def apply_logo(html_path, auto_logo=False, brand_text=None, logo_image=None, color=None, domain_override=None):
    """Standalone logo/brand swap - the same auto-logo / logo-image / brand-text
    logic clean_html_file runs as part of a full cleanup pass, callable on its own
    so a logo change doesn't require re-running the whole cleanup. `color` is a raw
    string ('white', 'black', '#rrggbb', 'r,g,b') parsed via clean_wayback_site.parse_color."""
    import clean_wayback_site as cw

    soup = _read_soup(html_path)
    content_domain = cw.get_site_domain(soup)
    site_domain = domain_override or cw._domain_from_folder_name(html_path) or content_domain
    report = cw.Report()

    if auto_logo:
        derived = cw.apply_auto_logo(
            soup, html_path, site_domain, report, brand_text=brand_text, dry_run=False, color=cw.parse_color(color)
        )
        effective_brand = brand_text or derived or cw._brand_name_from_domain(site_domain) or "Site"
        cw.apply_brand_text(soup, effective_brand, report)
    else:
        if logo_image:
            cw.apply_logo_image(soup, html_path, logo_image, report, dry_run=False)
        if brand_text:
            cw.apply_brand_text(soup, brand_text, report)

    _write_soup(html_path, soup)
    return {
        "logo_replaced": report.logo_replaced,
        "brand_text_replaced": report.brand_text_replaced,
    }


def _ensure_node_sharp():
    node_dir = Path(__file__).resolve().parent
    if (node_dir / "node_modules" / "sharp").is_dir():
        return
    print("[setup] installing missing dependency: sharp (npm) ...")
    subprocess.check_call(["npm", "install", "sharp"], cwd=str(node_dir))


def _run_webp_batch(jobs, quality, effort, max_dimension):
    """jobs: list of (src_path, dest_path) needing a real encode. Runs them all
    through one `node webp_batch.js` process (sharp/libvips - ~2x faster than
    Pillow at identical quality/effort, measured) instead of spawning Node once per
    file. Node prints its own "[i/n] path" progress to stderr as it goes, which
    passes straight through to this process' console. Returns {src_path: error_or_None}."""
    if not jobs:
        return {}
    _ensure_node_sharp()
    script = Path(__file__).resolve().parent / "webp_batch.js"
    payload = [
        {"src": str(src), "dest": str(dest), "quality": quality, "effort": effort, "maxDimension": max_dimension}
        for src, dest in jobs
    ]
    fd, jobs_path = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        proc = subprocess.run(
            ["node", str(script), jobs_path], cwd=str(script.parent),
            stdout=subprocess.PIPE, text=True, encoding="utf-8",
        )
        if proc.returncode != 0:
            raise RuntimeError(f"webp_batch.js exited {proc.returncode}")
        results = json.loads(proc.stdout)
    finally:
        try:
            os.unlink(jobs_path)
        except OSError:
            pass
    return {Path(r["src"]): (None if r["ok"] else r.get("error", "unknown error")) for r in results}


def convert_images_to_webp(html_path, quality=82, method=4, max_dimension=2000):
    """Convert every local raster image (.png/.jpg/.jpeg/.bmp/.tiff - covers every
    common raster type this exports use) referenced by this HTML file or any of its
    linked local stylesheets to WebP (via sharp/libvips - see _run_webp_batch),
    relink every reference - <img src>/srcset and CSS background-image url() alike -
    to the new .webp file, and delete the original raster file once nothing
    references it anymore. GIFs and already-WebP/SVG assets are left untouched.

    Two things matter far more for size and speed than the quality/effort knobs:
    - Resolution: raw camera photos (3000-7000px+ per side) are routinely 10-50x
      bigger, in pixel count, than anything a web page actually displays them at -
      re-encoding all those pixels is both slow AND barely shrinks the file (WebP
      can't out-compress a JPEG that's already lossy at the same pixel count).
      Downscaling to max_dimension on the long edge first (default 2000px, still
      sharp on any screen) is the single biggest lever for both problems at once -
      measured 2.4MB/7008px -> 257KB/2000px at quality 80, same visual quality.
    - Duplicates: byte-identical files (e.g. "photo.jpg" and "photo(1).jpg" saved
      twice during a wayback capture) are encoded once and copied, not re-encoded.

    Deliberately NOT using WebP's near-lossless mode - measured it on a real photo
    and it came out at 2.5MB against a 1.1MB lossy-quality-82 result (2.3x BIGGER
    than the source JPEG): near-lossless suits flat-color/graphic content, not
    photographic noise. Lossy at quality ~80-85 is the right tool for photos.

    Returns a summary dict."""
    soup = _read_soup(html_path)
    site_root = html_path.parent
    css_paths = _local_css_files(soup, html_path)
    css_texts = {p: read_text_safe(p) for p in css_paths}

    def _local_asset_path(url):
        if not url or url.startswith(("http://", "https://", "//", "data:")):
            return None
        clean = url.split("?")[0].split("#")[0]
        p = (site_root / clean).resolve()
        try:
            p.relative_to(site_root)
        except ValueError:
            return None  # outside the site folder - never touch
        return p if p.is_file() else None

    candidates = set()

    def _collect(url):
        p = _local_asset_path(url)
        if p and p.suffix.lower() in _RASTER_EXTS:
            candidates.add(p)

    for tag in soup.find_all(["img", "source"]):
        _collect(tag.get("src"))
        for part in (tag.get("srcset") or "").split(","):
            bit = part.strip().split(" ")[0]
            if bit:
                _collect(bit)

    for tag in soup.find_all(style=True):
        for m in _ANY_URL_RE.finditer(tag["style"]):
            _collect(m.group(1))

    for text in css_texts.values():
        for m in _ANY_URL_RE.finditer(text):
            _collect(m.group(1))

    if not candidates:
        return {"converted": 0, "skipped": 0, "failed": 0, "removed_originals": 0, "details": []}

    import hashlib

    old_to_new = {}
    converted, skipped, failed = [], [], []

    # pass 1: skip anything that already has a .webp sibling
    to_hash = []
    for src_path in sorted(candidates):
        dest_path = src_path.with_suffix(".webp")
        rel = src_path.relative_to(site_root).as_posix()
        if dest_path.exists():
            old_to_new[src_path] = dest_path
            skipped.append(rel)
        else:
            to_hash.append((src_path, dest_path, rel))

    # pass 2: hash the rest so byte-identical duplicates get encoded once and copied
    first_dest_for_hash = {}
    encode_jobs = []  # (src_path, dest_path) actually going through sharp
    duplicate_of = {}  # src_path -> the sibling dest_path to copy from
    for src_path, dest_path, rel in to_hash:
        try:
            digest = hashlib.sha1(src_path.read_bytes()).hexdigest()
        except Exception as e:
            failed.append(f"{rel}: {e}")
            continue
        if digest in first_dest_for_hash:
            duplicate_of[src_path] = first_dest_for_hash[digest]
        else:
            first_dest_for_hash[digest] = dest_path
            encode_jobs.append((src_path, dest_path))

    print(
        f"[webp] {len(encode_jobs)} unique file(s) to encode via sharp, "
        f"{len(duplicate_of)} duplicate(s) to copy, {len(skipped)} already had .webp"
    )

    # pass 3: one batched sharp/node process for every real encode
    results = _run_webp_batch(encode_jobs, quality=quality, effort=method, max_dimension=max_dimension)
    for src_path, dest_path in encode_jobs:
        rel = src_path.relative_to(site_root).as_posix()
        err = results.get(src_path, "no result returned from webp_batch.js")
        if err is None:
            old_to_new[src_path] = dest_path
            converted.append(rel)
        else:
            failed.append(f"{rel}: {err}")

    # pass 4: duplicates just copy their sibling's already-encoded output
    for src_path, source_dest in duplicate_of.items():
        rel = src_path.relative_to(site_root).as_posix()
        if not source_dest.is_file():
            failed.append(f"{rel}: source encode for duplicate failed, nothing to copy")
            continue
        dest_path = src_path.with_suffix(".webp")
        try:
            shutil.copyfile(source_dest, dest_path)
            old_to_new[src_path] = dest_path
            converted.append(rel)
        except OSError as e:
            failed.append(f"{rel}: {e}")

    def _relink(url):
        p = _local_asset_path(url)
        new_p = old_to_new.get(p) if p else None
        if not new_p:
            return None
        return os.path.relpath(new_p, site_root).replace(os.sep, "/")

    def _relink_attr(url):
        return _relink(url) or url

    for tag in soup.find_all(["img", "source"]):
        src = tag.get("src")
        if src:
            new_src = _relink(src)
            if new_src:
                tag["src"] = new_src
        srcset = tag.get("srcset")
        if srcset:
            parts = []
            for part in srcset.split(","):
                part = part.strip()
                if not part:
                    continue
                bits = part.split(" ")
                bits[0] = _relink_attr(bits[0])
                parts.append(" ".join(bits))
            tag["srcset"] = ", ".join(parts)

    for tag in soup.find_all(style=True):
        def _sub_style(m):
            new_url = _relink(m.group(1))
            return m.group(0).replace(m.group(1), new_url) if new_url else m.group(0)

        tag["style"] = _ANY_URL_RE.sub(_sub_style, tag["style"])

    _write_soup(html_path, soup)

    for path, text in css_texts.items():
        def _sub_css(m):
            new_url = _relink(m.group(1))
            return m.group(0).replace(m.group(1), new_url) if new_url else m.group(0)

        new_text = _ANY_URL_RE.sub(_sub_css, text)
        if new_text != text:
            path.write_text(new_text, encoding="utf-8")

    removed = []
    for src_path, dest_path in old_to_new.items():
        if not dest_path.is_file():
            continue
        try:
            src_path.unlink()
            removed.append(src_path.relative_to(site_root).as_posix())
        except OSError:
            pass

    return {
        "converted": len(converted),
        "skipped": len(skipped),
        "failed": len(failed),
        "removed_originals": len(removed),
        "details": converted + [f"(already had .webp) {s}" for s in skipped] + [f"(FAILED) {f}" for f in failed],
    }


_CSS_DECL_RE = re.compile(r"[^:{};]+:[^;{}]*;?")


def _asset_ref_matches(candidate, needle):
    """Loose match for 'clear this broken asset' - exact string, substring either
    way, or same filename (path/query differences aside) - so pasting a full remote
    URL, a local relative path, or just a filename all work."""
    if not candidate:
        return False
    candidate = candidate.strip()
    if not candidate:
        return False
    if needle == candidate or needle in candidate:
        return True
    cand_name = candidate.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    needle_name = needle.split("?")[0].split("#")[0].rsplit("/", 1)[-1]
    return bool(needle_name) and cand_name == needle_name


def remove_asset_reference(html_path, needle):
    """Find and clear every reference to `needle` (a full URL, a local relative
    path, or just a filename) from the page - <img>/<source> src & srcset, <link>
    and <script src> tags, inline style background-image - AND every url(...) rule
    in every linked local stylesheet that points at it. This is for the "red in
    Network tab" case: a broken/dead asset reference baked into the export.

    Doesn't delete any local file from disk - if the reference was the last one
    pointing at a local asset, run the "unused assets" cleanup afterward to
    quarantine the now-orphaned file. Returns a list of what was cleared, or raises
    ValueError if nothing matched anywhere."""
    needle = (needle or "").strip()
    if not needle:
        raise ValueError("укажи путь или URL файла")

    soup = _read_soup(html_path)
    css_paths = _local_css_files(soup, html_path)
    cleared = []

    def matches(url):
        return _asset_ref_matches(url, needle)

    for tag in soup.find_all(["img", "source"]):
        src = tag.get("src")
        if src and matches(src):
            del tag["src"]
            cleared.append(f"<{tag.name} src=\"{src}\"> in HTML")
        srcset = tag.get("srcset")
        if srcset:
            kept = [p for p in srcset.split(",") if not matches(p.strip().split(" ")[0])]
            if len(kept) != len(srcset.split(",")):
                if kept:
                    tag["srcset"] = ",".join(kept)
                else:
                    del tag["srcset"]
                cleared.append(f"<{tag.name} srcset> entry in HTML")

    for tag in list(soup.find_all(["link", "script"], src=True)) + list(soup.find_all("link", href=True)):
        url = tag.get("src") or tag.get("href")
        if url and matches(url):
            cleared.append(f"<{tag.name} {'src' if tag.get('src') else 'href'}=\"{url}\"> in HTML")
            tag.decompose()

    for tag in soup.find_all(style=True):
        style_val = tag["style"]

        def _sub_inline(m, _tag=tag):
            if matches(m.group(1)):
                cleared.append(f"inline style on <{_tag.name}>")
                return ""
            return m.group(0)

        new_style = _ANY_URL_RE.sub(_sub_inline, style_val)
        new_style = re.sub(r"[a-zA-Z-]+\s*:\s*;", "", new_style).strip()
        if new_style != style_val:
            if new_style:
                tag["style"] = new_style
            else:
                del tag["style"]

    _write_soup(html_path, soup)

    for css_path in css_paths:
        text = read_text_safe(css_path)
        spans = []
        for rule_m in _CSS_RULE_RE.finditer(text):
            decls_start = rule_m.start(2)
            for decl_m in _CSS_DECL_RE.finditer(rule_m.group(2)):
                url_m = _ANY_URL_RE.search(decl_m.group(0))
                if url_m and matches(url_m.group(1)):
                    spans.append((decls_start + decl_m.start(), decls_start + decl_m.end()))
        if spans:
            for start, end in sorted(spans, reverse=True):
                text = text[:start] + text[end:]
            css_path.write_text(text, encoding="utf-8")
            cleared.append(f"{len(spans)} declaration(s) in {css_path.name}")

    if not cleared:
        raise ValueError(f"'{needle}' не найден ни в HTML, ни в подключённых CSS-файлах")
    return cleared


def replace_brand_text_everywhere(html_path, old_name, new_name):
    """Standalone brand-name sweep for the studio: replace the old owner's name with the
    new one EVERYWHERE it appears as a whole word - across every visible text node AND
    the text-bearing attributes (alt/title/aria-label/placeholder/meta content/value) -
    not just the logo element and <title> the way apply_brand_text does. Reuses the core
    clean_wayback_site.replace_brand_in_text so the cleanup pass and this button share one
    implementation."""
    import clean_wayback_site as cw

    old_name = (old_name or "").strip()
    new_name = (new_name or "").strip()
    if not old_name:
        raise ValueError("укажи старое имя бренда (что заменить)")
    if not new_name:
        raise ValueError("укажи новое имя бренда")

    soup = _read_soup(html_path)
    result = cw.replace_brand_in_text(soup, old_name, new_name)
    if result["text_replacements"] == 0 and result["attr_replacements"] == 0:
        raise ValueError(f"'{old_name}' не встречается в тексте или атрибутах страницы")

    _write_soup(html_path, soup)
    return result


def strip_owner_traces(html_path, update_year=True):
    """Standalone owner-trace strip for the studio - delegates to the core
    clean_wayback_site.strip_owner_traces so the cleanup pass and this button share one
    implementation."""
    import clean_wayback_site as cw

    soup = _read_soup(html_path)
    result = cw.strip_owner_traces(soup, update_year=update_year)
    _write_soup(html_path, soup)
    return result


def revert_to_backup(html_path, which="auto"):
    """Restore index.html from a backup. Two kinds exist: '.bak' (written by
    clean_wayback_site before a full cleanup = the pre-cleanup original) and
    '.studio-bak' (written before the first site_studio mutation this session). 'auto'
    picks the most recently created one - i.e. the closest previous state, the natural
    'undo last thing' target. The current file is snapshotted to '.pre-revert' first so
    the revert itself is reversible. Only index.html is restored - moved assets and the
    extracted CSS are not rolled back."""
    candidates = {
        "studio-bak": html_path.with_suffix(html_path.suffix + ".studio-bak"),
        "bak": html_path.with_suffix(html_path.suffix + ".bak"),
    }
    existing = {k: p for k, p in candidates.items() if p.is_file()}
    if not existing:
        raise ValueError("бэкапов не найдено (.bak / .studio-bak рядом с index.html нет)")

    if which in existing:
        chosen_key = which
    else:
        chosen_key = max(existing, key=lambda k: existing[k].stat().st_mtime)
    src = existing[chosen_key]

    pre = html_path.with_suffix(html_path.suffix + ".pre-revert")
    shutil.copyfile(html_path, pre)
    shutil.copyfile(src, html_path)
    return {"restored_from": src.name, "prev_saved_as": pre.name}


_FORMAT_REMOVE_SUFFIXES = (".bak", ".studio-bak", ".pre-revert")
_FORMAT_REMOVE_DIRS = ("_wayback_removed", "_unused_removed")


def format_for_upload(html_path, clean_unused=True):
    """Format the site folder in place so it's upload-ready - no zip, no copy, just the
    live site left in the same directory. Deletes the backup files (.bak/.studio-bak/
    .pre-revert), the leftover *.cleanup-report.txt, and the whole quarantine folders
    (_wayback_removed/ and _unused_removed/) outright. With clean_unused=True the unused-
    asset sweep runs first (moving orphans into _unused_removed) and then that folder is
    deleted too - so unreferenced images end up gone for good, not just quarantined.
    Destructive: after this, 'revert to backup' no longer has anything to restore from."""
    site_dir = html_path.parent
    result = {"removed_files": [], "removed_dirs": []}

    if clean_unused:
        try:
            result["unused_swept"] = remove_unused_assets(html_path)
        except Exception as e:  # noqa: BLE001 - formatting shouldn't die on a sweep hiccup
            result["unused_swept_error"] = str(e)

    for p in list(site_dir.rglob("*")):
        if p.is_file() and (p.suffix.lower() in _FORMAT_REMOVE_SUFFIXES or p.name.endswith(".cleanup-report.txt")):
            try:
                p.unlink()
                result["removed_files"].append(p.relative_to(site_dir).as_posix())
            except OSError:
                pass

    for d in _FORMAT_REMOVE_DIRS:
        dd = site_dir / d
        if dd.is_dir():
            shutil.rmtree(dd, ignore_errors=True)
            result["removed_dirs"].append(d + "/")

    return result

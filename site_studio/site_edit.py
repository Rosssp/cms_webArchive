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
from datetime import datetime
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_wayback_site import safe_urlsplit, safe_urljoin, BeautifulSoup, PARSER, _ensure_pillow, normalize_font_family, read_text_safe, collapse_blank_lines  # noqa: E402

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


def _select_one(soup, selector, must_be_tag=None):
    try:
        matches = soup.select(selector)
    except Exception as e:
        raise ValueError(f"'{selector}' is not a valid CSS selector: {e}")
    if not matches:
        raise ValueError(f"selector '{selector}' matched no elements on the page")
    if len(matches) > 1:
        raise ValueError(f"selector matched {len(matches)} elements - уточни селектор, нужен ровно один")
    el = matches[0]
    if must_be_tag and el.name != must_be_tag:
        raise ValueError(f"selector matched a <{el.name}>, not a <{must_be_tag}>")
    return el


def get_element_info(html_path, selector):
    """What the click-to-edit pickers (id editor, header/footer link editor) show
    right after you click an element in the preview - its current id/text/href, so
    the edit fields start pre-filled with what's actually there instead of blank."""
    soup = _read_soup(html_path)
    el = _select_one(soup, selector)
    return {
        "tag": el.name,
        "id": el.get("id") or "",
        "text": el.get_text(" ", strip=True),
        "href": el.get("href") if el.name == "a" else None,
    }


def set_element_id(html_path, selector, new_id):
    """Set (or overwrite) the id of exactly the one element `selector` matches - the
    manual counterpart to auto_link_menu's automatic id assignment, for wiring up an
    anchor by hand. Refuses to touch >1 element at once (an id has to stay unique)."""
    new_id = (new_id or "").strip()
    if not new_id:
        raise ValueError("укажи id")
    if re.search(r"\s", new_id):
        raise ValueError("id не может содержать пробелы")
    soup = _read_soup(html_path)
    el = _select_one(soup, selector)
    old_id = el.get("id") or ""
    el["id"] = new_id
    _write_soup(html_path, soup)
    return {"old_id": old_id, "new_id": new_id}


def set_nav_link(html_path, selector, text=None, href=None):
    """Set the visible text and/or href of exactly the one <a> `selector` matches -
    the manual counterpart to auto_link_menu, for fixing up a single header/footer
    link by hand once you've picked it in the preview. Either field can be left out
    (None) to leave that part alone; at least one of the two must be given."""
    soup = _read_soup(html_path)
    el = _select_one(soup, selector, must_be_tag="a")
    changed = {}
    if text is not None and text.strip():
        for child in list(el.contents):
            child.extract()
        el.append(text.strip())
        changed["text"] = text.strip()
    if href is not None and href.strip():
        el["href"] = href.strip()
        changed["href"] = href.strip()
    if not changed:
        raise ValueError("укажи новый текст и/или href")
    _write_soup(html_path, soup)
    return changed


def _write_soup(html_path, soup):
    import clean_wayback_site as cw

    bak = html_path.with_suffix(html_path.suffix + ".studio-bak")
    if not bak.exists():
        shutil.copyfile(html_path, bak)
    cw.strip_empty_style_declarations(soup)
    html_path.write_text(cw.collapse_blank_lines(str(soup)), encoding="utf-8")


def apply_font(html_path, family_param):
    """family_param e.g. "Jost:wght@400;500;600;700" or "Jost:wght@400;700,Inter".
    Delegates to clean_wayback_site.inject_google_fonts so the quick-apply button and
    the full cleanup pass share one implementation - self-hosted (downloads the
    actual font files from Google Fonts into the project and writes local @font-face,
    not a live CDN <link>), with a CDN-link fallback only if the download fails."""
    import clean_wayback_site as cw

    family_param = normalize_font_family(family_param)
    soup = _read_soup(html_path)
    if not soup.find("head"):
        raise ValueError("this file has no <head>")

    cw.inject_google_fonts(soup, family_param, html_path=html_path)

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


def relink_asset_reference(html_path, needle, new_ref):
    """Same traversal as remove_asset_reference (img/source src+srcset, link/script
    src/href, inline style url(), and url(...) in every linked local stylesheet) but
    REPLACES every match with `new_ref` instead of deleting it - the recovery-succeeded
    counterpart, used once a broken asset has actually been re-downloaded and saved
    locally. Returns a list of what was relinked."""
    needle = (needle or "").strip()
    if not needle:
        raise ValueError("укажи путь или URL файла")

    soup = _read_soup(html_path)
    css_paths = _local_css_files(soup, html_path)
    relinked = []

    def matches(url):
        return _asset_ref_matches(url, needle)

    for tag in soup.find_all(["img", "source"]):
        src = tag.get("src")
        if src and matches(src):
            tag["src"] = new_ref
            relinked.append(f"<{tag.name} src=\"{src}\"> in HTML")
        srcset = tag.get("srcset")
        if srcset:
            parts = srcset.split(",")
            new_parts = []
            hit = False
            for p in parts:
                p = p.strip()
                bits = p.split(" ")
                if matches(bits[0]):
                    bits[0] = new_ref
                    hit = True
                new_parts.append(" ".join(bits))
            if hit:
                tag["srcset"] = ",".join(new_parts)
                relinked.append(f"<{tag.name} srcset> entry in HTML")

    for tag in list(soup.find_all(["link", "script"], src=True)) + list(soup.find_all("link", href=True)):
        url = tag.get("src") or tag.get("href")
        if url and matches(url):
            attr = "src" if tag.get("src") else "href"
            tag[attr] = new_ref
            relinked.append(f"<{tag.name} {attr}=\"{url}\"> in HTML")

    for tag in soup.find_all(style=True):
        style_val = tag["style"]

        def _sub_inline(m, _tag=tag):
            if matches(m.group(1)):
                relinked.append(f"inline style on <{_tag.name}>")
                return f"url('{new_ref}')"
            return m.group(0)

        new_style = _ANY_URL_RE.sub(_sub_inline, style_val)
        if new_style != style_val:
            tag["style"] = new_style

    _write_soup(html_path, soup)

    for css_path in css_paths:
        text = read_text_safe(css_path)

        def _sub_css(m):
            if matches(m.group(1)):
                relinked.append(f"url(...) in {css_path.name}")
                return f"url('{new_ref}')"
            return m.group(0)

        new_text = _ANY_URL_RE.sub(_sub_css, text)
        if new_text != text:
            css_path.write_text(new_text, encoding="utf-8")

    if not relinked:
        raise ValueError(f"'{needle}' не найден ни в HTML, ни в подключённых CSS-файлах")
    return relinked


_BROKEN_RESOURCE_NOISE_RES = (
    re.compile(r"^Unsafe attempt to load URL", re.IGNORECASE),
    re.compile(r"'file:' URLs are treated as unique security origins", re.IGNORECASE),
)
_URL_IN_TEXT_RE = re.compile(r"https?://[^\s'\"]+")


def find_broken_resources(html_path):
    """Load the page in a real headless browser - the automated equivalent of
    reading the DevTools Network/Console tabs for red entries - and collect every
    resource that actually failed: non-2xx responses, requests the browser gave up
    on (net::ERR_*), and console errors that name a URL (CORS-blocked font/font-face
    loads show up this way, not as a distinct failed request). Same-origin 'file:'
    frame noise is filtered out - that's a file:// browsing artifact, not a broken
    asset. Returns a list of {"url": ..., "reason": ...} deduplicated by URL."""
    from playwright.sync_api import sync_playwright

    import image_providers  # noqa: F401 - ensures playwright/chromium are installed

    image_providers._ensure_playwright()

    page_url = html_path.resolve().as_uri()
    broken = {}

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page()

            def on_response(response):
                if response.status >= 400:
                    broken[response.url] = f"HTTP {response.status}"

            def on_request_failed(request):
                broken[request.url] = request.failure or "request failed"

            def on_console(msg):
                if msg.type != "error":
                    return
                text = msg.text
                if any(r.search(text) for r in _BROKEN_RESOURCE_NOISE_RES):
                    return
                m = _URL_IN_TEXT_RE.search(text)
                if m:
                    broken.setdefault(m.group(0).rstrip("'\").,;"), text[:200])

            page.on("response", on_response)
            page.on("requestfailed", on_request_failed)
            page.on("console", on_console)
            try:
                page.goto(page_url, timeout=30000, wait_until="networkidle")
            except Exception:
                pass
            page.wait_for_timeout(1000)
        finally:
            browser.close()

    broken.pop(page_url, None)

    site_root = html_path.resolve().parent

    def _rel_label(u):
        """Turn a full file:// URL into a path relative to the site folder so the UI
        shows 'index_files/slider.jpg' instead of 'file:///D:/.../slider.jpg'.
        Remote (http/https) URLs and anything outside the site root are left as-is
        (falling back to just the file name when possible)."""
        if not u.startswith("file:"):
            return u
        from urllib.parse import urlparse, unquote
        raw = unquote(urlparse(u).path)
        if os.name == "nt":
            raw = raw.lstrip("/")
        p = Path(raw)
        try:
            # Всегда относительный путь от папки сайта (c ../, если файл выше неё).
            return Path(os.path.relpath(p, site_root)).as_posix()
        except Exception:
            # Разные диски на Windows и прочие крайние случаи - хотя бы имя файла.
            return p.name or u

    return [
        {"url": u, "reason": r, "label": _rel_label(u)}
        for u, r in broken.items()
        if not u.startswith("data:")
    ]


def auto_clean_broken_resources(html_path):
    """Load the page for real, find every resource that's actually red in the
    network/console (see find_broken_resources), and try to RECOVER each one -
    download the real bytes from web.archive.org and relink every reference to the
    local copy. Does not delete/strip anything automatically: whatever can't be
    recovered is just reported so it can be reviewed and removed by hand (the manual
    field in this same section) if that's actually the right call. Returns a
    summary dict."""
    import clean_wayback_site as cw

    found = find_broken_resources(html_path)
    recovered, not_recovered, not_recovered_items = [], [], []

    for item in found:
        url = item["url"]
        label = item.get("label", url)
        data, ext = cw.recover_asset_bytes(url)
        if data is None:
            not_recovered.append(f"{label}: {item['reason']}")
            not_recovered_items.append({"label": label, "reason": item["reason"]})
            continue

        assets_dir = html_path.with_name(html_path.stem + "_files") / "_recovered"
        assets_dir.mkdir(parents=True, exist_ok=True)
        name = Path(url.split("?")[0].split("#")[0]).name or "asset"
        dest = assets_dir / name
        i = 1
        while dest.exists() and dest.read_bytes() != data:
            dest = assets_dir / f"{Path(name).stem}_{i}{Path(name).suffix or ext}"
            i += 1
        if not dest.exists():
            dest.write_bytes(data)
        new_ref = dest.relative_to(html_path.parent).as_posix()
        try:
            relink_asset_reference(html_path, url, new_ref)
            recovered.append(f"{url} -> {new_ref}")
        except ValueError:
            # recovered the bytes but couldn't find a reference to relink (e.g. a
            # dynamically-constructed URL) - still worth keeping the file around,
            # just note it wasn't wired back up automatically.
            recovered.append(f"{url} -> {new_ref} (файл сохранён, но ссылка не найдена для замены)")

    return {
        "found": len(found),
        "recovered": recovered,
        "not_recovered": not_recovered,
        "not_recovered_items": not_recovered_items,
    }


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
_FORMAT_REMOVE_DIR_NAMES = ("_wayback_removed", "_unused_removed")


def flatten_recovered_assets(html_path):
    """A CSS asset-recovery pass (clean_wayback_site's _recover_css_asset) saves
    downloaded files into a "<css-stem>_recovered/" folder next to the CSS that
    referenced them - fine for a working site, but a folder literally named
    "..._recovered" is an implementation detail that shouldn't still be visible in an
    upload-ready export. Moves every such folder's files up into its parent
    directory (deduping identical files, renaming on a genuine name collision) and
    rewrites every local .css/.html/.htm reference from "<name>_recovered/<file>"
    down to just "<file>" - unlike the quarantine folders, this one holds LIVE,
    referenced assets, so it's flattened in place, never deleted. Returns the list of
    folders flattened."""
    site_root = html_path.parent
    flattened = []
    for rec_dir in list(site_root.rglob("*_recovered")):
        if not rec_dir.is_dir():
            continue
        parent = rec_dir.parent
        prefix = rec_dir.name + "/"
        moves = {}
        for f in list(rec_dir.iterdir()):
            if not f.is_file():
                continue
            dest = parent / f.name
            i = 1
            while dest.exists() and dest.read_bytes() != f.read_bytes():
                dest = parent / f"{Path(f.name).stem}_{i}{Path(f.name).suffix}"
                i += 1
            if not dest.exists():
                shutil.move(str(f), str(dest))
            else:
                f.unlink()  # byte-identical duplicate already at the target name
            moves[f.name] = dest.name

        if moves:
            for path in site_root.rglob("*"):
                if not path.is_file() or path.suffix.lower() not in (".css", ".html", ".htm"):
                    continue
                text = read_text_safe(path)
                new_text = text
                for old_name, new_name in moves.items():
                    new_text = new_text.replace(prefix + old_name, new_name)
                if new_text != text:
                    path.write_text(new_text, encoding="utf-8")

        try:
            rec_dir.rmdir()
            flattened.append(rec_dir.relative_to(site_root).as_posix())
        except OSError:
            pass  # folder not actually empty (non-file entries) - leave it
    return flattened


NAV_CONTAINER_TAGS = ("header", "nav", "footer")
HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Old sites rarely use <h1>-<h6>: a section title is bold text, a <font size="4">, or a styled
# <td>. Those ARE headings to a visitor, so they must be anchorable - otherwise a whole site
# collapses to a single anchor and its menu gets stripped for having nowhere to point.
_VISUAL_HEADING_TAGS = ("b", "strong", "font", "big", "caption", "legend", "th", "dt", "summary")
_VISUAL_HEADING_STYLE_RE = re.compile(
    r"font-weight\s*:\s*(?:bold|[6-9]00)|font-size\s*:\s*(?:[2-9]\d|1\d\d)(?:px|pt)|"
    r"font-size\s*:\s*(?:1\.[3-9]|[2-9])(?:em|rem)", re.I)
# NB: the trailing \d* matters. Old sites number their heading classes - "heading1", "heading2",
# "title2", "head3" - and a word-boundary-only pattern matched none of them, so a page whose every
# section title was <p class="heading1"> produced zero sections and its menu had nothing to anchor
# to. Numbered variants are a convention, not a one-site quirk.
_VISUAL_HEADING_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:title|heading|head|headline|caption|subject|subtitle|"
    r"section[-_]?(?:name|title)|page[-_]?title|post[-_]?title|entry[-_]?title)\d*(?:[\s_-]|$)",
    re.I)


def _looks_like_heading(el):
    """True when `el` READS as a heading even though it isn't an <hN>.

    A short, prominent line of text: bold/large/`.title`-classed, no links inside (a menu item is
    not a heading) and short enough to be a title rather than a paragraph."""
    name = getattr(el, "name", None)
    if name is None:
        return False
    if name in HEADING_TAGS:
        return True
    txt = el.get_text(" ", strip=True)
    if not (2 <= len(txt) <= 90):
        return False
    if el.find("a") is not None or el.find(HEADING_TAGS) is not None:
        return False
    if name in _VISUAL_HEADING_TAGS:
        return True
    style = el.get("style") or ""
    if _VISUAL_HEADING_STYLE_RE.search(style):
        return True
    if _VISUAL_HEADING_CLASS_RE.search(_cls(el)):
        return True
    return False


def _find_heading_like(el):
    """The first real-or-visual heading inside `el` (or None)."""
    hit = el.find(HEADING_TAGS)
    if hit is not None:
        return hit
    for cand in el.find_all(_VISUAL_HEADING_TAGS + ("div", "p", "span", "td")):
        if _looks_like_heading(cand):
            return cand
    return None
_NAV_UNRESOLVED_HREFS = {"#", "", "/", "#!", "javascript:void(0)", "javascript:void(0);", "javascript:;"}
_HEADER_MAX_LINKS = 4
# How many links may point at one and the same section before further "matches" are treated
# as noise (a site-name word shared by the heading and dozens of menu labels matches everything).
_MAX_LINKS_PER_SECTION = 3
_NAV_CLASS_RE = re.compile(r"(?:^|[\s_-])(?:nav|navbar|menu|topnav|nav-links|main-menu|primary-menu|navigation|header)(?:[\s_-]|$)", re.I)
_MOBILE_CLASS_RE = re.compile(r"(?:mobile|burger|hamburger|offcanvas|drawer)", re.I)
_SOCIAL_CLASS_RE = re.compile(r"(?:social|share)", re.I)
_LABEL_TRAILING_DROP = {"us", "now", "more", "info", "page", "here", "section"}
_LABEL_STOPWORDS = {"the", "a", "an", "of", "to", "and", "or", "it", "in", "on", "for", "with", "your", "our", "my"}


# Split on whitespace and punctuation ONLY - never inside a word. Python's \w covers letters and
# digits but NOT combining marks (Mn/Mc), so `[^\W_]+` shatters every Indic word at its vowel
# signs: ਗੁਰਮੁਖੀ became ['ਗ','ਰਮ'] and Vietnamese "Giới thiệu" became ['Gi','i','thi','u'] - menu
# labels and match keys turned to gibberish on exactly the archives we clean most.
_TOKEN_SPLIT_RE = re.compile(r"[\s ]+|[!-/:-@\[-`{-~‐-⁞«»]+")


def _tokenize(text):
    """Words of `text` in ANY script (Latin, Cyrillic, Vietnamese, Gurmukhi, Bengali, Arabic...)."""
    return [t for t in _TOKEN_SPLIT_RE.split((text or "").strip()) if t]


def _slug_words(text, max_words=None):
    text = (text or "").strip().lower().replace("’", "'")
    words = _tokenize(text)
    if max_words:
        words = words[:max_words]
    return words


def _slugify(text, max_words=3):
    return "-".join(_slug_words(text, max_words=max_words))


def _is_dropdown_toggle(a):
    """A Bootstrap (or similar) dropdown TOGGLE button - href="#" here is not a dead
    wayback-export link, it's the mechanism: JS intercepts the click and opens the
    submenu, it was never meant to navigate anywhere. Relabeling/redirecting it would
    break a working menu, not fix a broken one."""
    classes = " ".join(a.get("class") or [])
    return "dropdown-toggle" in classes or a.get("data-toggle") == "dropdown"


def _cls(tag):
    c = tag.get("class")
    return " ".join(c) if isinstance(c, list) else (c or "")


def _set_link_text(a, text):
    for c in list(a.contents):
        c.extract()
    a.append(text)


def _remove_nav_item(a):
    """Remove a nav link, and its wrapping <li> if that leaves the <li> empty."""
    li = a.find_parent("li")
    a.decompose()
    if li is not None and not li.get_text(strip=True) and not li.find(True):
        li.decompose()


def _is_live_anchor(a, soup=None):
    """Ведёт ли пункт меню на РЕАЛЬНЫЙ раздел этой страницы.

    Единственный ответ на вопрос «якорь рабочий?» — раньше его считали в двух местах по разным
    правилам, и это стоило sanjhapunjab всего меню. Проверка `href.startswith("#")` признавала
    рабочей заглушку `href="#"`, которую сам же чистильщик ставит вместо мёртвой ссылки: сорок
    мёртвых пунктов выглядели как сорок живых якорей, порог «меню в основном мертво» не срабатывал,
    и пересборка меню из секций не запускалась ни разу.
    """
    href = (a.get("href") or "").strip()
    if len(href) < 2 or not href.startswith("#") or href.lower() in _NAV_UNRESOLVED_HREFS:
        return False
    if soup is None:
        return True
    target = href[1:]
    return soup.find(id=target) is not None or soup.find("a", attrs={"name": target}) is not None


def _nav_menu_links(container):
    """The real menu <a> items in a nav container - skips dropdown toggles, social/share
    links, and icon-only links (no visible text), which aren't page-section menu items."""
    out = []
    for a in container.find_all("a"):
        if _is_dropdown_toggle(a) or _SOCIAL_CLASS_RE.search(_cls(a)):
            continue
        if not a.get_text(strip=True):
            continue
        out.append(a)
    return out


def _find_logo_link(soup):
    """The clickable logo/brand link (usually <a class="navbar-brand"> wrapping the logo <img>).
    It's the site's real "home" link, so auto_link_menu rewrites its href to the absolute
    canonical-domain URL. Prefer an explicit brand class; else the first text-less <a> that
    wraps an <img> inside the header/nav region."""
    a = soup.find("a", class_=re.compile(r"navbar-brand|(?:^|[\s_-])logo|brand", re.I))
    if a is not None:
        return a
    for cand in soup.find_all("a"):
        if cand.find("img") and not cand.get_text(strip=True) and cand.find_parent(["header", "nav"]):
            return cand
    return None


def _find_header_nav(soup):
    """The site's primary top navigation: a <header>/<nav> if present, else the first
    nav-classed <div>/<ul> holding 2+ menu links (many exports drop the semantic tag and use
    <div class="navbar">). None if the page genuinely has no top menu (just a hero)."""
    for tn in ("nav", "header"):
        el = soup.find(tn)
        if el and _nav_menu_links(el):
            return el
    for el in soup.find_all(["div", "ul", "nav"]):
        # Match the id as well as the class. _has_site_menu already did, and the mismatch was a
        # trap: <ul id="navigation" class="dropdown"> was invisible HERE (so the menu was never
        # wired) yet visible THERE (so header generation was suppressed) - the site ended up with
        # an unwired menu whose dead links the final sweep then deleted one by one.
        ident = _cls(el) + " " + (el.get("id") or "")
        if (_NAV_CLASS_RE.search(ident) and not _MOBILE_CLASS_RE.search(ident)
                and not el.find_parent("footer") and len(_nav_menu_links(el)) >= 2):
            return el
    return None


def _is_cms_menu(el):
    """True if `el` is a genuine CMS-generated navigation menu (WordPress & co: 3+ <li> carrying a
    'menu-item'/'nav-item' class). Such a menu is already curated and richly labelled - the header
    cap/relabel and footer-sitemap rewrites must LEAVE IT ALONE (rewriting it destroys the real
    multi-page menu, e.g. turning a 7-item Punjabi menu into a single 'Snakes town' section link).
    A hand-rolled Bootstrap navbar (no menu-item classes) is NOT a CMS menu, so it still gets wired."""
    if el is None:
        return False
    return len(el.find_all("li", class_=re.compile(r"(?:^|[\s_-])(?:menu-item|nav-item)", re.I))) >= 3


def _has_site_menu(soup):
    """True if the page ALREADY has a real top menu, even one _find_header_nav can't wire up (an
    image-/CSS-based menu whose <a> items carry no text - common in old themes: <ul id="navigation"
    class="dropdown menu"> of image links). Checks the id AND class for nav/menu hints and needs
    2+ links (with or without text). Used to SUPPRESS generating a header on top of an existing one
    - generating one there just duplicates the site's own menu."""
    for el in soup.find_all(["nav", "ul", "div"]):
        ident = _cls(el) + " " + (el.get("id") or "")
        if not _NAV_CLASS_RE.search(ident) or _MOBILE_CLASS_RE.search(ident):
            continue
        if el.find_parent("footer") or el.find_parent(["nav", "ul"]) is not None:
            continue  # skip sub-menus / nested lists - count only the outer menu container
        links = [a for a in el.find_all("a") if a.get("href")]
        menu_lis = el.find_all("li", class_=re.compile(r"menu-item|nav-item", re.I))
        if len(links) >= 2 or len(menu_lis) >= 2:  # real <a> menu, or an image/CSS menu-item list
            return True
    return False


def _site_menu_element(soup):
    """The existing top menu container itself (same rules as _has_site_menu, which only answers
    yes/no). Returned so a page that HAS a menu but no <header> can have that very menu wrapped
    into one, instead of being left with no header at all."""
    for el in soup.find_all(["nav", "ul", "div"]):
        ident = _cls(el) + " " + (el.get("id") or "")
        if not _NAV_CLASS_RE.search(ident) or _MOBILE_CLASS_RE.search(ident):
            continue
        if el.find_parent("footer") or el.find_parent(["nav", "ul"]) is not None:
            continue
        links = [a for a in el.find_all("a") if a.get("href")]
        menu_lis = el.find_all("li", class_=re.compile(r"menu-item|nav-item", re.I))
        if len(links) >= 2 or len(menu_lis) >= 2:
            return el
    return None


def _find_mobile_navs(soup, exclude_ids):
    """Duplicate/mobile nav containers (class hints mobile/burger/offcanvas/drawer) to mirror
    the header's decisions into, so the mobile menu gets the same anchors and labels."""
    out = []
    for el in soup.find_all(["div", "ul", "nav"]):
        if id(el) in exclude_ids:
            continue
        if _MOBILE_CLASS_RE.search(_cls(el)) and _nav_menu_links(el):
            out.append(el)
    return out


def _looks_like_footer_nav(footer):
    """True if the footer actually has a block of site-nav-style links (2+ text links that
    aren't social icons) - if it's just a copyright line, we add nothing to it."""
    links = [a for a in footer.find_all("a")
             if a.get_text(strip=True) and not _SOCIAL_CLASS_RE.search(_cls(a))]
    return len(links) >= 2


# A sidebar widget is chrome, not a section of the page. Without this the "sections" of
# sanjhapunjab came out as Search / Featured Posts / Archives while its eight actual article
# headings were ignored - so no menu item could ever match anything real.
_WIDGET_CLASS_RE = re.compile(
    r"(?:^|[\s_-])(?:widget|sidebar|side-bar|aside|secondary|rightcont|leftcont|"
    r"search|archives?|categor(?:y|ies)|recent|tags?|calendar|meta|blogroll|"
    r"subscribe|social|share|advert|banner|promo)\w*(?:[\s_-]|$)", re.I)


def _is_widget_block(el):
    for anc in [el, *el.parents]:
        if getattr(anc, "name", None) in (None, "[document]"):
            break
        ident = _cls(anc) + " " + (anc.get("id") or "")
        # Блок, который сам объявлен разделом, виджетом быть не может. Иначе `section-category`
        # (раздел «Health»/«Lifestyle» на новостном сайте) попадал под слово «category» в списке
        # виджетов, и ВСЕ настоящие разделы страницы выбрасывались из списка секций.
        if re.search(r"(?:^|[\s_-])section(?:[\s_-]|$)", ident, re.I):
            return False
        if anc.name in ("aside",):
            return True
        if _WIDGET_CLASS_RE.search(ident):
            return True
    return False


def _fallback_section_blocks(soup, nav_desc):
    """When a page has no <section> tags (common in older exports that lay content out as
    <div class="content-section-a"> blocks straight under <body>), use the content root's
    direct children that carry a heading as the sections instead. Skips nav/header/footer
    (and their descendants) and non-element nodes; returns block elements in document order."""
    root = soup.find("main") or soup.body or soup
    # Descend past the wrapper(s) themes put around everything. Looking only at the direct children
    # of <body> found ONE "section" - the wrapper itself - on the extremely common
    # <body><div id="wrapper">…whole page…</div></body>. With a single section to anchor to, the
    # sweep below then deleted every other menu item, which is the "8 items -> 1" menu gutting.
    for _ in range(6):
        kids = [c for c in root.find_all(recursive=False)
                if getattr(c, "name", None) not in (None, "script", "style", "link", "meta", "br", "noscript")]
        heading_kids = [c for c in kids if _find_heading_like(c) is not None]
        if len(heading_kids) >= 2:
            break
        if len(kids) == 1 and kids[0].name in ("div", "center", "form", "section", "main", "table", "tbody", "tr", "td"):
            root = kids[0]
            continue
        if len(heading_kids) == 1 and heading_kids[0].name in ("div", "center", "section", "table", "tbody", "tr", "td", "main"):
            root = heading_kids[0]
            continue
        break
    out = []
    for ch in root.find_all(recursive=False):
        if not getattr(ch, "name", None) or ch.name in ("nav", "header", "footer", "script", "style"):
            continue
        if id(ch) in nav_desc:
            continue
        if _find_heading_like(ch) is not None and not _is_widget_block(ch):
            out.append(ch)
    return out


_AI_SECTION_LABELS = {}


def _ai_section_starts(soup, nav_desc):
    """Blocks the model says begin a section, for pages with no usable headings at all."""
    root = soup.find("main") or soup.body or soup
    cands = []
    for el in root.find_all(["div", "p", "section", "td", "table", "center"]):
        if id(el) in nav_desc or el.find_parent(["header", "footer", "nav"]) is not None:
            continue
        txt = el.get_text(" ", strip=True)
        if not (20 <= len(txt) <= 1200):
            continue
        if el.find(["div", "p", "table"]) is not None:
            continue  # take leaf-ish blocks, not wrappers holding the whole page
        cands.append(el)
        if len(cands) >= 30:
            break
    if len(cands) < 2:
        return []
    try:
        import semantics
        if not semantics.available():
            return []
        rows = semantics.find_section_starts(
            [{"i": i, "tag": c.name, "cls": _cls(c), "text": c.get_text(" ", strip=True)}
             for i, c in enumerate(cands)])
    except Exception:  # noqa: BLE001 - no key / bad JSON -> deterministic behaviour unchanged
        return []
    out = []
    for row in (rows or []):
        i = row.get("i")
        if isinstance(i, int) and 0 <= i < len(cands):
            out.append((cands[i], row.get("label") or ""))
    return out


def _content_sections(soup):
    """Real content sections a nav item can point to - <section> blocks NOT inside a
    header/nav/footer, each carrying a heading. Each entry knows its anchor element, heading
    text and a set of match-keys (words from its heading, class, id and any .section-tag) so a
    link can be matched to the right section by meaning (e.g. "About Us" -> .about-us), not
    just document order."""
    nav_desc = set()
    for tn in NAV_CONTAINER_TAGS:
        for c in soup.find_all(tn):
            nav_desc.add(id(c))
            nav_desc.update(id(x) for x in c.find_all(True))
    out = []
    # Prefer real <section> tags; fall back to heading-bearing top-level blocks when the page
    # has none (bad-semantics exports where sections are plain <div>s).
    # "sections or fallback" was all-or-nothing: a page where the cleaner produced a single
    # <section> while twelve real content blocks stayed <div> never reached the fallback, so the
    # menu had one anchor target and the rest of its items were deleted. Two is the threshold - one
    # section is not an outline.
    _secs = [s for s in soup.find_all("section")
             if id(s) not in nav_desc and not _is_widget_block(s)]
    # ЯКОРЬ СТАВИТСЯ НА <h2> (правило владельца, 2026-07-20). Чистильщик сам приводит уровни так,
    # что h2 — это заголовок РАЗДЕЛА, а h3 — карточка внутри него. Значит список h2 и есть готовый
    # перечень разделов, причём детерминированный: не зависит ни от агента, ни от того, обернул ли
    # шаблон блок в <section>. Раньше разделы искались по блокам-обёрткам, и у sanjhapunjab при
    # одиннадцати h2 находилось два раздела — цеплять якоря было практически не к чему.
    _h2_hosts = []
    _main = soup.find("main") or soup.body or soup
    for h in _main.find_all("h2"):
        if id(h) in nav_desc or _is_widget_block(h):
            continue
        if h.find_parent(["header", "footer", "nav"]) is not None:
            continue
        if not h.get_text(strip=True):
            continue
        _h2_hosts.append(h)
    # Якорь вешается на САМ заголовок: он и есть цель перехода, и его id никуда не уедет при
    # перестановке блоков. Обёртку не ищем — именно поиск обёртки и терял разделы.
    for h in _h2_hosts:
        if not any(id(h) == id(x) for x in _secs):
            _secs.append(h)
    if len(_secs) < 2:
        _seen_ids = {id(s) for s in _secs}
        _extra = [b for b in _fallback_section_blocks(soup, nav_desc) if id(b) not in _seen_ids]
        _secs = _secs + _extra
    # Still nothing? The page has no headings at all - not even bold/large text we can recognise.
    # Ask the model where a reader would see a new topic begin. This is a question about MEANING:
    # no rule separates "Mayor's Corner" (a section title) from a bold word inside a paragraph.
    # Without it such pages get zero sections, and with nothing to anchor to the menu is stripped
    # to a single item.
    # Still thin? Every real heading in the content is a legitimate section start. A blog page is
    # eight articles with eight <h2>; treating only wrapper divs as sections found none of them, so
    # the menu had nothing to point at even though the page was full of anchorable titles.
    # Заголовки РАЗДЕЛОВ добавляются ВСЕГДА, а не только когда секций мало. У firsttalk на странице
    # девять тегов <section> (модалка, тикер, лента), поэтому проверка «если секций < 2» не
    # срабатывала, и настоящие разделы Health/Lifestyle/India в список не попадали вовсе — меню
    # цеплялось к служебным блокам.
    if True:
        try:
            import clean_wayback_site as _cw
            _sec_heads, _ = _cw.classify_headings(soup)
        except Exception:  # noqa: BLE001
            _sec_heads = []
        for h in _sec_heads:
            if id(h) in nav_desc or _is_widget_block(h):
                continue
            if h.find_parent(["header", "footer", "nav"]) is not None:
                continue
            host = h.parent if h.parent is not None and h.parent.name not in ("main", "body") else h
            # поднимаемся до блока раздела, а не до обёртки заголовка
            for _ in range(3):
                par = host.parent
                if par is None or par.name in ("main", "body", "html"):
                    break
                if len(par.get_text(" ", strip=True)) > len(host.get_text(" ", strip=True)) * 1.5:
                    break
                host = par
            if not any(id(host) == id(x) for x in _secs):
                _secs.append(host)

    if len(_secs) < 2:
        main = soup.find("main") or soup.body or soup
        for h in main.find_all(HEADING_TAGS):
            if id(h) in nav_desc or _is_widget_block(h):
                continue
            if h.find_parent(["header", "footer", "nav"]) is not None:
                continue
            host = h.parent if h.parent is not None and h.parent.name not in ("main", "body") else h
            if not any(id(host) == id(x) for x in _secs):
                _secs.append(host)

    if len(_secs) < 2:
        _ai_secs = _ai_section_starts(soup, nav_desc)
        if _ai_secs:
            _seen_ids = {id(s) for s in _secs}
            _secs = _secs + [b for b, _lbl in _ai_secs if id(b) not in _seen_ids]
            _AI_SECTION_LABELS.update({id(b): lbl for b, lbl in _ai_secs})
    for sec in _secs:
        if id(sec) in nav_desc:
            continue
        # NOTE: the hero/banner band is NOT skipped - it's a real, linkable section. The header
        # must carry a link to it (the "Home"/top link) and the footer lists every section, hero
        # included. It's just flagged is_hero so the header can guarantee that first link.
        is_hero = bool(re.search(r"(?:^|[\s_-])(?:hero|banner|intro|masthead|jumbotron|welcome|cover)(?:[\s_-]|$)", _cls(sec), re.I))
        # Title source, in order of how good a menu label it makes: the eyebrow/.section-tag
        # (usually the cleanest short descriptor, e.g. "Our Services"), else the first heading
        # (an h2, or - when the section has no heading of its own, just a card grid - the first
        # card's h3), else a title made from the section's own class name.
        tag_el = sec.find(class_=re.compile(r"section-tag|eyebrow|overline|subtitle|kicker|label", re.I))
        tag_text = re.sub(r"\s+", " ", tag_el.get_text(" ", strip=True)).strip() if tag_el else ""
        # Раздел может БЫТЬ заголовком (якорь ставим прямо на h2). `find` ищет только среди
        # потомков и для самого заголовка вернёт None — подпись тогда собиралась из имени класса.
        h = sec if sec.name in HEADING_TAGS else sec.find(HEADING_TAGS)
        h_text = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip() if h else ""
        cls_words = [w for w in _slug_words(_cls(sec))
                     if w not in _SECTION_LABEL_NOISE and not _ANIM_CLASS_RE.match(w)]
        title = tag_text or h_text or " ".join(w.capitalize() for w in cls_words[:3])
        keys = (set(_slug_words(h_text)) | set(_slug_words(tag_text))
                | set(cls_words) | set(_slug_words(sec.get("id") or "")))
        # A short body sample (first paragraph-ish text after the heading) so the optional AI
        # classifier can tell "Our Work" (portfolio) from "How It Works" (process) by content.
        p = sec.find("p")
        sample = re.sub(r"\s+", " ", p.get_text(" ", strip=True)).strip()[:200] if p else ""
        out.append({"el": sec, "text": title, "keys": keys, "sample": sample,
                    "kind": None, "ai_label": _AI_SECTION_LABELS.get(id(sec)), "is_hero": is_hero})
    # Разделы с ОДИНАКОВОЙ подписью схлопываются в один. Иначе и в меню, и в карте появлялись
    # повторы («Snakes town» трижды подряд, наезжая друг на друга) — один и тот же заголовок
    # попадал в список и как блок-обёртка, и как сам <h2>. Для читателя это выглядит как поломка
    # вёрстки, хотя ссылки рабочие. Первое вхождение выигрывает — оно выше по странице.
    _seen_titles, _uniq = set(), []
    for s in out:
        key = re.sub(r"\s+", " ", (s.get("text") or "")).strip().casefold()
        if key and key in _seen_titles:
            continue
        if key:
            _seen_titles.add(key)
        _uniq.append(s)
    out = _uniq
    # The first section in document order is the hero/top block even if it carries no hero-ish
    # class - the header's guaranteed "top" link points here.
    if out:
        out[0]["is_hero"] = True
    return out


def _classify_sections_ai(sections):
    """Best-effort: ask Haiku for a canonical kind + clean menu label per section and stash them
    on each section dict ('kind', 'ai_label'). No-op without the semantics module/key."""
    if not sections:
        return
    try:
        import semantics
    except ImportError:
        return
    if not semantics.available():
        return
    labels = semantics.classify_sections(
        [{"text": s["text"], "sample": s.get("sample", "")} for s in sections]
    )
    if not labels:
        return
    for sec, lab in zip(sections, labels):
        sec["kind"] = lab.get("kind")
        sec["ai_label"] = lab.get("label") or None

    # Human anchor names. The deterministic slug comes from the heading's words and fails exactly
    # where it matters - a Vietnamese or Urdu heading yields nothing usable and the fallback was
    # "#section-3536", which tells a visitor and a search engine nothing. The model names a block
    # for what it IS (#about, #products, #contact) in any language.
    try:
        slugs = semantics.name_anchors(
            [{"i": i, "text": s.get("text") or "", "cls": _cls(s["el"])}
             for i, s in enumerate(sections)])
        for i, slug in (slugs or {}).items():
            if not (0 <= i < len(sections)):
                continue
            if not sections[i]["el"].get("id"):
                sections[i]["kind"] = slug  # _ensure_section_anchor prefers `kind` as the slug
            # ...и ПОДПИСЬ тоже. Иначе в меню и в карте сайта попадают имена классов и технические
            # id: "Heading2", "Rightcomtext", "content_wapper_choice", "section-7680" - для
            # посетителя это шум. Берём человеческое имя, когда своего заголовка у секции нет или
            # он сам выглядит техническим.
            # Проверяем И собственный заголовок, И уже проставленную метку: техническое имя
            # («Heading2», «Rightcomtext», «content_wapper_choice», «section-7392») приходит чаще
            # всего именно из метки, а не из текста, и раньше оно побеждало человеческое имя.
            def _is_technical(v):
                v = (v or "").strip()
                if not v:
                    return True
                if re.fullmatch(r"[\w -]*(?:section|wapper|wrapper|cont|content|text|heading|"
                                r"title|div|block|item|left|right|main|body)[\w -]*\d*", v, re.I):
                    return True
                # строка без пробелов из букв/цифр/подчёркиваний — это id или класс, а не заголовок
                return bool(re.fullmatch(r"[a-z0-9_-]{4,}", v, re.I)) and " " not in v

            if _is_technical(sections[i].get("text")) and _is_technical(sections[i].get("ai_label")):
                sections[i]["ai_label"] = slug.replace("-", " ").strip().capitalize()
    except Exception:  # noqa: BLE001 - no key -> deterministic slugs, unchanged behaviour
        pass


def _ensure_section_anchor(sec):
    """Anchor id for a section: its existing id if present, else a fresh contextual slug (max
    3 words, from the heading or the section class) added to the section element."""
    if sec["el"].get("id"):
        return sec["el"]["id"]
    # Prefer the AI-derived canonical kind as the anchor slug (clean, meaningful: #services,
    # #about) when we have a confident one - falls back to the heading/class slug otherwise.
    kind = sec.get("kind")
    if kind and kind not in ("other", "hero"):
        sec["el"]["id"] = kind
        return kind
    words = _slug_words(sec["text"])
    while words and words[-1] in _LABEL_STOPWORDS:  # trim trailing filler for a clean anchor
        words.pop()
    slug = "-".join(words[:3])
    if not slug:
        slug = "-".join(_slug_words(_cls(sec))[:3])
    # NEVER return empty: the caller writes "#" + slug, so an empty slug produces href="#", a link
    # that goes nowhere - exactly what NAV_LINKING.md forbids ("Never leave an empty #"). It happens
    # whenever the heading is non-Latin or punctuation-only and _slug_words yields nothing.
    if not slug:
        slug = "section-" + str(abs(id(sec["el"])) % 10000)
    # ...and it must be UNIQUE: two sections with the same heading otherwise share one id and every
    # link scrolls to the first of them.
    root = sec["el"]
    while root.parent is not None:
        root = root.parent
    base, n = slug, 2
    while root.find(id=slug) is not None:
        slug = f"{base}-{n}"
        n += 1
    sec["el"]["id"] = slug
    return slug


def _best_section(link_text, sections, used):
    """The section a nav link should point to: the unused section whose match-keys overlap the
    link text the most; zero-overlap falls back to the next unused section in order. None if
    every section is already claimed."""
    avail = [(i, s) for i, s in enumerate(sections) if id(s["el"]) not in used]
    if not avail:
        return None
    lw = set(_slug_words(link_text))
    best = max(avail, key=lambda it: (len(lw & it[1]["keys"]), -it[0]))
    return best[1]


def _in_protected_cms_menu(a):
    """True if this link belongs to a genuine CMS-generated menu (see _is_cms_menu). Those are the
    site's real navigation - we may neutralize a dead href inside them, but never delete the item."""
    for parent in a.parents:
        if getattr(parent, "name", None) in ("ul", "nav", "div") and _is_cms_menu(parent):
            return True
        if getattr(parent, "name", None) == "body":
            break
    return False


# Class tokens that say nothing about WHAT a section is - layout scaffolding plus the animation/
# hover/utility families. A section with no heading takes its label from its classes, so without
# this a `<section class="wow fadeInUp animated">` became the menu item "Animated" (tuonggohungthinh
# turned "Giới thiệu" into "Animated"). Never name a section after how it animates.
_SECTION_LABEL_NOISE = {
    "section", "container", "wrapper", "row", "col", "content", "grid", "inner", "outer",
    "block", "box", "item", "wrap", "main", "area", "holder", "flex", "clearfix",
}
_ANIM_CLASS_RE = re.compile(
    r"^(?:wow|animated?|animate|aos|hvr|sr|delay|duration|infinite|"
    r"(?:fade|slide|zoom|bounce|flip|roll|light|rotate|swing|puff|pulse|shake|wobble|tada)"
    r"(?:in|out|up|down|left|right|x|y)*"
    r"|d(?:elay)?-?\d+|\d+s)$", re.I)


def _matched_section(link_text, sections):
    """The section a link GENUINELY refers to - by word overlap with the section's heading/class/
    id/eyebrow - or None when nothing matches. Unlike `_best_section` this never falls back to
    "the first available section": silently pointing an unmatched menu item at an unrelated block
    is how a whole mega-menu ended up on one anchor."""
    lw = set(_slug_words(link_text))
    if not lw:
        return None
    best, score = None, 0
    for s in sections:
        n = len(lw & s["keys"])
        if n > score:
            best, score = s, n
    return best


def _short_label(text):
    """A brief header-style label from a link's text: drop trailing filler ('Us', 'Now'...) and
    stopwords, cap at 2 words. 'About Us' -> 'About', 'How It Works' -> 'How Works'.

    The word split MUST be Unicode-aware. With the old [A-Za-z0-9]+ every non-Latin script fell
    apart mid-word - Vietnamese "Giới thiệu" split into ['Gi','i','thi','u'] and the menu item
    became "Gi i". These archives are Vietnamese/Punjabi/Bengali/Cyrillic far more often than not."""
    words = _tokenize(text)
    while words and words[-1].lower() in _LABEL_TRAILING_DROP:
        words.pop()
    sig = [w for w in words if w.lower() not in _LABEL_STOPWORDS] or words
    return " ".join(sig[:2]) if sig else (text or "").strip()


_GENERIC_LABEL_RE = re.compile(
    r"^(?:home|links?|click(?:\s*here)?|read\s*more|learn\s*more|more|page|untitled|menu|item|nav|go|here|#)$",
    re.I,
)


def _is_generic_label(text):
    """True when a link's own text is a meaningless placeholder ('Home', 'Click here', 'Read
    more', '#'...) worth replacing with the destination section's name. A real label like
    'Privacy Policy' is NOT generic - it stays as-is, we only give it a working anchor."""
    t = re.sub(r"\s+", " ", (text or "")).strip()
    return not t or bool(_GENERIC_LABEL_RE.match(t))


_DIVIDER_CLASS_RE = re.compile(r"divider|separator|\bsep\b|dot", re.I)


def _section_label(sec):
    """The menu label for a section: prefer the AI label, else a short form of its heading."""
    return sec.get("ai_label") or _short_label(sec["text"]) or (sec["text"] or "").strip()


def _assign_footer_slot(slot, sec, changes):
    """Point one footer link 'slot' (its <li> wrapper, or the bare <a>) at a section anchor,
    relabel it to the section name, and drop target=_blank (in-page anchors open in place)."""
    a = slot if getattr(slot, "name", None) == "a" else slot.find("a")
    if a is None:
        return
    anchor = _ensure_section_anchor(sec)
    a["href"] = "#" + anchor
    if a.has_attr("target"):
        del a["target"]
    _set_link_text(a, _section_label(sec) or anchor)
    changes.append(f'{a.get_text(" ", strip=True)} -> #{anchor}')


# Recognising an EXISTING copyright line is what keeps this pass idempotent - anything not matched
# here gets a second line appended on the next clean. Kept deliberately broad, and the localiser is
# additionally required to keep the © sign so a translated line always matches.
_COPYRIGHT_LINE_RE = re.compile(r"©|&copy;|\(c\)\s*\d{4}|copyright|all rights reserved|"
                                r"tous droits|derechos reservados|全著作權|판权|판권|"
                                r"bản quyền|bảo lưu|版权所有|著作権|حقوق|सर्वाधिकार|"
                                r"все права защищены|усі права", re.I)


def _augment_existing_copyright(footer, name, year):
    """The footer ALREADY says something copyright-ish - keep its wording, but make sure it carries
    (a) the current year and (b) this site's domain. Edits the single text node that holds the ©
    line, so surrounding markup is untouched. Idempotent: a line that's already current+on-domain is
    left byte-for-byte. Returns True if anything changed."""
    node = None
    for s in footer.find_all(string=_COPYRIGHT_LINE_RE):
        node = s
        break
    if node is None:  # the © sits across several tags - don't risk mangling it, leave as-is
        return False
    s = orig = str(node)
    years = re.findall(r"(?:19|20)\d{2}", s)
    if years and int(years[-1]) < year:  # bump the newest year in a "2010-2015" style range
        idx = s.rfind(years[-1])
        s = s[:idx] + str(year) + s[idx + 4:]
    elif not years:  # a copyright with no year at all - give it one (before any trailing period)
        s = f"{s.rstrip().rstrip('.')} {year}"
    low = name.lower()
    bare = low[4:] if low.startswith("www.") else low
    if bare and bare not in s.lower():
        s = f"{s.rstrip()} · {name}"
    if s != orig:
        node.replace_with(s)
        return True
    return False


def _ensure_footer_copyright(footer, domain, soup):
    """Guarantee the page ends with a proper copyright line: `© <year> <domain>. All rights
    reserved.`

    If the footer already states something copyright-ish, its wording is KEPT but topped up to the
    current year and this site's domain (see _augment_existing_copyright). If it says nothing of the
    sort, the full line is appended. On a non-English page the appended sentence is translated by the
    model when a key is configured (a Vietnamese site ending in an English sentence looks
    machine-made); without a key the English line is still written, so the guarantee holds either
    way."""
    if footer is None:
        return False
    year = datetime.now().year
    name = (domain or "").strip().rstrip("/") or "this site"
    if _COPYRIGHT_LINE_RE.search(footer.get_text(" ", strip=True)):
        return _augment_existing_copyright(footer, name, year)
    text = f"© {year} {name}. All rights reserved."
    lang = ""
    root = soup.find("html")
    if root is not None:
        lang = (root.get("lang") or "").split("-")[0].lower()
    if lang and lang not in ("en", ""):
        try:
            import semantics
            if semantics.available():
                localized = semantics.localize_copyright(text, lang, name, year)
                if localized:
                    text = localized
        except Exception:  # noqa: BLE001 - the English line is a fine fallback
            pass
    p = soup.new_tag("p")
    p["class"] = ["site-copyright"]
    p.string = text
    footer.append(p)
    return True


def _rebuild_nav_from_sections(nav, sections, limit=4):
    """Rewrite a menu into working in-page navigation, REUSING its own <a> elements.

    Preserving the original labels only makes sense when they can be matched to something on the
    page. On a restored single-page site they usually cannot: the menu points at sub-pages that no
    longer exist, and on sanjhapunjab it was 40 Urdu items over English article titles - matching
    them was never going to work, and the result was 40 dead words in the header.

    What the page needs is navigation that WORKS. So the item text is replaced with the section
    title and the href with its anchor. Existing <a> elements are reused rather than created, so
    the theme's CSS still styles the bar exactly as before; surplus items are removed.
    """
    if nav is None or not sections:
        return 0
    items = _nav_menu_links(nav)
    want = sections[:max(1, min(limit, len(sections)))]
    # Если переиспользовать нечего — СОЗДАТЬ пункты. Раньше функция просто выходила, и это была
    # причина, по которой firsttalk оставался с одним пунктом: его семь пунктов вели на подстраницы
    # (/spotlight, /world), их обнулили и удалили как непривязанные, а пересборка потом не нашла ни
    # одного <a>, который можно переписать. Меню обязано быть, даже если исходное стёрли.
    if not items:
        # Подняться до САМОГО корня: new_tag есть только у объекта документа, а find_parent("html")
        # возвращает обычный тег — из-за этого создание пунктов молча не срабатывало.
        soup = nav
        while getattr(soup, "parent", None) is not None:
            soup = soup.parent
        if not hasattr(soup, "new_tag"):
            return 0
        host = nav.find("ul") or nav
        for _ in want:
            a = soup.new_tag("a") if hasattr(soup, "new_tag") else None
            if a is None:
                break
            if host.name == "ul":
                li = soup.new_tag("li")
                li.append(a)
                host.append(li)
            else:
                host.append(a)
            items.append(a)
        if not items:
            return 0
    changed = 0
    for a, sec in zip(items, want):
        href = "#" + _ensure_section_anchor(sec)
        label = _section_label(sec)
        if not label:
            continue
        a["href"] = href
        _set_link_text(a, label)
        changed += 1
    for a in items[len(want):]:
        _remove_nav_item(a)
        changed += 1
    return changed


def _rebuild_footer_sitemap(footer, sections):
    """Rewrite the footer's nav-link block into the site's FULL sitemap: one in-page anchor per
    section, ALL of them (unlike the capped header). Reuses the footer's own link markup - the
    <li> items and their dividers - so styling/separators survive: reassigns existing slots,
    clones the template for extra sections, removes surplus. Returns a list of change strings."""
    links = _nav_menu_links(footer)
    if not links or not sections:
        return []

    def _slot(a):
        li = a.find_parent("li")
        return li if li is not None else a

    slots, seen = [], set()
    for a in links:
        s = _slot(a)
        if id(s) not in seen:
            seen.add(id(s))
            slots.append(s)
    if not slots:
        return []
    n_sec, n_slot = len(sections), len(slots)

    # a divider element sitting between the links (e.g. <li class="footer-menu-divider">·</li>)
    divider_tpl = None
    sib = slots[0].next_sibling
    while sib is not None and getattr(sib, "name", None) is None:
        sib = sib.next_sibling
    if sib is not None and getattr(sib, "name", None) and _DIVIDER_CLASS_RE.search(_cls(sib)):
        divider_tpl = sib

    import copy as _copy
    changes = []
    # 1) reassign the slots we already have
    for i in range(min(n_sec, n_slot)):
        _assign_footer_slot(slots[i], sections[i], changes)
    # 2) extra sections -> clone the last slot (with a divider in front) and append
    if n_sec > n_slot:
        tail = slots[-1]
        for j in range(n_slot, n_sec):
            if divider_tpl is not None:
                d = _copy.copy(divider_tpl)
                tail.insert_after(d)
                tail = d
            s = _copy.copy(slots[0])
            tail.insert_after(s)
            tail = s
            _assign_footer_slot(s, sections[j], changes)
    # 3) surplus slots -> remove them and an adjacent divider
    if n_slot > n_sec:
        for k in range(n_sec, n_slot):
            sl = slots[k]
            prev = sl.previous_sibling
            while prev is not None and getattr(prev, "name", None) is None:
                prev = prev.previous_sibling
            if prev is not None and getattr(prev, "name", None) and _DIVIDER_CLASS_RE.search(_cls(prev)):
                prev.extract()
            sl.extract()
    return changes


def auto_link_menu(site_dir, keep_header_items=False):
    """Wire up header/footer/mobile nav across every page. See NAV_LINKING.md for the full spec;
    the short version, and the two rules that MUST hold (a past bug got them wrong):

      - HEADER: a CURATED menu, capped at 4 in-page anchors. Points each broken link (#, /,
        javascript:void, empty) at the best-matching section (word overlap vs heading/class/id/
        .section-tag, else next unused), short label (About, How...). *** The header MUST always
        carry a link to the HERO (the first/top block) *** - guaranteed even if nothing was broken.
      - FOOTER: the FULL sitemap - REBUILT to hold one in-page anchor per section, ALL of them
        (unlike the capped header), labeled by section name, reusing the footer's own list markup
        and dividers. Whatever the original footer links were (dead policy/PDF links etc.) they
        become the section list. Never leaves an empty '#'.
      - MOBILE nav (duplicate menu): mirror the header's href+label onto matching links.

    The LOGO gets the absolute canonical-domain home URL; every other nav href is a relative
    in-page anchor. Sections come from _content_sections (hero INCLUDED, flagged is_hero). Backs
    up each edited page (.studio-bak). Returns a summary."""
    html_paths = sorted(site_dir.rglob("*.html"))
    if not html_paths:
        return {"linked": [], "relabeled": [], "removed": [], "unresolved": []}

    page_index = []
    for pp in html_paths:
        w = _slug_words(re.sub(r"[_-]", " ", pp.stem))
        if w:
            page_index.append((w, pp.relative_to(site_dir).as_posix()))

    def _page_match(words):
        for pw, rel in page_index:
            if pw == words:
                return rel
        if words == ["home"]:
            return next((rel for _, rel in page_index if rel.lower() == "index.html"), None)
        return None

    linked, relabeled, removed, unresolved = [], [], [], []

    import clean_wayback_site as cw

    for p in html_paths:
        soup = _read_soup(p)
        rel_self = p.relative_to(site_dir).as_posix()
        bare = cw._bare_domain(cw._domain_from_folder_name(p) or cw.get_site_domain(soup) or "")
        # Full form (keeps www if the folder is named www.<domain>) - the header "Home" link
        # points at this as an absolute canonical-domain URL.
        site_domain_full = (cw._domain_from_folder_name(p) or cw.get_site_domain(soup) or "")
        sections = _content_sections(soup)
        _classify_sections_ai(sections)  # best-effort: better kinds/labels when a key is present
        used = set()
        dirty = False
        header_map = {}  # slug(original text) -> (href, label)

        # The LOGO is the real "home" link -> absolute canonical-domain URL (www/non-www form
        # from the folder name). The nav menu below stays in-page anchors only.
        logo = _find_logo_link(soup)
        if logo is not None and site_domain_full:
            home_url = f"https://{site_domain_full}/"
            if logo.get("href") != home_url:
                logo["href"] = home_url
                linked.append(f'{rel_self}: logo -> {home_url}')
                dirty = True

        def _broken(a, _bare=bare):
            href = (a.get("href") or "").strip()
            low = href.lower()
            if low in _NAV_UNRESOLVED_HREFS or low.startswith("javascript"):
                return True
            parts = safe_urlsplit(href)
            netloc = parts.netloc.lower()
            if netloc.startswith("www."):
                netloc = netloc[4:]
            if netloc and _bare and netloc != _bare:
                # A third-party link in the HEADER/FOOTER: a restored page shouldn't send anyone
                # off-site from its own menu, so treat it as broken - it gets re-pointed at an
                # in-page section and relabelled like any other lost menu item. (Off-menu external
                # links are handled by the cleaner, which just strips their href.)
                return True
            # points at the site root / itself with no real target (/, /#, https://site/,
            # https://site/#) - a placeholder nav item, treat as broken. A real "#anchor"
            # (non-empty fragment) or a "/page" path is left alone.
            if (parts.path or "") in ("", "/") and (parts.fragment or "") in ("", "#") and not parts.query:
                return True
            return False

        def _resolve(a, short):
            orig = a.get_text(" ", strip=True)
            words = _slug_words(orig)
            if not words:
                return None
            if short:
                # Header nav is ALWAYS in-page section anchors - never a page/absolute link, and
                # never special-cased by NAME: "Home" / "Startseite" / "الرئيسية" all map to a
                # section the same way (by word-overlap, else next-in-order). The site language
                # is unknowable, so we never key off an English word. The LOGO (handled above)
                # carries the absolute home-domain link.
                pass  # -> fall through to _best_section
            else:
                pm = _page_match(words)
                if pm:
                    a["href"] = os.path.relpath(site_dir / pm, p.parent).replace(os.sep, "/")
                    return a.get_text(" ", strip=True)
            sec = _best_section(orig, sections, used)
            if not sec:
                return None
            used.add(id(sec["el"]))
            a["href"] = "#" + _ensure_section_anchor(sec)
            # Label the menu item after the SECTION it now points to (its eyebrow/heading) -
            # the section title describes the destination better than a lost link's own text.
            # Header gets the short form, footer the fuller one; fall back to the link's own
            # text only if the section has no title at all.
            # Prefer the AI menu label (already short + in the page's language); else derive one
            # from the section title (short form for the header, fuller for the footer).
            ai = sec.get("ai_label")
            title = ai or sec["text"] or orig
            label = ai or (_short_label(title) if short else title)
            _set_link_text(a, label)
            return label

        # --- header: cap at 4, resolve the broken ones, drop the excess/unresolvable ---
        # ...UNLESS it's a genuine CMS menu (WordPress &c): that's the site's real multi-page nav,
        # already curated and richly labelled - capping/relabelling it would gut it. Leave it as-is
        # (its dead sub-page links were already neutered to '#' by the cleaner, labels preserved).
        header_nav = _find_header_nav(soup)
        if header_nav is not None and _is_cms_menu(header_nav):
            header_nav = None  # hands off - don't wire, don't cap, don't relabel, don't generate
        if header_nav is not None:
            kept = 0
            orphans = []
            for a in _nav_menu_links(header_nav):
                orig = a.get_text(" ", strip=True)
                key = " ".join(_slug_words(orig))
                if not keep_header_items and kept >= _HEADER_MAX_LINKS:
                    _remove_nav_item(a)
                    removed.append(f'{rel_self}: header "{orig}" (over {_HEADER_MAX_LINKS})')
                    dirty = True
                    continue
                if not _broken(a):
                    kept += 1
                    header_map[key] = (a.get("href"), a.get_text(" ", strip=True))
                    continue
                label = _resolve(a, short=True)
                if label is not None:
                    kept += 1
                    header_map[key] = (a["href"], label)
                    linked.append(f'{rel_self}: header "{orig}" -> {a["href"]} ("{label}")')
                    dirty = True
                else:
                    # Don't delete yet: on a site with no sections to anchor to, EVERY item is
                    # unresolvable and deleting them all guts the visible menu (sylhet went from
                    # 8 items to 1). Decide after the loop, once we know how many survive.
                    orphans.append((a, orig))

            # A menu is part of how the page LOOKS, so it must never be emptied to satisfy the
            # linking pass. Unresolvable items are dropped only while at least two real menu items
            # survive; otherwise they stay put and merely lose their href, so the bar still reads
            # like the site's menu instead of a lone orphaned word.
            # The menu must mirror what the page actually HAS. Once the sections run out, the
            # remaining menu items point nowhere, and a bar full of dead words is worse than a
            # short honest one - so they go. (An earlier version kept them href-less to avoid
            # "gutting" the menu; that just left rows of unclickable text.) If this empties the
            # menu completely the real fault is upstream - the page produced no sections - and the
            # header invariant will generate a proper one.
            if orphans:
                # Two different situations, and they need opposite handling:
                #  - the page HAS sections and the menu simply has more items than there are
                #    targets -> the extra items lead nowhere, remove them (this is the rule).
                #  - the page has NO sections at all (old table layout, no headings) -> removing
                #    them empties the menu completely and the header ships as a blank bar, which
                #    is strictly worse than a menu whose items don't scroll anywhere. Keep the
                #    items, just drop the dead href. Regeneration cannot save this case: a
                #    <header> already exists, so every generate branch is gated off.
                if sections and not keep_header_items:
                    for a, orig in orphans:
                        _remove_nav_item(a)
                        removed.append(f'{rel_self}: header "{orig}" (нет секции — удалён)')
                else:
                    # keep_header_items (owner switch), or no sections at all -> never delete a header
                    # item; the ones that couldn't be anchored just lose their href (plain text label).
                    for a, orig in orphans:
                        if a.get("href"):
                            del a["href"]
                    why = "оставить пункты хедера (свич)" if keep_header_items else "на странице нет секций"
                    relabeled.append(f'{rel_self}: {why} — {len(orphans)} пунктов меню оставлены без href')
                dirty = True

        # The header MUST carry a link to the HERO (the first/top block). Resolving broken links
        # already sends the first one there (hero is the first unused section), but if the header
        # had nothing broken to resolve, force its first menu item onto the hero so "top" is always
        # reachable from the menu.
        if header_nav is not None and sections:
            hero = next((s for s in sections if s.get("is_hero")), sections[0])
            hero_href = "#" + _ensure_section_anchor(hero)
            hlinks = _nav_menu_links(header_nav)
            if hlinks and not any((a.get("href") or "") == hero_href for a in hlinks):
                a0 = hlinks[0]
                orig0 = a0.get_text(" ", strip=True)
                a0["href"] = hero_href
                if _is_generic_label(orig0):
                    _set_link_text(a0, _section_label(hero) or orig0)
                used.add(id(hero["el"]))
                header_map[" ".join(_slug_words(orig0))] = (hero_href, a0.get_text(" ", strip=True))
                linked.append(f'{rel_self}: header "{orig0}" -> {hero_href} (hero link guaranteed)')
                dirty = True

        # No header AT ALL on this page -> GENERATE a simple one (logo + anchor nav + burger),
        # tinted with the site's own colours, auto light/dark. See header_gen / NAV_LINKING.md.
        # But NOT if the site already has a menu our text-based detector just couldn't wire (an
        # image/CSS menu) - stacking a generated header on top of it duplicates the real nav.
        # NOTE: deliberately NOT gated on `sections`. Old table-layout sites have no <section> and
        # no headings at all, so there's nothing to anchor to - but they still need a header, and
        # build_header ships the logo bar alone in that case.
        # INVARIANT: a cleaned page must never ship without a <header>. The old condition left a
        # hole - when the site HAS a menu but nothing managed to wrap it, generation was suppressed
        # (rightly, to avoid a duplicate nav) and no header was built either, so bikenfoot and
        # sylhetcitycorporation came out with none at all. Wrap the site's own menu instead: it is
        # the real header, and it beats bolting a second one on top of it.
        if soup.find("header") is None:
            existing_menu = _site_menu_element(soup)
            if existing_menu is not None:
                if existing_menu.name == "header":
                    pass
                else:
                    hdr = soup.new_tag("header")
                    existing_menu.insert_before(hdr)
                    hdr.append(existing_menu.extract())
                    if hdr.find("nav") is None and existing_menu.name in ("div", "ul"):
                        existing_menu.name = "nav"
                    linked.append(f"{rel_self}: WRAPPED existing site menu in <header>")
                    dirty = True

        if (header_nav is None and soup.find("header") is None
                and not _has_site_menu(soup)):
            try:
                import header_gen
                info = header_gen.build_header(
                    soup, p, sections, site_domain_full or bare,
                    ensure_anchor=_ensure_section_anchor, section_label=_section_label,
                )
                if info:
                    linked.append(f'{rel_self}: GENERATED header (variant {info["variant"]} '
                                  f'"{info["variant_name"]}", accent {info["accent_light"]}, '
                                  f'nav={info["nav"]})')
                    dirty = True
            except Exception as ex:  # noqa: BLE001 - header generation is best-effort
                unresolved.append(f'{rel_self}: header generation failed: {ex}')

        # Last resort, so "there is always a header" holds even if every branch above declined or
        # threw: no <header> on the page at this point means we build one, no conditions attached.
        if soup.find("header") is None:
            try:
                import header_gen
                info = header_gen.build_header(
                    soup, p, sections, site_domain_full or bare,
                    ensure_anchor=_ensure_section_anchor, section_label=_section_label,
                )
                if info:
                    linked.append(f'{rel_self}: GENERATED header (last-resort invariant)')
                    dirty = True
                else:
                    unresolved.append(f'{rel_self}: НЕТ <header> — build_header вернул пусто')
            except Exception as ex:  # noqa: BLE001
                unresolved.append(f'{rel_self}: НЕТ <header> — генерация упала: {ex}')

        # --- mobile navs: mirror header decisions onto matching links ---
        exclude = {id(header_nav)} if header_nav is not None else set()
        footer = soup.find("footer")
        if footer is not None:
            exclude.add(id(footer))
        for mn in _find_mobile_navs(soup, exclude):
            for a in _nav_menu_links(mn):
                key = " ".join(_slug_words(a.get_text(" ", strip=True)))
                if key in header_map and header_map[key][0]:
                    a["href"] = header_map[key][0]
                    _set_link_text(a, header_map[key][1])
                    relabeled.append(f'{rel_self}: mobile "{key}" -> {header_map[key][0]}')
                    dirty = True

        # --- footer = the site's FULL sitemap: one in-page anchor per section, ALL of them (the
        #     header is capped/curated; the footer lists everything). We rebuild the footer's nav
        #     block from the section list rather than patching each stale policy link, so the
        #     result is always exactly "every section, anchored + labeled" no matter what the
        #     original footer links were (Privacy/Payment/Refund PDFs etc.). ---
        # (Not for a CMS footer menu - like the header, that's the site's real nav; leave it.)
        # Если в футере ВООБЩЕ нет блока ссылок — его надо создать. Карта сайта в футере это
        # правило владельца («футер = ВСЕ секции»), а не улучшение по возможности. Раньше карта
        # строилась только поверх уже существующих ссылок, поэтому созданный или бедный футер
        # (sylhet, tuonggo, bikenfoot) оставался пустым.
        if (footer is not None and sections and not _is_cms_menu(footer)
                and not _looks_like_footer_nav(footer)):
            _fnav = soup.new_tag("nav")
            _fnav["class"] = ["wb-footer-nav"]
            for _sec in sections:
                _a = soup.new_tag("a", href="#" + _ensure_section_anchor(_sec))
                _a.string = (_sec.get("ai_label") or _sec.get("text") or "").strip()[:60] or "Раздел"
                _fnav.append(_a)
            _cp = footer.find("p", class_="site-copyright")
            if _cp is not None:
                _cp.insert_before(_fnav)
            else:
                footer.insert(0, _fnav)
            linked.append(f"{rel_self}: в футере СОЗДАНА карта сайта ({len(sections)} разделов)")
            dirty = True

        if (footer is not None and _looks_like_footer_nav(footer) and sections
                and not _is_cms_menu(footer)):
            fchanges = _rebuild_footer_sitemap(footer, sections)
            if fchanges:
                for c in fchanges:
                    linked.append(f'{rel_self}: footer {c}')
                dirty = True

        # If the menu still has no working in-page navigation, build it from the sections. Matching
        # original labels is a nice-to-have; a header whose items lead nowhere is not acceptable.
        # Skipped under keep_header_items: the owner asked to keep the original items even hrefless,
        # so we must NOT replace them with a fresh section-menu.
        _hnav = _find_header_nav(soup) or soup.find("header")
        if _hnav is not None and sections and not keep_header_items:
            _anchored = [a for a in _nav_menu_links(_hnav) if _is_live_anchor(a, soup)]
            # Rebuild when the menu is mostly dead, not only when it is completely dead. Two
            # working anchors out of forty is not navigation.
            _all_items = _nav_menu_links(_hnav)
            if len(_anchored) < min(3, len(sections)) or len(_anchored) < len(_all_items) * 0.5:
                _n = _rebuild_nav_from_sections(_hnav, sections, _HEADER_MAX_LINKS)
                if _n:
                    linked.append(f"{rel_self}: меню пересобрано из секций ({_n} пунктов) — "
                                  f"исходные подписи никуда не вели")
                    dirty = True

        # Footer links follow the SAME rule as the header: once the sections run out, a menu item
        # that points nowhere is dead weight and gets removed rather than kept as unclickable text.
        if footer is not None and not _is_cms_menu(footer):
            fsections = len(sections)
            fkept = 0
            for a in list(_nav_menu_links(footer)):
                if not _broken(a):
                    fkept += 1
                    continue
                if fkept < fsections:
                    fkept += 1
                    continue
                _remove_nav_item(a)
                removed.append(f'{rel_self}: footer "{a.get_text(" ", strip=True)[:24]}" (нет секции — удалён)')
                dirty = True

        # STRUCTURE IS A GUARANTEE: header/main/footer must exist on EVERY page, including ancient
        # table layouts that have no closing bar to promote. If nothing could be turned into a
        # footer, build one - an empty structural slot is not acceptable, and the copyright line
        # below gives it real content.
        if soup.find("footer") is None and soup.body is not None:
            new_ftr = soup.new_tag("footer")
            new_ftr["class"] = ["wb-footer"]
            soup.body.append(new_ftr)
            linked.append(f"{rel_self}: СОЗДАН <footer> (на странице его не было)")
            dirty = True
        footer = soup.find("footer")

        # Every restored page ends with a copyright line. PBN pages get published as-is, and a site
        # with no closing line reads as unfinished; the archive often lost it with the widget that
        # rendered it. Written only when the footer has none - never duplicated.
        if footer is not None:
            if _ensure_footer_copyright(footer, site_domain_full or bare, soup):
                linked.append(f"{rel_self}: footer — добавлена строка копирайта")
                dirty = True

        # --- final sweep: any link STILL pointing nowhere (dead "#"/"/"/empty/js) that the
        #     header/footer passes didn't touch - hero CTA buttons ("Get Started"), stray
        #     placeholders, icon-only social links. A link WITH visible text gets a section anchor
        #     (contextual word-match, else the first/any section); an icon-only link (social, no
        #     text) just loses its dead href so it stops looking clickable. ---
        # Deterministic matching first; whatever it leaves unmatched goes to the model. Word overlap
        # cannot connect an Urdu menu to English headings, and the result was 40 items with neither
        # an anchor nor a removal - the worst of both.
        _ai_map = {}
        if sections:
            _pending = []
            for _a in soup.find_all("a"):
                _low = (_a.get("href") or "").strip().lower()
                if _a.has_attr("href"):
                    if _low not in _NAV_UNRESOLVED_HREFS and not _low.startswith("javascript:"):
                        continue
                elif _a.find_parent(["header", "nav", "footer"]) is None:
                    continue
                _txt = _a.get_text(" ", strip=True)
                if not _txt:
                    continue
                if _matched_section(_txt, sections) is None:
                    _pending.append(_a)
            if _pending:
                try:
                    import semantics
                    if semantics.available():
                        _res = semantics.match_menu_to_sections(
                            [x.get_text(" ", strip=True) for x in _pending],
                            [s.get("text") or "" for s in sections])
                        for _a, _si in zip(_pending, _res or []):
                            if _si is not None and 0 <= _si < len(sections):
                                _ai_map[id(_a)] = sections[_si]
                        if _ai_map:
                            linked.append(f"{rel_self}: агент привязал {len(_ai_map)} пунктов "
                                          f"меню по смыслу (совпадений по словам не было)")
                except Exception:  # noqa: BLE001 - no key -> deterministic behaviour unchanged
                    pass

        _anchor_use = {}  # section-element id -> how many links already point at it
        # Also take <a> that has NO href at all. An earlier pass (external-link stripping) removes
        # the attribute outright, and everything here only ever looked at a[href] - so those items
        # were invisible to the whole wiring stage: never anchored, never removed, left as dead
        # words in the menu. That is exactly what sanjhapunjab shipped: 40 such items.
        for a in list(soup.find_all("a")):
            low = (a.get("href") or "").strip().lower()
            if a.has_attr("href"):
                if low not in _NAV_UNRESOLVED_HREFS and not low.startswith("javascript"):
                    continue
            elif a.find_parent(["header", "nav", "footer"]) is None:
                continue  # a hrefless <a> in body text is not a menu item - leave it alone
            if _is_dropdown_toggle(a) and a.find_next_sibling(["ul", "div"]) is not None:
                continue  # a toggle that still HAS a submenu legitimately uses "#" - leave it
            # ...but a toggle whose submenu went away with the JS toggles nothing. It is a dead
            # menu item like any other, so it gets a section anchor and the section's name.
            txt = a.get_text(" ", strip=True)
            if not txt:
                # icon-only dead link (social etc.) -> drop the dead href, keep the empty <a>
                del a["href"]
                relabeled.append(f'{rel_self}: dropped dead href on icon-only <a> ({_cls(a)[:24]})')
                dirty = True
                continue
            # Only anchor a link that GENUINELY matches a section. Falling back to "the first
            # section" pointed a 135-item mega-menu at one and the same anchor - worse than
            # leaving it dead, and it hides the fact that nothing matched.
            sec = _matched_section(txt, sections) if sections else None
            if sec is None:
                sec = _ai_map.get(id(a))  # смысловая привязка от агента
            # One section must not swallow the whole menu. On danvanhaiphong the org's name
            # ("dân vận") is both the first section's heading AND part of dozens of menu labels,
            # so 45 items "matched" the same anchor - a technically-real overlap that means
            # nothing. Past a few links the match is noise: treat the rest as unmatched.
            if sec is not None:
                key = id(sec["el"])
                if _anchor_use.get(key, 0) >= _MAX_LINKS_PER_SECTION:
                    sec = None
                else:
                    _anchor_use[key] = _anchor_use.get(key, 0) + 1
            if sec is not None:
                a["href"] = "#" + _ensure_section_anchor(sec)
                # Rename to the section. The original label pointed at a sub-page that no longer
                # exists, so keeping it just lies about where the link goes. Header gets the SHORT
                # form (a narrow bar), the footer the FULL section title (it is the sitemap).
                _in_footer = a.find_parent("footer") is not None
                _new_label = (sec.get("ai_label")
                              or (sec.get("text") if _in_footer else _short_label(sec.get("text") or ""))
                              or sec.get("text") or "")
                if _new_label:
                    _set_link_text(a, _new_label.strip())
                linked.append(f'{rel_self}: "{txt[:20]}" -> {a["href"]} ("{_new_label[:20]}")')
            elif _in_protected_cms_menu(a):
                # The site's REAL multi-page menu (WordPress &c). Deleting its items guts the
                # site's navigation - keep every item, just stop it being a dead link.
                del a["href"]
                relabeled.append(f'{rel_self}: cms-menu "{txt[:24]}" (href dropped, item kept)')
            elif keep_header_items and a.find_parent("header") is not None:
                # Owner switch "не удалять пункты хедера": a header item with nothing to anchor to
                # stays put and merely loses its href (plain-text label), never removed.
                if a.has_attr("href"):
                    del a["href"]
                relabeled.append(f'{rel_self}: header "{txt[:24]}" (пункт оставлен без href)')
            elif a.find_parent(["header", "nav", "footer"]) is not None:
                # Nothing to point at, and it sits in the menu -> a menu item that leads nowhere
                # is pure noise on a restored single-page site. Drop it.
                _remove_nav_item(a)
                removed.append(f'{rel_self}: nav "{txt[:24]}" (no matching section, removed)')
            else:
                # In the page body keep the text/layout, just stop pretending it's a link.
                del a["href"]
                relabeled.append(f'{rel_self}: stripped dead href on "{txt[:24]}"')
            dirty = True

        # No two menu items may carry the SAME text AND the same anchor. firsttalk shipped three
        # identical "Latest Article" links all pointing at #featured - that is not navigation, it is
        # the same item repeated. Keep the first, drop the rest.
        for _navc in soup.find_all(["nav", "header", "footer"]):
            _seen_pairs = set()
            for _a in list(_navc.find_all("a", href=True)):
                _h = (_a.get("href") or "").strip()
                if not _h.startswith("#"):
                    continue
                _key = (_h, _a.get_text(" ", strip=True).lower())
                if _key in _seen_pairs:
                    _remove_nav_item(_a)
                    removed.append(f'{rel_self}: nav дубль "{_key[1][:18]}" -> {_h}')
                    dirty = True
                else:
                    _seen_pairs.add(_key)

        # FINAL CLEANUP: nothing in a menu may be left without an href. By this point every item
        # that could be anchored has been; whatever still has no href leads nowhere, and dead words
        # in a menu bar are worse than a shorter menu (owner's rule). Typically these are the
        # submenu items of a mega-menu whose parent items now point at sections - sanjhapunjab kept
        # 36 of them. Guarded so the menu is never emptied completely.
        for _navc in soup.find_all(["nav", "header", "footer"]):
            # keep_header_items (owner switch): header items with no anchor MUST stay as plain-text
            # labels - so don't strip the dead ones out of the header here.
            if keep_header_items and (_navc.name == "header" or _navc.find_parent("header") is not None):
                continue
            # Считать живые ссылки по ВСЕМУ контейнеру, а не только по верхнему уровню меню.
            # У мега-меню (firsttalk: 7 верхних пунктов + 57 во вложенных списках) верхний уровень
            # мог дать всего одну живую ссылку, страховка «не опустошать меню» срабатывала, и все
            # 57 мёртвых подпунктов оставались на странице.
            _live = [a for a in _navc.find_all("a") if (a.get("href") or "").strip()]
            _dead = [a for a in _navc.find_all("a")
                     if not (a.get("href") or "").strip() and a.get_text(strip=True)]
            if not _dead or len(_live) < 2:
                continue
            for _a in _dead:
                _remove_nav_item(_a)
                removed.append(f'{rel_self}: nav "{_a.get_text(" ", strip=True)[:20]}" '
                               f'(без ссылки — удалён)')
            dirty = True

        if dirty:
            _write_soup(p, soup)

        # FINAL landmark check - here, not inside the cleaner, because this is the first moment the
        # markup is actually finished (the header can still be generated a few lines above). Checked
        # earlier it reported "НЕТ <header>" on pages that ended up with a perfectly good one.
        final = _read_soup(p)
        # The FULL contract, checked here rather than inside the cleaner. The cleaner runs before
        # this function, so its own check could not see what happens below - the header cap, the
        # deletion of unresolvable menu items, the rebuilt footer sitemap. The rule written to catch
        # a gutted menu was blind to the very pass that guts menus. This is the last moment the
        # markup changes, so it is the only place the contract means anything.
        try:
            import clean_wayback_site as _cw

            class _R:
                audit_warnings = []

            _rep = _R()
            for _v in _cw.verify_output_contract(final, _rep):
                unresolved.append(f"{rel_self}: КОНТРАКТ — {_v}")
        except Exception as _e:  # noqa: BLE001 - a failing check must never fail the clean
            for _tag in ("header", "main", "footer"):
                if final.find(_tag) is None:
                    unresolved.append(f"{rel_self}: НЕТ <{_tag}> в готовой странице")

    return {"linked": linked, "relabeled": relabeled, "removed": removed, "unresolved": unresolved}


def format_for_upload(html_path, clean_unused=True):
    """Format the site folder in place so it's upload-ready - no zip, no copy, just the
    live site left in the same directory. Deletes the backup files (.bak/.studio-bak/
    .pre-revert), the leftover *.cleanup-report.txt, and every _wayback_removed/ and
    _unused_removed/ quarantine folder outright (searched anywhere in the tree - they
    live inside the "<name>_files" assets folder, not the site root). Also flattens
    any "*_recovered" folder up into its parent (see flatten_recovered_assets) - those
    hold live referenced assets, so they're merged in, not deleted. With
    clean_unused=True the unused-asset sweep runs first (moving orphans into
    _unused_removed) so unreferenced files end up gone for good, not just quarantined.
    Destructive: after this, 'revert to backup' no longer has anything to restore from."""
    site_dir = html_path.parent
    result = {"removed_files": [], "removed_dirs": []}

    if clean_unused:
        try:
            result["unused_swept"] = remove_unused_assets(html_path)
        except Exception as e:  # noqa: BLE001 - formatting shouldn't die on a sweep hiccup
            result["unused_swept_error"] = str(e)

    try:
        result["flattened_recovered"] = flatten_recovered_assets(html_path)
    except Exception as e:  # noqa: BLE001 - formatting shouldn't die on a flatten hiccup
        result["flattened_recovered_error"] = str(e)

    # Tidy accumulated blank-line runs in every HTML page (legacy whitespace, CRLF gaps from
    # the raw download, etc.) - formatting for upload should leave clean files, not bloated ones.
    result["tidied"] = []
    for hp in list(site_dir.rglob("*.html")):
        if any(part in ("_wayback_removed", "_unused_removed") for part in hp.relative_to(site_dir).parts):
            continue
        try:
            txt = read_text_safe(hp)
            tidy = collapse_blank_lines(txt)
            if tidy != txt:
                hp.write_text(tidy, encoding="utf-8")
                result["tidied"].append(hp.relative_to(site_dir).as_posix())
        except OSError:
            pass

    for p in list(site_dir.rglob("*")):
        if p.is_file() and (p.suffix.lower() in _FORMAT_REMOVE_SUFFIXES or p.name.endswith(".cleanup-report.txt")):
            try:
                p.unlink()
                result["removed_files"].append(p.relative_to(site_dir).as_posix())
            except OSError:
                pass

    for name in _FORMAT_REMOVE_DIR_NAMES:
        for dd in list(site_dir.rglob(name)):
            if dd.is_dir():
                shutil.rmtree(dd, ignore_errors=True)
                result["removed_dirs"].append(dd.relative_to(site_dir).as_posix() + "/")

    return result

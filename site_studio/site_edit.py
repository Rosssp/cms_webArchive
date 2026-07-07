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
sys.path.insert(0, str(Path(__file__).resolve().parent))
from clean_wayback_site import BeautifulSoup, PARSER, _ensure_pillow, normalize_font_family, read_text_safe  # noqa: E402

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
_NAV_UNRESOLVED_HREFS = {"#", "", "javascript:void(0)", "javascript:void(0);", "javascript:;"}


def _slug_words(text, max_words=None):
    text = (text or "").strip().lower().replace("’", "'")
    words = re.findall(r"[a-z0-9]+", text)
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


def _collect_nav_links(soup):
    """Every <a> inside <header>/<nav>/<footer> - the "menu items that almost always
    lead nowhere in a wayback export" the user means, as opposed to a random <a> in
    the middle of body content (which auto_link_menu should never touch) - except a
    dropdown-toggle button, whose '#' is functional, not broken."""
    seen, links = set(), []
    for tag_name in NAV_CONTAINER_TAGS:
        for container in soup.find_all(tag_name):
            for a in container.find_all("a"):
                if id(a) not in seen and not _is_dropdown_toggle(a):
                    seen.add(id(a))
                    links.append(a)
    return links


def _page_sections(soup):
    """Real content section headings (h1-h6) on the page, in document order -
    excludes anything inside <header>/<nav>/<footer> itself (a nav label is not a
    "section" to link other nav labels to). These are the ONLY valid link targets
    for auto_link_menu's relabeling pass: real content that actually exists,
    as opposed to whatever arbitrary label a lost JS-driven menu used to carry."""
    nav_descendant_ids = set()
    for tag_name in NAV_CONTAINER_TAGS:
        for c in soup.find_all(tag_name):
            nav_descendant_ids.update(id(x) for x in c.find_all(True))
    out = []
    for h in soup.find_all(HEADING_TAGS):
        if id(h) in nav_descendant_ids:
            continue
        text = re.sub(r"\s+", " ", h.get_text(" ", strip=True)).strip()
        words = _slug_words(text)
        if not words or all(w.isdigit() for w in words):
            # A bare number is a slide/carousel counter badge ("1", "2", "4"...),
            # not a real section title - a menu item pointing at "4" would be nonsense.
            continue
        out.append((h, text))
    return out


def auto_link_menu(site_dir):
    """Header/footer/nav <a> links in a wayback export overwhelmingly just point at
    '#' - matching their OWN (often meaningless, sometimes lost-JS-menu) label
    against page content almost never finds anything real. So this goes the other
    way: (1) if the link's text happens to match an uploaded page's filename (e.g.
    "Gallery" matches gallery.html, "Home" always falls back to index.html) - link
    there, relative, keeping the label as-is, since that IS a real, correctly-named
    target; (2) otherwise, claim the next unused real content section (h1-h6, in
    document order, never one from inside a nav/header/footer itself) on the SAME
    page - assign it an id if it doesn't have one, point the link there, and RENAME
    the link's text to that section's actual heading text, since the menu item
    should describe what it now points to, not the other way around; (3) once a
    page runs out of sections, anything left just stays unresolved rather than
    reusing/guessing - logged so it's obvious what still needs real content. Every
    resulting href is relative - no hardcoded absolute URL, ever. Only touches
    already-broken links (#, empty, javascript:void(0) and the like) - never
    disturbs a link that already goes somewhere. Backs up every page it touches
    (.studio-bak, same convention as every other studio edit). Returns a summary."""
    html_paths = sorted(site_dir.rglob("*.html"))
    if not html_paths:
        return {"linked": [], "relabeled": [], "unresolved": []}

    soups = {}
    page_index = []  # (words, rel_path)
    for p in html_paths:
        rel = p.relative_to(site_dir).as_posix()
        soup = _read_soup(p)
        soups[p] = soup
        stem_words = _slug_words(re.sub(r"[_-]", " ", p.stem))
        if stem_words:
            page_index.append((stem_words, rel))

    def _find_page_match(words):
        for pw, rel in page_index:
            if pw == words:
                return rel
        return None

    linked, relabeled, unresolved = [], [], []
    dirty = set()

    for p in html_paths:
        soup = soups[p]
        rel_self = p.relative_to(site_dir).as_posix()
        sections = _page_sections(soup)  # consumed left-to-right as this page's links claim them
        section_i = 0

        for a in _collect_nav_links(soup):
            href = (a.get("href") or "").strip()
            if href.lower() not in _NAV_UNRESOLVED_HREFS:
                continue
            orig_text = a.get_text(" ", strip=True)
            words_full = _slug_words(orig_text)
            if not words_full:
                continue

            page_match = _find_page_match(words_full)
            if not page_match and words_full == ["home"]:
                # "Home" is such a universal nav-link convention for "the site's own
                # index page" that it's worth a hardcoded fallback even when no page
                # is literally named home.html.
                index_rel = next((rel for _, rel in page_index if rel.lower() == "index.html"), None)
                if index_rel:
                    page_match = index_rel
            if page_match:
                new_href = os.path.relpath(site_dir / page_match, p.parent).replace(os.sep, "/")
                a["href"] = new_href
                linked.append(f'{rel_self}: "{orig_text}" -> {new_href}')
                dirty.add(p)
                continue

            if section_i < len(sections):
                h, section_text = sections[section_i]
                section_i += 1
                anchor_id = h.get("id") or _slugify(section_text, max_words=3) or f"section-{section_i}"
                if not h.get("id"):
                    h["id"] = anchor_id
                a["href"] = "#" + anchor_id
                for child in list(a.contents):
                    child.extract()
                a.append(section_text)
                relabeled.append(f'{rel_self}: "{orig_text}" -> "{section_text}" (#{anchor_id})')
                dirty.add(p)
                continue

            unresolved.append(f'{rel_self}: "{orig_text}" - на странице закончились реальные секции, не привязано')

    for p in dirty:
        _write_soup(p, soups[p])

    return {"linked": linked, "relabeled": relabeled, "unresolved": unresolved}


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

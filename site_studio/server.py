#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
site_studio - a tiny local control panel for editing a cleaned PBN site export.

    python server.py <path-to-site-folder> [--port 5151]

Then open http://127.0.0.1:5151/ in a browser. From there you can:
  - point it at a site folder (containing index.html)
  - run the wayback/junk cleanup pass (clean_wayback_site.py) with logo/brand/
    favicon/font options, right from the page
  - type a Google Fonts family (e.g. "Jost:wght@400;700") and apply it
  - type an image search query + a CSS selector for the gallery/card wrapper +
    how many images to fetch, and apply - it downloads real images (no API key,
    scraped from Bing Images) and drops them into every <img> found inside
    elements matching that selector
  - see the actual site live in an embedded preview (served from /preview/), which
    reloads after every apply so you see the result immediately
"""

import contextlib
import importlib
import io
import subprocess
import sys
from pathlib import Path

try:
    import flask  # noqa: F401
except ImportError:
    print("[setup] installing missing dependency: flask ...")
    subprocess.check_call([sys.executable, "-m", "pip", "install", "-q", "flask"])
    importlib.invalidate_caches()

from flask import Flask, jsonify, request, send_from_directory, render_template

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import image_providers  # noqa: E402
import site_edit  # noqa: E402
import clean_wayback_site  # noqa: E402

app = Flask(__name__)

STATE = {"site_dir": None, "html_path": None}

_PAGE_SKIP_DIR_NAMES = {"_wayback_removed", "_unused_removed", "node_modules"}


def list_html_pages(site_dir):
    """Every .html file in the site folder (recursive - a page can sit in a
    subfolder, e.g. agb.html at the root next to an about/index.html), relative to
    site_dir, index.html first if present, then alphabetically. Skips the
    quarantine/tooling folders - never real pages."""
    pages = []
    for p in site_dir.rglob("*.html"):
        if any(part in _PAGE_SKIP_DIR_NAMES for part in p.relative_to(site_dir).parts):
            continue
        pages.append(p.relative_to(site_dir).as_posix())
    pages.sort(key=lambda rel: (rel != "index.html", rel.lower()))
    return pages


def set_site(site_dir_str):
    site_dir = Path(site_dir_str).resolve()
    pages = list_html_pages(site_dir)
    if not pages:
        raise ValueError(f"no .html files found in {site_dir}")
    STATE["site_dir"] = site_dir
    STATE["html_path"] = site_dir / pages[0]


def set_page(rel_path_str):
    if not STATE["site_dir"]:
        raise ValueError("select a site folder first")
    site_dir = STATE["site_dir"]
    candidate = (site_dir / rel_path_str).resolve()
    if site_dir not in candidate.parents or not candidate.is_file() or candidate.suffix.lower() != ".html":
        raise ValueError(f"'{rel_path_str}' is not an .html file inside the current site folder")
    STATE["html_path"] = candidate


@app.route("/")
def index():
    return render_template("index.html", default_fonts=clean_wayback_site.DEFAULT_GOOGLE_FONTS)


@app.route("/api/status")
def api_status():
    active_page = None
    pages = []
    if STATE["site_dir"]:
        pages = list_html_pages(STATE["site_dir"])
        if STATE["html_path"]:
            active_page = STATE["html_path"].relative_to(STATE["site_dir"]).as_posix()
    return jsonify(
        {
            "site_dir": str(STATE["site_dir"]) if STATE["site_dir"] else None,
            "pages": pages,
            "active_page": active_page,
        }
    )


@app.route("/api/set-site", methods=["POST"])
def api_set_site():
    try:
        set_site(request.json["site_dir"])
        return jsonify({"ok": True, "site_dir": str(STATE["site_dir"])})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/set-page", methods=["POST"])
def api_set_page():
    try:
        set_page((request.json or {}).get("page") or "")
        return jsonify({"ok": True, "page": STATE["html_path"].relative_to(STATE["site_dir"]).as_posix()})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/pick-folder", methods=["POST"])
def api_pick_folder():
    """Open a native OS folder-picker on the machine running this server (this is a
    local tool, so the server and the user are the same machine) and return the chosen
    absolute path. The browser can't hand back an absolute folder path for security
    reasons, so the dialog has to happen server-side. Tk runs in a throwaway subprocess
    so it never fights Flask's request thread, and the path comes back as raw UTF-8
    bytes to survive non-ASCII (Cyrillic) folder names on Windows."""
    picker_code = (
        "import sys, tkinter as tk\n"
        "from tkinter import filedialog\n"
        "root = tk.Tk()\n"
        "root.withdraw()\n"
        "root.attributes('-topmost', True)\n"
        "path = filedialog.askdirectory(title='Выбери папку сайта (с index.html)')\n"
        "root.destroy()\n"
        "sys.stdout.buffer.write((path or '').encode('utf-8'))\n"
    )
    try:
        proc = subprocess.run([sys.executable, "-c", picker_code], capture_output=True, timeout=300)
    except Exception as e:
        return jsonify({"ok": False, "error": f"не удалось открыть диалог выбора папки: {e}"}), 400
    path = (proc.stdout or b"").decode("utf-8", "replace").strip()
    if not path:
        return jsonify({"ok": False, "error": "папка не выбрана"}), 400
    return jsonify({"ok": True, "path": path})


@app.route("/api/apply-font", methods=["POST"])
def api_apply_font():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    family_raw = ((request.json or {}).get("family") or "").strip()
    if not family_raw:
        return jsonify({"ok": False, "error": "font family is required"}), 400
    family = clean_wayback_site.resolve_font_input(family_raw)
    try:
        result = site_edit.apply_font(STATE["html_path"], family)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/apply-logo", methods=["POST"])
def api_apply_logo():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    auto_logo = bool(body.get("auto_logo"))
    brand_text = (body.get("brand_text") or "").strip() or None
    logo_image = (body.get("logo_image") or "").strip() or None
    color = (body.get("color") or "").strip() or None
    domain = (body.get("domain") or "").strip() or None

    if not auto_logo and not logo_image and not brand_text:
        return jsonify({"ok": False, "error": "укажи brand text, путь к логотипу или включи авто-логотип"}), 400
    if logo_image and not auto_logo and not Path(logo_image).is_file():
        return jsonify({"ok": False, "error": f"logo image not found: {logo_image}"}), 400

    try:
        result = site_edit.apply_logo(
            STATE["html_path"], auto_logo=auto_logo, brand_text=brand_text,
            logo_image=logo_image, color=color, domain_override=domain,
        )
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/find-images", methods=["POST"])
def api_find_images():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    query = (body.get("query") or "").strip()
    selector = (body.get("selector") or "").strip()
    if not query or not selector:
        return jsonify({"ok": False, "error": "query and selector are both required"}), 400

    try:
        suggested_count = min(80, site_edit.count_img_slots(STATE["html_path"], selector))
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        candidates = image_providers.find_candidates(query, limit=max(suggested_count * 3, 24))
    except image_providers.ProviderError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": f"search failed: {e}"}), 400

    return jsonify({"ok": True, "candidates": candidates, "suggested_count": suggested_count})


@app.route("/api/apply-selected-images", methods=["POST"])
def api_apply_selected_images():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    selector = (body.get("selector") or "").strip()
    urls = body.get("urls") or []
    if not selector or not urls:
        return jsonify({"ok": False, "error": "selector and at least one image url are required"}), 400

    try:
        images = image_providers.download_images(urls)
    except image_providers.ProviderError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        result = site_edit.apply_images(STATE["html_path"], selector, images)
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/remove-elements", methods=["POST"])
def api_remove_elements():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    selector = ((request.json or {}).get("selector") or "").strip()
    if not selector:
        return jsonify({"ok": False, "error": "selector is required"}), 400
    try:
        count = site_edit.remove_elements(STATE["html_path"], selector)
        return jsonify({"ok": True, "selector": selector, "removed": count})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/element-info", methods=["POST"])
def api_element_info():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    selector = ((request.json or {}).get("selector") or "").strip()
    if not selector:
        return jsonify({"ok": False, "error": "selector is required"}), 400
    try:
        info = site_edit.get_element_info(STATE["html_path"], selector)
        return jsonify({"ok": True, **info})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/set-element-id", methods=["POST"])
def api_set_element_id():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    selector = (body.get("selector") or "").strip()
    if not selector:
        return jsonify({"ok": False, "error": "selector is required"}), 400
    try:
        result = site_edit.set_element_id(STATE["html_path"], selector, body.get("id"))
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/set-nav-link", methods=["POST"])
def api_set_nav_link():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    selector = (body.get("selector") or "").strip()
    if not selector:
        return jsonify({"ok": False, "error": "selector is required"}), 400
    try:
        result = site_edit.set_nav_link(STATE["html_path"], selector, text=body.get("text"), href=body.get("href"))
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/remove-asset-reference", methods=["POST"])
def api_remove_asset_reference():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    needle = ((request.json or {}).get("path") or "").strip()
    if not needle:
        return jsonify({"ok": False, "error": "path is required"}), 400
    try:
        cleared = site_edit.remove_asset_reference(STATE["html_path"], needle)
        return jsonify({"ok": True, "cleared": cleared})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/find-broken-resources", methods=["POST"])
def api_find_broken_resources():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    try:
        found = site_edit.find_broken_resources(STATE["html_path"])
        return jsonify({"ok": True, "found": found})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/auto-clean-broken-resources", methods=["POST"])
def api_auto_clean_broken_resources():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    try:
        result = site_edit.auto_clean_broken_resources(STATE["html_path"])
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/remove-unused-assets", methods=["POST"])
def api_remove_unused_assets():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    try:
        removed = site_edit.remove_unused_assets(STATE["html_path"])
        return jsonify({"ok": True, "removed": removed})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/compress-images", methods=["POST"])
def api_compress_images():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    try:
        quality = max(1, min(100, int(body.get("quality", 82))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "quality must be a number"}), 400
    try:
        max_dimension = max(200, min(8000, int(body.get("max_dimension", 2000))))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "max_dimension must be a number"}), 400
    try:
        result = site_edit.convert_images_to_webp(STATE["html_path"], quality=quality, max_dimension=max_dimension)
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/cleanup", methods=["POST"])
def api_cleanup():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}

    favicon = (body.get("favicon") or "").strip() or None
    if favicon and not Path(favicon).is_file():
        return jsonify({"ok": False, "error": f"favicon file not found: {favicon}"}), 400

    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            clean_wayback_site.clean_html_file(
                STATE["html_path"],
                (body.get("fonts") or "").strip() or None,
                dry_run=bool(body.get("dry_run")),
                backup=not body.get("no_backup"),
                keep_contact_info=bool(body.get("keep_contact_info")),
                favicon=favicon,
                recover_images=not body.get("no_image_recovery"),
                domain_override=(body.get("domain") or "").strip() or None,
            )
    except Exception as e:
        return jsonify({"ok": False, "error": f"{e}\n\n{buf.getvalue()}"}), 400

    return jsonify({"ok": True, "report": buf.getvalue()})


@app.route("/api/ensure-seo", methods=["POST"])
def api_ensure_seo():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    domain_override = (body.get("domain") or "").strip() or None

    try:
        text = clean_wayback_site.read_text_safe(STATE["html_path"])
        soup = clean_wayback_site.BeautifulSoup(text, clean_wayback_site.PARSER)
        site_domain = domain_override or clean_wayback_site.get_site_domain(soup)
        report = clean_wayback_site.Report()
        clean_wayback_site.ensure_local_seo_files(STATE["html_path"], site_domain, report, dry_run=False)
        clean_wayback_site.ensure_htaccess(STATE["html_path"], report, dry_run=False)
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    def _line(label, created, present):
        if created:
            return f"{label}: создан"
        if present:
            return f"{label}: уже был - не трогал"
        return f"{label}: не удалось создать"

    lines = [
        f"домен: {site_domain or '(не определён - укажи domain override)'}",
        _line("robots.txt", report.robots_created, report.robots_present),
        _line("sitemap.xml", report.sitemap_created, report.sitemap_present),
        _line(".htaccess", report.htaccess_created, report.htaccess_present),
    ]
    return jsonify({"ok": True, "report": "\n".join(lines)})


@app.route("/api/check-url", methods=["POST"])
def api_check_url():
    url = ((request.json or {}).get("url") or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "url is required"}), 400
    try:
        report = clean_wayback_site.check_url_live(url)
        return jsonify({"ok": True, "report": report})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/replace-brand-text", methods=["POST"])
def api_replace_brand_text():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    try:
        result = site_edit.replace_brand_text_everywhere(
            STATE["html_path"],
            (body.get("old_name") or "").strip(),
            (body.get("new_name") or "").strip(),
        )
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/strip-owner-traces", methods=["POST"])
def api_strip_owner_traces():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    try:
        result = site_edit.strip_owner_traces(STATE["html_path"], update_year=body.get("update_year", True))
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/revert-backup", methods=["POST"])
def api_revert_backup():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    try:
        result = site_edit.revert_to_backup(STATE["html_path"], which=(body.get("which") or "auto"))
        return jsonify({"ok": True, **result})
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/format-dir", methods=["POST"])
def api_format_dir():
    if not STATE["html_path"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    body = request.json or {}
    try:
        result = site_edit.format_for_upload(
            STATE["html_path"],
            clean_unused=bool(body.get("clean_unused", True)),
        )
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/api/auto-link-menu", methods=["POST"])
def api_auto_link_menu():
    if not STATE["site_dir"]:
        return jsonify({"ok": False, "error": "select a site folder first"}), 400
    try:
        result = site_edit.auto_link_menu(STATE["site_dir"])
        return jsonify({"ok": True, **result})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 400


@app.route("/preview/")
@app.route("/preview/<path:filename>")
def preview(filename="index.html"):
    if not STATE["site_dir"]:
        return "No site folder selected yet.", 400
    return send_from_directory(STATE["site_dir"], filename)


def main():
    import argparse

    parser = argparse.ArgumentParser(description="Local control panel + live preview for a cleaned site export.")
    parser.add_argument("site_dir", nargs="?", help="Path to the site folder (containing index.html)")
    parser.add_argument("--port", type=int, default=5151)
    args = parser.parse_args()

    if args.site_dir:
        set_site(args.site_dir)

    print(f"site_studio running at http://127.0.0.1:{args.port}/")
    if STATE["site_dir"]:
        print(f"editing: {STATE['site_dir']}")
    else:
        print("no site folder given - pick one in the web UI")

    app.run(host="127.0.0.1", port=args.port, debug=False)


if __name__ == "__main__":
    main()

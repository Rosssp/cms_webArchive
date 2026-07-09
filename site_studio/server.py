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
import os
import subprocess
import sys
import threading
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
import wayback_download  # noqa: E402

app = Flask(__name__)

STATE = {"site_dir": None, "html_path": None}

# Live cleanup progress: the cleanup runs in a background thread and reports phase/percent
# through here; the UI polls /api/cleanup-progress to draw the spinner + percentage. The
# "detail" line is the current file (e.g. "картинка 3 из 8"), parsed live out of the core's
# own stdout progress lines so the UI can show which file it's on without threading a second
# callback all the way through the recovery loops.
CLEANUP = {"running": False, "percent": 0, "action": "", "detail": "", "report": None, "error": None}
CLEANUP_LOCK = threading.Lock()

# The landing screen's per-card registry. Each pasted archive URL becomes ONE entry that
# lives for the whole lifecycle: download -> (optional) clean -> done. All state lives here
# server-side (not in the browser), so a page refresh just re-reads /api/entries and the UI
# rebuilds every card exactly where it was - downloads and cleanups keep running in their
# background threads regardless of what the browser does.
#
# Each entry (keyed by a short id):
#   id, url, domain, site_dir,
#   dl_pct, dl_phase, dl_done, dl_error,
#   clean_running, clean_pct, clean_action, clean_done, clean_error, clean_cancelled,
#   preview (bool: is there a _preview.png to show)
ENTRIES = {}
ENTRIES_LOCK = threading.Lock()
_CANCEL = {}  # entry id -> threading.Event, set to abort a running cleanup
_ENTRY_SEQ = [0]
_DL_SEM = threading.Semaphore(2)  # downloads are browser-heavy - cap concurrent ones at 2
_WB_DEST = {"path": None}  # last chosen download folder (restored on refresh)

import re as _re  # noqa: E402


def _new_entry(url):
    _ENTRY_SEQ[0] += 1
    eid = f"e{_ENTRY_SEQ[0]}"
    ENTRIES[eid] = {
        "id": eid, "url": url, "domain": "", "site_dir": None,
        "dl_pct": 0, "dl_phase": "в очереди", "dl_done": False, "dl_error": None,
        "clean_running": False, "clean_pct": 0, "clean_action": "",
        "clean_done": False, "clean_error": None, "clean_cancelled": False,
        "preview": False,
    }
    return eid


def _entry_set(eid, **kw):
    with ENTRIES_LOCK:
        e = ENTRIES.get(eid)
        if e:
            e.update(kw)


def _has_preview(site_dir):
    try:
        return bool(site_dir) and (Path(site_dir) / "_preview.png").is_file()
    except Exception:
        return False


def _screenshot_local(html_path, out_path):
    """Render a cleaned local index.html in a headless browser and save a thumbnail, so the
    card preview reflects the CLEAN result (not just the archived page grabbed at download)."""
    image_providers._ensure_playwright()
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 900})
            page.goto(Path(html_path).resolve().as_uri(), wait_until="networkidle", timeout=40000)
            page.wait_for_timeout(800)
            page.screenshot(path=str(out_path))
        finally:
            browser.close()


def _humanize_progress_line(line):
    m = _re.search(r"\[image-recovery\]\s+(\d+)/(\d+)", line)
    if m:
        return f"картинка {m.group(1)} из {m.group(2)}"
    if "[image-recovery]" in line and "missing image" in line:
        m = _re.search(r"(\d+)\s+missing image", line)
        return f"нашёл {m.group(1)} недостающих картинок" if m else None
    if "[css-recovery] fetching" in line:
        m = _re.search(r"fetching\s+(\S+)", line)
        name = m.group(1).rstrip(".").rsplit("/", 1)[-1] if m else ""
        return f"восстанавливаю {name}" if name else "восстанавливаю ассет из CSS"
    if "[corrupted-asset]" in line and "recovered" not in line and "FAILED" not in line:
        return "чиню повреждённую картинку"
    return None


class _ProgressStream(io.StringIO):
    """Tees the cleanup's stdout into the report buffer AND pulls a human-readable
    'current file' detail out of the core's own [image-recovery]/[css-recovery]/... lines."""

    def write(self, s):
        n = super().write(s)
        for part in s.splitlines():
            d = _humanize_progress_line(part)
            if d:
                with CLEANUP_LOCK:
                    CLEANUP["detail"] = d
        return n

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
    return render_template("landing.html")


@app.route("/studio")
def studio():
    return render_template("index.html", default_fonts=clean_wayback_site.DEFAULT_GOOGLE_FONTS)


def _download_entry(eid, dest_path):
    """Background worker: download one archived page into its own card entry. Capped by
    _DL_SEM so at most 2 browsers run at once even if many URLs were pasted."""
    with _DL_SEM:
        with ENTRIES_LOCK:
            if eid not in ENTRIES:  # card was removed before its turn came up
                return
        _entry_set(eid, dl_phase="открываю браузер")
        url = ENTRIES[eid]["url"]

        def cb(pct, msg):
            _entry_set(eid, dl_pct=int(pct), dl_phase=msg)

        try:
            res = wayback_download.download_wayback_site(url, dest_path, progress=cb)
            _entry_set(eid, dl_done=True, dl_pct=100, dl_phase="готово",
                       site_dir=res["site_dir"], domain=res["domain"],
                       preview=_has_preview(res["site_dir"]))
        except Exception as e:  # noqa: BLE001 - report per-card, keep the others going
            _entry_set(eid, dl_done=True, dl_error=str(e), dl_phase="ошибка")


@app.route("/api/wayback/start", methods=["POST"])
def api_wayback_start():
    body = request.json or {}
    urls, seen = [], set()
    for u in (body.get("urls") or []):
        u = (u or "").strip()
        if u and u not in seen:
            seen.add(u)
            urls.append(u)
    dest = (body.get("dest") or "").strip()
    if not urls:
        return jsonify({"ok": False, "error": "вставь хотя бы одну ссылку"}), 400
    if not dest:
        return jsonify({"ok": False, "error": "укажи папку для скачивания"}), 400
    dest_path = Path(dest)
    try:
        dest_path.mkdir(parents=True, exist_ok=True)
    except Exception as e:
        return jsonify({"ok": False, "error": f"не удалось создать папку: {e}"}), 400

    _WB_DEST["path"] = str(dest_path)
    with ENTRIES_LOCK:
        ids = [_new_entry(u) for u in urls]
    for eid in ids:
        threading.Thread(target=_download_entry, args=(eid, dest_path), daemon=True).start()
    return jsonify({"ok": True, "ids": ids})


@app.route("/api/entries")
def api_entries():
    with ENTRIES_LOCK:
        entries = [dict(e) for e in ENTRIES.values()]
    return jsonify({"dest": _WB_DEST["path"], "entries": entries})


@app.route("/api/wayback/adopt", methods=["POST"])
def api_wayback_adopt():
    """Turn every site already sitting in `dest` (each <dest>/<domain>/index.html) into a card,
    so previously-downloaded sites show up again after a server restart - the folders on disk
    are the source of truth, no in-memory registry to lose. Skips folders already tracked."""
    dest = ((request.json or {}).get("dest") or "").strip()
    if not dest:
        return jsonify({"ok": False, "error": "не указана папка"}), 400
    root = Path(dest)
    if not root.is_dir():
        return jsonify({"ok": False, "error": "папки нет"}), 400
    _WB_DEST["path"] = str(root)
    added = []
    with ENTRIES_LOCK:
        tracked = {e["site_dir"] for e in ENTRIES.values() if e.get("site_dir")}
        try:
            subs = sorted(p for p in root.iterdir() if p.is_dir())
        except OSError:
            subs = []
        for sub in subs:
            if not (sub / "index.html").is_file():
                continue
            sd = str(sub)
            if sd in tracked:
                continue
            eid = _new_entry(sd)  # no original archive URL for an on-disk site - show its path
            ENTRIES[eid].update(domain=sub.name, site_dir=sd, dl_done=True, dl_pct=100,
                                dl_phase="на диске", preview=_has_preview(sd))
            added.append(eid)
    return jsonify({"ok": True, "added": added})


def _clean_entry(eid):
    """Background worker: run the full cleanup (+ menu auto-link + fresh preview) on one card's
    downloaded site. Cancellable via its _CANCEL event; each runs in its own thread so many
    cards clean in parallel."""
    with ENTRIES_LOCK:
        e = ENTRIES.get(eid)
        site_dir = Path(e["site_dir"]) if e and e.get("site_dir") else None
    if site_dir is None:
        return
    html_path = site_dir / "index.html"
    ev = _CANCEL.get(eid)

    def cb(pct, action):
        with ENTRIES_LOCK:
            cur = ENTRIES.get(eid)
            if cur:
                cur["clean_pct"] = max(cur["clean_pct"], int(pct))
                cur["clean_action"] = action

    try:
        clean_wayback_site.clean_html_file(
            html_path, None, dry_run=False, backup=True,
            progress=cb, cancelled=(ev.is_set if ev else None),
        )
        _entry_set(eid, clean_action="Привязываю ссылки меню/футера")
        try:
            site_edit.auto_link_menu(site_dir)
        except Exception:  # noqa: BLE001 - never fail cleanup over the menu step
            pass
        _entry_set(eid, clean_action="Обновляю превью")
        try:
            _screenshot_local(html_path, site_dir / "_preview.png")
        except Exception:  # noqa: BLE001 - preview is best-effort
            pass
        _entry_set(eid, clean_running=False, clean_pct=100, clean_action="Готово",
                   clean_done=True, preview=_has_preview(site_dir))
    except clean_wayback_site.CleanupCancelled:
        _entry_set(eid, clean_running=False, clean_cancelled=True, clean_action="Отменено")
    except Exception as e:  # noqa: BLE001 - surface to the card
        _entry_set(eid, clean_running=False, clean_error=str(e), clean_action="ошибка")
    finally:
        _CANCEL.pop(eid, None)


@app.route("/api/entry/clean", methods=["POST"])
def api_entry_clean():
    eid = (request.json or {}).get("id")
    with ENTRIES_LOCK:
        e = ENTRIES.get(eid)
        if not e:
            return jsonify({"ok": False, "error": "карточка не найдена"}), 404
        if not e.get("site_dir") or not e.get("dl_done") or e.get("dl_error"):
            return jsonify({"ok": False, "error": "сайт ещё не скачан"}), 400
        if e.get("clean_running"):
            return jsonify({"ok": False, "error": "очистка уже идёт"}), 409
        e.update(clean_running=True, clean_pct=0, clean_action="Запускаю…",
                 clean_done=False, clean_error=None, clean_cancelled=False)
    _CANCEL[eid] = threading.Event()
    threading.Thread(target=_clean_entry, args=(eid,), daemon=True).start()
    return jsonify({"ok": True})


@app.route("/api/entry/cancel", methods=["POST"])
def api_entry_cancel():
    eid = (request.json or {}).get("id")
    ev = _CANCEL.get(eid)
    if ev:
        ev.set()
    return jsonify({"ok": True})


@app.route("/api/entry/remove", methods=["POST"])
def api_entry_remove():
    eid = (request.json or {}).get("id")
    ev = _CANCEL.get(eid)
    if ev:
        ev.set()  # stop any running cleanup for this card
    with ENTRIES_LOCK:
        ENTRIES.pop(eid, None)
    return jsonify({"ok": True})


@app.route("/api/entry/open-folder", methods=["POST"])
def api_entry_open_folder():
    eid = (request.json or {}).get("id")
    with ENTRIES_LOCK:
        e = ENTRIES.get(eid)
    path = e.get("site_dir") if e else None
    if not path or not os.path.isdir(path):
        return jsonify({"ok": False, "error": "папки ещё нет"}), 400
    try:
        os.startfile(path)  # noqa: S606 - local tool, opens the folder in Explorer
    except Exception as e:  # noqa: BLE001
        return jsonify({"ok": False, "error": str(e)}), 400
    return jsonify({"ok": True})


@app.route("/api/entry/preview")
def api_entry_preview():
    eid = request.args.get("id")
    with ENTRIES_LOCK:
        e = ENTRIES.get(eid)
    path = e.get("site_dir") if e else None
    if not path:
        return "", 404
    p = Path(path) / "_preview.png"
    if not p.is_file():
        return "", 404
    resp = send_from_directory(p.parent, p.name)
    resp.headers["Cache-Control"] = "no-store"  # preview is overwritten after cleanup
    return resp


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
    with CLEANUP_LOCK:
        if CLEANUP["running"]:
            return jsonify({"ok": False, "error": "очистка уже идёт"}), 409

    body = request.json or {}
    favicon = (body.get("favicon") or "").strip() or None
    if favicon and not Path(favicon).is_file():
        return jsonify({"ok": False, "error": f"favicon file not found: {favicon}"}), 400

    html_path = STATE["html_path"]
    site_dir = STATE["site_dir"]
    with CLEANUP_LOCK:
        CLEANUP.update(running=True, percent=0, action="Запускаю…", detail="", report=None, error=None)

    def _progress(percent, action):
        with CLEANUP_LOCK:
            # monotonic - never let a phase report a lower percent than already shown
            CLEANUP["percent"] = max(CLEANUP["percent"], int(percent))
            if action != CLEANUP["action"]:
                CLEANUP["detail"] = ""  # new phase - drop the previous phase's file detail
            CLEANUP["action"] = action

    def _run():
        buf = _ProgressStream()
        try:
            with contextlib.redirect_stdout(buf):
                clean_wayback_site.clean_html_file(
                    html_path,
                    (body.get("fonts") or "").strip() or None,
                    dry_run=bool(body.get("dry_run")),
                    backup=not body.get("no_backup"),
                    keep_contact_info=bool(body.get("keep_contact_info")),
                    favicon=favicon,
                    recover_images=not body.get("no_image_recovery"),
                    domain_override=(body.get("domain") or "").strip() or None,
                    progress=_progress,
                )
                # Last step of cleanup: wire up the header/footer/mobile nav across the site
                # (attach broken links to sections, add anchors, cap the header at 4, ...).
                if not body.get("dry_run") and site_dir is not None:
                    with CLEANUP_LOCK:
                        CLEANUP["action"] = "Привязываю ссылки меню/футера"
                        CLEANUP["detail"] = ""
                    try:
                        nav = site_edit.auto_link_menu(site_dir)
                        print(
                            f"\n[menu] привязано {len(nav.get('linked', []))}, удалено лишних "
                            f"{len(nav.get('removed', []))}, зеркалировано {len(nav.get('relabeled', []))}, "
                            f"не привязано {len(nav.get('unresolved', []))}"
                        )
                    except Exception as e:  # noqa: BLE001 - never fail cleanup over the menu step
                        print(f"[menu] auto-link skipped: {e}")
            with CLEANUP_LOCK:
                CLEANUP.update(running=False, percent=100, action="Готово", report=buf.getvalue())
        except Exception as e:  # noqa: BLE001 - surface any failure to the UI
            with CLEANUP_LOCK:
                CLEANUP.update(running=False, error=f"{e}\n\n{buf.getvalue()}")

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"ok": True, "started": True})


@app.route("/api/cleanup-progress")
def api_cleanup_progress():
    with CLEANUP_LOCK:
        return jsonify(
            {
                "running": CLEANUP["running"],
                "percent": CLEANUP["percent"],
                "action": CLEANUP["action"],
                "detail": CLEANUP["detail"],
                "report": CLEANUP["report"],
                "error": CLEANUP["error"],
            }
        )


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

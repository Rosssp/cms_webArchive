# -*- coding: utf-8 -*-
"""
semantics.py
============

Optional Haiku-powered semantic enrichment for the cleaner. Three capabilities, each a
single cheap Haiku call, all BEST-EFFORT: on any error (no key, network, bad JSON) they
return None / a safe passthrough so the deterministic pipeline is never broken by the LLM.

  1. generate_meta(text, ...)      -> {"title", "description"} for <head>
  2. classify_sections(items)      -> per-section canonical kind (hero/about/services/...)
  3. semantic_tags(blocks)         -> suggested HTML5 tag per top-level block
                                      (header/nav/main/section/article/aside/footer/None)

Enabled only when ANTHROPIC_API_KEY is set (read from process env or the project .env) and
SITE_STUDIO_SEMANTICS is not turned off. Model = ANTHROPIC_MODEL (default: cheapest Haiku).
Cheapest model on purpose - this runs per page and we only need short, structured answers.
"""

import json
import os
import re
from functools import lru_cache
from pathlib import Path

DEFAULT_MODEL = "claude-haiku-4-5-20251001"

# Canonical section kinds classify_sections maps to (a closed vocabulary keeps the anchors/
# labels predictable and lets the caller fall back cleanly on anything off-list).
SECTION_KINDS = (
    "hero", "about", "services", "features", "portfolio", "pricing",
    "team", "testimonials", "blog", "faq", "gallery", "process",
    "stats", "cta", "contact", "other",
)

# HTML5 landmarks semantic_tags is allowed to suggest. "keep" = leave the tag as-is.
SEMANTIC_TAGS = ("header", "nav", "main", "section", "article", "aside", "footer", "keep")


def _load_dotenv():
    """Load KEY=VALUE lines from the project .env into os.environ (without overriding anything
    already set in the real environment). No hard dependency on python-dotenv."""
    root = Path(__file__).resolve().parent.parent
    env = root / ".env"
    if not env.is_file():
        return
    try:
        for line in env.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            k, v = k.strip(), v.strip().strip('"').strip("'")
            if k and k not in os.environ:
                os.environ[k] = v
    except OSError:
        pass


@lru_cache(maxsize=1)
def _config():
    _load_dotenv()
    key = (os.environ.get("ANTHROPIC_API_KEY") or "").strip()
    model = (os.environ.get("ANTHROPIC_MODEL") or "").strip() or DEFAULT_MODEL
    off = (os.environ.get("SITE_STUDIO_SEMANTICS") or "").strip().lower() in ("0", "false", "no", "off")
    return {"key": key, "model": model, "enabled": bool(key) and not off}


def available():
    """True if the semantic pass can run (key present and not explicitly disabled)."""
    return _config()["enabled"]


@lru_cache(maxsize=1)
def _client():
    cfg = _config()
    if not cfg["enabled"]:
        return None
    try:
        import anthropic
    except ImportError:
        return None
    try:
        return anthropic.Anthropic(api_key=cfg["key"])
    except Exception:  # noqa: BLE001 - never let client init crash the cleaner
        return None


_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.I)


def _ask_json(system, user, max_tokens=512):
    """One Haiku call expecting a JSON reply. Returns the parsed object, or None on any failure."""
    client = _client()
    if client is None:
        return None
    try:
        msg = client.messages.create(
            model=_config()["model"],
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(getattr(b, "text", "") for b in msg.content).strip()
        text = _FENCE_RE.sub("", text).strip()
        # tolerate a leading/trailing prose sentence around the JSON
        m = re.search(r"[\[{].*[\]}]", text, re.S)
        return json.loads(m.group(0) if m else text)
    except Exception:  # noqa: BLE001 - bad JSON / network / rate limit -> caller falls back
        return None


def _clip(s, n):
    s = re.sub(r"\s+", " ", (s or "")).strip()
    return s[:n].strip()


def _clip_words(s, n):
    """Like _clip but never cut mid-word: trim back to the last whole word that fits in n chars
    (so a slightly-too-long meta description ends cleanly, not on 'digi')."""
    s = re.sub(r"\s+", " ", (s or "")).strip()
    if len(s) <= n:
        return s
    cut = s[:n]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp > n * 0.6 else cut).rstrip(" ,;:-").strip()


# --------------------------------------------------------------------------- #
# 1) Title + meta description
# --------------------------------------------------------------------------- #

def generate_meta(page_text, existing_title=None, domain=None):
    """SEO <title> + <meta description> from a page's visible text. Returns
    {"title", "description"} (both plain text, page's own language) or None."""
    body = _clip(page_text, 6000)
    if not available() or len(body) < 40:
        return None
    system = (
        "You are an SEO editor. From the visible text of ONE web page, write meta tags in the "
        "SAME LANGUAGE as that text. Reply with ONLY a JSON object: "
        '{"title": string, "description": string}. '
        "title: 40-60 characters, the page's real topic, no trailing brand/site-name spam. "
        "description: 120-160 characters, a natural sentence summarising the page, no quotes. "
        "No markdown, no extra keys, no commentary."
    )
    hint = f"Site domain: {domain}\n" if domain else ""
    if existing_title:
        hint += f"Current title (may be junk): {_clip(existing_title, 120)}\n"
    data = _ask_json(system, f"{hint}\nPAGE TEXT:\n{body}", max_tokens=300)
    if not isinstance(data, dict):
        return None
    title = _clip_words(data.get("title"), 65)
    desc = _clip_words(data.get("description"), 165)
    if not title or not desc:
        return None
    return {"title": title, "description": desc}


# --------------------------------------------------------------------------- #
# 2) Section classification (for menu anchors + labels)
# --------------------------------------------------------------------------- #

def classify_sections(items):
    """Given ordered sections (each {"text": heading/label, "sample": short body sample}),
    return a list of the same length with a canonical kind from SECTION_KINDS for each.
    Returns None on failure so the caller keeps its deterministic labels."""
    items = list(items or [])
    if not available() or not items:
        return None
    listing = "\n".join(
        f'{i}. heading="{_clip(it.get("text"), 80)}" sample="{_clip(it.get("sample"), 160)}"'
        for i, it in enumerate(items)
    )
    system = (
        "Classify each web-page section into ONE kind from this exact list: "
        + ", ".join(SECTION_KINDS) + ". "
        'Reply with ONLY a JSON array of objects {"i": index, "kind": kind, "label": short}. '
        "label = a 1-2 word menu label in the section's own language (e.g. About, Services, Контакты). "
        "Use \"other\" only when nothing fits. No commentary."
    )
    data = _ask_json(system, f"SECTIONS:\n{listing}", max_tokens=600)
    if not isinstance(data, list):
        return None
    out = [{"kind": "other", "label": ""} for _ in items]
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(out):
            kind = str(row.get("kind", "other")).strip().lower()
            out[i] = {
                "kind": kind if kind in SECTION_KINDS else "other",
                "label": _clip(row.get("label"), 24),
            }
    return out


# --------------------------------------------------------------------------- #
# 3) Tag semantics (bad-markup div soup -> HTML5 landmarks)
# --------------------------------------------------------------------------- #

def semantic_tags(blocks):
    """Given top-level block signatures (each {"tag","cls","id","heading","links","pos"}),
    suggest an HTML5 landmark tag for each from SEMANTIC_TAGS ("keep" = leave unchanged).
    Returns a list of the same length, or None on failure."""
    blocks = list(blocks or [])
    if not available() or not blocks:
        return None
    listing = "\n".join(
        f'{i}. <{b.get("tag","div")} class="{_clip(b.get("cls"), 60)}" id="{_clip(b.get("id"), 40)}"> '
        f'heading="{_clip(b.get("heading"), 60)}" links={b.get("links", 0)} pos={b.get("pos","")}'
        for i, b in enumerate(blocks)
    )
    system = (
        "You improve HTML5 semantics. Each item is a top-level block of a page currently using "
        "non-semantic tags (mostly <div>). Pick the correct landmark tag for each from this exact "
        "list: " + ", ".join(SEMANTIC_TAGS) + ". "
        '"keep" means leave it as-is. The page header (top nav bar) is ALREADY handled - do NOT '
        "output header. Rules: a hero / intro / banner / welcome / slider band = section (NEVER "
        "header or footer, even if it is first or has buttons); a normal content band = section; a "
        "self-contained article/post = article; a sidebar = aside; ONLY the very last bottom bar "
        '(copyright/site links) = footer. Reply with ONLY a JSON array {"i": index, "tag": tag}. '
        "No commentary."
    )
    data = _ask_json(system, f"BLOCKS:\n{listing}", max_tokens=500)
    if not isinstance(data, list):
        return None
    out = ["keep"] * len(blocks)
    for row in data:
        if not isinstance(row, dict):
            continue
        try:
            i = int(row.get("i"))
        except (TypeError, ValueError):
            continue
        if 0 <= i < len(out):
            tag = str(row.get("tag", "keep")).strip().lower()
            out[i] = tag if tag in SEMANTIC_TAGS else "keep"
    return out

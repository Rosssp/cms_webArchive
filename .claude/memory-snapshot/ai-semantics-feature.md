---
name: ai-semantics-feature
description: "The optional Haiku \"semantics\" pass in the wayback cleaner — model, scope, on/off"
metadata: 
  node_type: memory
  type: project
  originSessionId: d2977795-0bd5-4502-a65e-166b52726951
---

The cleaner has an optional AI "semantics" pass powered by Anthropic, added 2026-07-12. Deliberately uses the **cheapest** model, Haiku 4.5 (`claude-haiku-4-5-20251001`), because it runs per page and only needs short structured answers.

Scope — three capabilities, all best-effort (any failure → deterministic fallback, never breaks cleanup):
1. **Tag semantics** — promote top-level `<div>` soup to HTML5 landmarks (header/nav/main/section/footer).
2. **Section classification** — canonical kind + clean menu label per section (drives anchors like `#services` and nav labels).
3. **Meta** — generate `<title>` + `<meta description>` from page content.

Where it lives: `site_studio/semantics.py` (the module). Wired into `clean_wayback_site.clean_html_file` (meta + tags, skipped in dry-run) and `site_edit.auto_link_menu` (classification).

On/off: **auto-enabled whenever `ANTHROPIC_API_KEY` is present** (loaded from project `.env`, which is gitignored). Kill-switch: env `SITE_STUDIO_SEMANTICS=0`. Model override: env `ANTHROPIC_MODEL`.

NOTE: the key the user first pasted (2026-07-12) was exposed in chat — it should be rotated in console.anthropic.com.

---
name: fix-script-not-file-restart-server
description: Always fix the root-cause SCRIPT (not the one site file); studio server needs restart to pick up changes
metadata: 
  node_type: memory
  type: feedback
  originSessionId: d2977795-0bd5-4502-a65e-166b52726951
---

When something is wrong with a cleaned site, fix the **root cause in the SCRIPT** (`clean_wayback_site.py` / `site_studio/site_edit.py`), NOT just the individual output `index.html`. Every site is downloaded + cleaned through the same script, so a per-file patch fixes one site and leaves the bug for all the others. Patching the output file is only ever a demo of the fix, never the fix itself.

**Why:** the owner runs many sites through the script/studio pipeline; the value is that the pipeline produces correct output unattended.

**How to apply:**
1. Fix the logic in the script and verify by running the script directly on the raw site.
2. CRITICAL: the studio server (`site_studio/server.py`) runs with `debug=False` — **no auto-reload**. It loads the modules once at startup and holds them in memory. So ANY script edit has NO effect in the studio until the **server is restarted**. `_clean_entry` even wraps `auto_link_menu` in `except: pass`, so a stale/broken menu step fails silently.
3. When a fix "doesn't work through the studio", suspect a **stale server first** — compare the server process start time vs the edited file's mtime. Tell the owner to restart the server (or offer to), then re-clean.

Related: [[nav-linking-rules]], [[ai-semantics-feature]].

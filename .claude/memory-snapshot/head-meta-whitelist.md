---
name: head-meta-whitelist
description: В head из СЕО/соц-мета оставить ТОЛЬКО title, description, canonical, одну иконку; всё og/twitter/hreflang/msapplication снести
metadata:
  type: project
---

**Правило владельца (2026-07-22): в `<head>` из СЕО/соц-мета остаётся ТОЛЬКО:**
- `<title>`
- `<meta name="description">`
- `<link rel="canonical">`
- ОДНА иконка `<link rel="icon">`

**Всё остальное соц/СЕО — снести:** `<meta property=…>` (og:*, twitter:*, fb:*, article:*),
`og:site_name`, `og:image:width/height`, `twitter:card`, `twitter:description`, `og:type`,
`og:locale`, `keywords`, `robots`, `msapplication-*`, `<link rel="alternate" hreflang>`, лишние
иконки (apple-touch-icon, mask-icon, дубли).

**НЕ ТРОГАТЬ техническое** (иначе ломается страница): `<meta charset>`, `<meta name="viewport">`
(без него нет адаптива), `<meta http-equiv=…>`, `<link rel="stylesheet">`, `preconnect`/`preload`/
`dns-prefetch` (шрифты), `<style>`, `<script>`, `<base>`.

Реализация: `strip_head_to_essentials` (whitelist), вызывается после `ensure_canonical`. Прежний
`strip_url_meta_tags` сносил только меты с URL-контентом (og:image, og:url) — og:site_name,
twitter:card, og:image:width оставались. Проверено на bambooship: осталось charset/viewport/
description/canonical/icon/26 stylesheet; снесено 11 соц-мет + 2 hreflang + apple-touch-icon.

Связано: [[preserve-css-identity]] (не сломать stylesheet/viewport).

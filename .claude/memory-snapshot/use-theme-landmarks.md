---
name: use-theme-landmarks
description: Если у страницы уже есть header/main/footer темы — поднять их на уровень body, не хоронить в обёртке; медиа-элемент не пустая слайдер-оболочка
metadata:
  type: project
---

**WordPress/Astra и подобные темы УЖЕ дают правильные лендмарки** — `<header id=masthead>`,
`<main id=main>`, `<footer id=colophon>` внутри обёртки `div#page.hfeed.site`. Чистильщик раньше
переименовывал обёртку в `<section>` и оставлял лендмарки ВЛОЖЕННЫМИ, так что `<header>/<main>/
<footer>` были не на верхнем уровне body. Владелец руками выносил их наверх (bambooship) — это и
есть правильно.

**Фикс (2026-07-22):** `hoist_theme_landmarks` — когда все три лендмарка лежат под одной обёрткой-
ребёнком body, поднять header перед обёрткой, footer после, а саму обёртку сделать `<main>`
(вложенный `<main>` демотировать в div — один main на страницу). `relocate_hidden_svg_defs` уносит
скрытые svg-symbol-листы (`visibility:hidden;position:absolute` с `<defs>`) из НАЧАЛА body в конец
(они стояли перед header). Проверено: первый ребёнок body = header, регресс 6 сайтов без изменений.

**Грабля fix_static_sliders (та же сессия):** класс слайдер-бокса (`_SLIDER_BOX_RE`) матчил
`swiper-slide-image` — а это класс на САМИХ `<img>` (логотипы партнёров Elementor). У `<img>` нет
вложенных `<img>`, поэтому «пустая оболочка → удалить» выкашивала саму картинку. Фикс: медиа-элемент
(`img/video/svg/picture/canvas/iframe`) — это КОНТЕНТ, в пустой-схлоп его не пускать.

Связано: [[page-structure-rule]], [[preserve-css-identity]].

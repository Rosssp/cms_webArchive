---
name: lazyload-promote-src
description: Ленивая загрузка — реальный URL в data-lazy-src/data-srcset, а в src svg-пустышка; promote_src должен их поднимать
metadata:
  type: project
---

WP-плагины ленивой загрузки (WP Rocket, a3 Lazy Load, Lazy Load, lazyload.min.js…) кладут в `src`
ПУСТЫШКУ (инлайновый `data:image/svg+xml` спейсер или 1x1-gif), а настоящий URL — в data-атрибут
(`data-lazy-src`, `data-lazy-srcset`, `data-src`, `data-original`…). Файл при этом ЧАСТО уже скачан
локально (`index_files/…jpg`).

**Баг (2026-07-22, saramonicvietnam):** «не скачались фото» — 55 из 130 картинок галереи. На деле
файлы были на диске, но `promote_src` смотрел ТОЛЬКО `data-src` и только когда `src` пустой. А тут
`src`=svg-пустышка (не пустой) и реальный путь в `data-lazy-src` → промоут не срабатывал →
страница показывала пустые плейсхолдеры. Механизм скачивания был НИ ПРИ ЧЁМ (`recover_asset_bytes`
тянул картинку и с ts, и без — 8850 байт).

**Фикс:** `promote_src` (a) заменяет `src`, если он ОТСУТСТВУЕТ ИЛИ это заглушка
(`data:image/svg+xml`/`gif`), реальным из списка `_LAZY_SRC_ATTRS`; (b) поднимает `data-lazy-srcset`
/`data-srcset` в `srcset`. Проверено: 55 пустышек → 0, все 124 img локальны, 0 без файла.

**Урок (как с href):** «не скачалось/нет стилей» ≠ баг скачивания. Сначала посмотреть РАЗМЕТКУ на
живом (src vs data-*), а не чинить сеть. Связано: [[preserve-css-identity]], [[trace-dont-read]],
[[transient-vs-definite]].

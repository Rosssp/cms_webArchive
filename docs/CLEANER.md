# clean_wayback_site.py — как работает очистка (архитектура + каталог сценариев)

Документ-справочник по чистильщику вебархива. Цель — чтобы можно было дорабатывать под новые кривые
архивы, **ничего не ломая**. Здесь: полный пайплайн очистки по шагам, каталог сценариев (что архив
ломает и как это чинится/где дыра), и правила безопасной доработки.

> Принцип №1: **чиним сам скрипт под сценарий, а не конкретный скачанный сайт.** Скачанные сайты —
> только временные копии для проверки. Правка обязана обобщаться на любой архив.
>
> Принцип №2: **рендерим, а не верим отчёту.** Баг «нет стилей» (`<base href>`) был невидим в DOM и
> отчёте — вскрылся только скриншотом. После правок, влияющих на вид, — обязательно рендер-скриншот.

---

## 1. Из чего состоит проект

- **`clean_wayback_site.py`** — детерминированный чистильщик (одна страница = один прогон
  `clean_html_file`). Всё ниже — про него.
- **`site_studio/`** — Flask-панель: `wayback_download.py` (скачивание+скролл+снятие тулбара архива),
  `server.py` (лендинг + студия), `site_edit.py` (`auto_link_menu` и ручные правки), `header_gen.py`
  (генерация хеддера, 5 шаблонов), `semantics.py` (опциональный ИИ-слой на Haiku).

## 2. Пайплайн `clean_html_file` (по фазам `_p(pct, msg)`)

Порядок важен — многие шаги зависят от предыдущих. Проценты — то, что видит UI.

| % | Что делает | Ключевые функции |
|---|---|---|
| 4 | Парсинг. Предочистка мусорного тега между `<html>` и `<head>`, нормализация charset. | `PRECLEAN_STRAY_TAG_RE`, `normalize_charset_meta` |
| 9 | Восстановление недостающих локальных картинок из архива. | `recover_missing_local_images` |
| — | Фикс редиректа/noindex. | `check_and_fix_redirect`, `check_and_fix_noindex` |
| 28 | **Снятие обёрток архива:** HTML-комменты, атрибуты `<html>`, тулбар (`#wm-ipp-base` и пр.), **`<base href>`** (иначе все относительные ссылки резолвятся с мёртвого домена → голый HTML), unwayback всех атрибутов, чистка скриптов, чистка `<link rel=stylesheet>`, снятие CMS-меты. | `strip_wayback_comment_and_html_attrs`, `strip_wayback_toolbar`, **`strip_base_href`**, `unwayback_all_attrs`, `clean_scripts`, `clean_stylesheet_links`, `strip_cms_meta_links` |
| 44 | Следы владельца, `<head>`-стили, промоут `data-src`→`src`, lazy-loading, чистка data/event-атрибутов, **ИИ-семантика тегов**, **выравнивание уровней заголовков**. | `strip_owner_traces`, `clean_head_styles`, `promote_src`, `add_lazy_loading`, `apply_semantic_tags`, `normalize_heading_levels` |
| 56 | Локальный шрифт (детект или рандом-пресет, скачивается локально), object-fit для картинок. | `inject_google_fonts`, `resolve_font_input` |
| 60 | **Иконки:** детект версии FA (4/5/6), self-host с cdnjs, экранирование `*{font-family !important}`-ресетов; чистка `<a>`; локализация медиа (в т.ч. same-domain-absolute → локально, мёртвое — отцепить); локализация сторонних картинок; контакты/e-mail; iframes; дроп скриптов с отсутствующими jQuery-плагинами; вынос инлайн-JS; self-host jQuery; флаги контента; детект лого. | `ensure_icon_fonts`, `clean_links_a`, `localize_media_refs`, `localize_external_media`, `normalize_email_domains`, `clean_iframes`, `drop_scripts_with_missing_plugins`, `externalize_inline_scripts`, `ensure_jquery` |
| 64 | (Если задан `domain_override`) авто-лого по домену + ребренд текста. | `apply_auto_logo`, `apply_brand_text` |
| 70 | Favicon (генерит монограмму если нет), SEO-файлы (robots/sitemap/.htaccess, перезапись), canonical. | `ensure_favicon`, `ensure_local_seo_files`, `ensure_canonical` |
| 82 | Запись очищенного `index.html` (+ `.bak`). | — |
| 88 | **Восстановление ассетов из архива** (медленное, сеть): unwayback всех локальных текстовых файлов + recovery CSS `url()`/@font-face; замена «битых» локальных картинок (HTML-вместо-картинки) на реальные. | `clean_local_linked_files`, `recover_corrupted_local_assets`, `_recover_css_asset`, `recover_asset_bytes` |
| 96 | Удаление неиспользуемых файлов, перенос сирот в `_wayback_removed`/`_unused_removed`. | `remove_unused_local_assets`, `move_orphaned_wayback_assets` |
| — | **Последний шаг (в server/CLI):** привязка меню хеддера/футтера, генерация хеддера если нет, зеркалирование мобильного меню. | `site_edit.auto_link_menu` → `header_gen.build_header` |

### Сеть к archive.org (важно для скорости)
Все запросы к архиву идут через `recover_asset_bytes` → `_fetch_url_bytes`, сериализованы
`_ARCHIVE_FETCH_LOCK` (Semaphore=1, чтобы 2 параллельные очистки не вызвали троттлинг). CDN-шрифты
(Google Fonts и пр.) НЕ тянутся из архива (`_FONT_CDN_HOSTS`). Есть circuit breaker `_REC_TL` (после
N подряд промахов перестаёт долбить CDX) + негативный кэш. Отмена — thread-local `_set_cancel_check`.
**Узкое место:** один большой сайт с десятками мёртвых/живых ассетов делает их строго по одному →
минуты. Решение (в работе) — ограниченный параллелизм (семафор ~4 + распараллелить циклы).

## 3. Каталог сценариев архива (что ломает страницу и статус в скрипте)

Легенда: ✅ чинится · ⚠️ частично/слабо · ❌ дыра · 🤖 нужен/поможет ИИ-агент.

### A. Рендер/стили (страница выглядит сломанной)
1. ✅ **`<base href="https://origin/">`** → все относительные css/js/img резолвятся с мёртвого домена
   → голый HTML. `strip_base_href`. *Была причина «нет стилей» на любом домене.*
2. ✅ **Корне-относительные wayback-ссылки на ассеты не переписывались** → стили не грузились, сырой
    скачанный сайт уже голый (ariyalur: 5 из 6 CSS). Файл БЫЛ скачан, но `<link href>` остался
    `/web/<ts>cs_/https://site/css/x.css` (без хоста), а `url_to_local` знал только абсолютную/
    распакованную форму. Починено (`wayback_download.py`): + корне-относительная форма в ключи И
    **fallback по имени файла** — любой оставшийся wayback-реф → `index_files/<файл>`, если он
    сохранён. Проверено рендером: ariyalur из голого HTML → полностью стилизован. Бьёт по классу.
3. ⚠️ **CSS-файл на деле — HTML** (вейбэк отдал error-page под именем `.css`, напр. `customize.css`).
   Браузер не парсит → часть стилей теряется. *Детект «CSS это HTML» → recovery или снять `<link>`.*
4. ⚠️ CSS `@import url()` на мёртвый/архивный таргет.
5. ❌ **Стили грузятся через JS** (динамический сайт) — скрипт вырезан → стилей нет. Редко у PBN; 🤖.
6. ⚠️ Варианты линков стилей: `rel="preload" as="style"`, `media=...`, `<link>` без явного `rel` —
   проверить, что `clean_stylesheet_links` их видит.
7. ✅ Инлайн `<style>` с вейбэк-`url()` → вынос в `index-custom.css`.
8. ✅ Шрифты: версия FA, Google Fonts, self-host.
8a. ✅ **ПРАВИЛО ШРИФТА (жёсткое): сохранить тот шрифт, который был в вебархиве; если сайт вообще не
    объявляет шрифт — Arial.** Никаких «красивых» подстановок. Три ветки в `inject_google_fonts`:
    (1) шрифт сайта опознан и есть на Google Fonts → self-host + `*`-оверрайд;
    (2) шрифт есть, но не с Google (`Alvi Nastaleeq`, `bariol`, `UTMSwiss`) → **не трогаем CSS вообще**,
        сайт рендерит свой самохостный `@font-face`;
    (3) шрифт не объявлен нигде → `Arial, Helvetica, sans-serif`.
    ⚠️ **Грабля, которая реально сработала:** был откат на пресет `("Jost","Montserrat")`, если
    self-host не удался. Одна флапнувшая загрузка шрифта — и сайт молча ПЕРЕБРЕНДИРОВАН
    (danvanhaiphong потерял Poppins → Jost, `* {font-family: Jost !important}`). Пресетный путь
    удалён целиком (`PRESET_FONTS`, `random_preset_font_param`), `font_param_from_name("")` теперь
    возвращает `""`, а не дефолт. Не удалось самохостить → НЕ трогаем ничего.
    ⚠️ **Google Fonts API регистрозависим**: `poppins` → HTTP 400, `Poppins` → OK. Каноническое имя
    берётся из `https://fonts.google.com/metadata/fonts` (XSSI-префикс `)]}'` срезать).
    ⚠️ **Проверять только СВЕЖЕЙ очисткой из сырого `.bak`.** Замер по уже очищенной папке показывает
    шрифт ПРОШЛОГО прогона и «подтверждает» что угодно — так у меня один раз прошла ложная проверка.
9. ✅ Иконочный шрифт убит `*{font-family !important}`-ресетом → экранирование.
10. ⚠️ `<noscript>`-фолбэк показывается, т.к. JS вырезан.
11. ❌ SPA (React/Vue) — пустой `<div id=root>` без JS → белая страница. 🤖/вне scope PBN.

### A1. Слайдеры и скролл-анимации (контент есть, но не виден — JS вырезан)

Общий принцип: библиотека вырезана, значит слайдер обязан выглядеть как **статичный первый слайд**,
и никогда — как пустая полоса. Ничего не докачиваем и не переписываем разметку (у Owl/Slick она
многослойная — перестройка ломает вёрстку). Всё в `fix_static_sliders` + `unhide_scroll_reveal`.

- ✅ **Скролл-ревил прячет контент навсегда.** WOW.js/AOS/ScrollReveal ставят `visibility:hidden` /
  `opacity:0` и снимают их из JS. Без JS половина страницы невидима, хотя она ЕСТЬ в DOM.
  `unhide_scroll_reveal` снимает классы/атрибуты/CSS-правила скрытия.
- ✅ **Карусель без `.active` рендерится ПУСТОЙ.** CSS показывает только активный слайд.
  Bootstrap 4/5 → `.carousel-item`, **Bootstrap 3 → просто `.item`** (частый случай в архивах!).
  Долго обрабатывался только современный класс, все BS3-карусели молча пролетали мимо.
  Активируем первый слайд + первый индикатор. `.item` вне карусели (списки, дропдауны) не трогаем.
- ✅ **Трек заморожен посреди прокрутки.** Библиотека успела записать
  `transform: translate3d(-1234px,0,0)` на `.owl-stage`/`.swiper-wrapper`/`.slick-track` — слайд 1
  уехал за пределы `overflow:hidden`, видна пустота. Снимаем инлайновый transform/transition.
- ✅ **Клоны Owl/Slick дублируют контент.** `.owl-item.cloned` — рантайм-трюк для бесконечной
  прокрутки; без JS это просто ВИДИМЫЕ дубликаты (один отзыв показан дважды). Удаляем, но только
  пока остаётся хоть один не-клон.
- ⚠️ **Слайды существуют ТОЛЬКО в JS/JSON — восстановить нельзя.** Smart Slider 3 (`n2-ss-*`),
  RevSlider, Elementor slideshow строят слайды из конфига. **Проверено на живом архиве
  bambooship.vn: там `n2_slide_els=12`, но `n2_imgs=0` — картинок нет в САМОМ архиве**, и наша
  копия снимает ровно столько же (паритет 12/12). То есть «дожидаться JS-виджета при скачивании»
  тут не помогает — данных не существует. Остаётся схлопнуть пустую оболочку, чтобы не было дыры.
  Пустоту определяем по СОДЕРЖИМОМУ (нет текста/медиа/фонов), причём текст внутри `<style>`/
  `<script>` не считается — Smart Slider кладёт свой стилевой блок ВНУТРЬ слайдера, из-за чего
  «есть ли текст?» всегда было true и оболочка выживала.

### A0. ДВА СЛОЯ КОНТРОЛЯ (главная страховка от новых багов)

История проекта: 7 багов подряд, каждый — «на выходе молча потерялось то, что было на входе», и ни
один не был виден в пофазовом отчёте «ok». Поэтому контроль строится не на «написать эвристику
получше», а на двух слоях.

**Слой 1 — сверка входа с выходом** (`audit_against_original`, детерминированный, БЕЗ ключа).
Сравнивает очищенную страницу с исходным архивом и пишет потери ПЕРВОЙ строкой отчёта:
нет `<main>`; меню сжалось (8 пунктов → 1); пропали шрифты сайта; стало меньше картинок,
заголовков или текста. Ловит 6 из 7 исторических багов, включая оба регресса, внесённых при
починке других багов. Тонкости, на которых он сам ошибался и которые уже учтены:
- `font-family` — это СТЕК; сравнивать надо семейства по отдельности, иначе `georgia,palatino,serif`
  читается как один экзотический пропавший шрифт;
- системные семейства (`arial`, `impact`, `palatino`, `georgia`…) игнорируются — их «потеря» ничего
  не значит, а вот потеря `Inter` значит всё;
- шрифт мог законно переехать в отдельный CSS рядом — это НЕ потеря, проверять и файлы тоже;
- **наличие `header`/`footer` здесь не проверять**: хедер генерируется позже, в `auto_link_menu`.
  Финальная проверка лендмарков живёт в конце `auto_link_menu` — там разметка действительно готова.

**Слой 2 — агент** (`semantics.verify_landmarks` + `verify_landmark_roles`, только с ключом).
Отвечает на ЕДИНСТВЕННЫЙ вопрос, который эвристике не даётся: КАКОЙ блок играет роль хедера/футтера.
Правила тут принципиально бессильны — off-canvas панель, стоящая последней и заканчивающаяся
копирайтом, неотличима от футера; сайдбар с «Archives / Select month» неотличим от нава.
Жёсткое ограничение: агент может только ПЕРЕНЕСТИ роль на конкретный блок. Он НЕ может её просто
снять (danvanhaiphong так потерял футер — модель вернула null, и лендмарк исчез), и он не отвечает
за то, ЕСТЬ ли хедер: это детерминированная гарантия. Без ключа поведение не меняется вообще.

Разделение обязанностей: **детерминизм гарантирует, что лендмарки есть; агент решает, кто есть кто.**

### A0b. АГЕНТ РАЗМЕЧАЕТ БЛОКИ ДО СБОРКИ (главное изменение архитектуры)

**Было:** сборка гадала по позиции — «`<main>` это всё между хедером и футером». Каждый необычный
архив ломал догадку по-своему, и каждый фикс ломал предыдущий сайт. За одну сессию так набралось
7 багов подряд, где корень всегда был не там, где симптом.

**Стало:** `plan_layout` спрашивает модель, ЧТО каждый верхнеуровневый блок такое
(`header/hero/content/sidebar/comments/footer/ignore`), метки штампуются в `data-wb-role`, и дальше
сборка механическая: хедер сверху, `content` в `<main>`, футер снизу. Метки снимаются ПЕРЕД записью
файла (иначе уезжают в готовый HTML).

**`validate_plan` — обязателен.** Модель ошибается, и её ошибки применённые вслепую хуже, чем
отсутствие модели. Реально случившееся за один прогон: пометила хедером ПУСТОЙ `div` (взяли первый
из двух — сайт уехал с пустым хедером, меню на 40 ссылок осталось `div`); пометила футером блок со
ВСЕЙ страницей (футер съел контент, `<main>` = 0%); вернула `null` для роли — сайт ПОТЕРЯЛ футер,
который у него был. Поэтому план проверяется ЦЕЛИКОМ и при провале **выбрасывается**: тогда работает
детерминированный путь, то есть худший случай = старое поведение, а не поломка.

Правила валидации: ровно один хедер и в нём ≥2 ссылки; хедер и футер ≤40% страницы; блок с >25%
текста нельзя пометить `ignore`; контент должен остаться и держать ≥30% текста; вырожденный ответ
(всё одной ролью) отклоняется.

Прочие обязательные ограничения, каждое — из реального бага:
- `<main>` собирается ТОЛЬКО из НЕПРЕРЫВНОГО ряда блоков: вытаскивание разрозненных блоков
  физически двигает контент и меняет порядок страницы;
- корректор ролей может только ПЕРЕНЕСТИ роль на конкретный блок, но не снять её;
- корректор НЕ трогает `<main>` — его кандидаты обрезаны 4000 символов, поэтому он способен выбрать
  только мелкий блок и затирал правильно собранный `<main>` до 3%;
- старый ИИ-проход (`apply_semantic_tags`) тоже обязан проверять размер/непустоту — иначе назначает
  `<footer>` пустой распорке и `<main>` блоку в 20 символов.

### A0c. Ревью кода агентами — регулярно, не разово

Классы дефектов, которые НЕ видны на текущем наборе сайтов, глазами не ловятся. Три параллельных
ревью-агента дали 29 находок, среди них корень выкошенных меню (секции искались только среди прямых
детей `body` → на обычном `<div id="wrapper">` одна секция → меню обрезалось) и баг, который на
тест-сайтах не проявляется вовсе: сужение области поиска до `<main>`, ПРИШЕДШЕГО ИЗ АРХИВА, ставило
второй `<header>` посреди статьи и удаляло ссылки статьи как пункты меню — на любом современном
WordPress. Как запускать — скилл `cleaner-code-review`.

### A2. Проактивно закрытые классы (думаем наперёд — этого нет в 13 тест-сайтах, но придёт)
43. ✅ **CSP `<meta http-equiv="Content-Security-Policy">` блокирует ВСЕ локальные ресурсы.** Архивная
    CSP разрешает только оригинальный домен → локально браузер режет все стили/скрипты/картинки →
    пустая страница, невидимо. `strip_blocking_meta` снимает CSP (+ report-only). Снятие CSP только
    ослабляет — всегда безопасно. `<meta refresh>` уже обрабатывается (`check_and_fix_redirect`).
    Инлайн `style="url()"` фоны — уже локализуются download-переписыванием (проверено danvanhaiphong).
40. ✅ **SRI (`integrity=`) блокирует локальные копии.** Модерн-сайт с CDN-`<link>/<script integrity=sha…>`:
    после локализации хеш не сходится → браузер БЛОКИРУЕТ стиль/скрипт → невидимо «нет стилей/JS».
    `strip_sri_attrs` снимает `integrity`+`crossorigin`.
41. ✅ **Универсальный детект «сломано»** (`_audit_final_output`, последним): считает оставшиеся
    wayback-ссылки (`/web/<ts>/`) в HTML/CSS/JS + битые `<link rel=stylesheet>` на несуществующий
    файл + `<frameset>` + почти-пустой `<body>`. Ловит признак поломки для ЛЮБОГО класса — даже
    ещё не придуманного — и пишет предупреждение в отчёт, чтобы новый кривой архив САМ СЕБЯ выдал,
    а не всплыл потом как «опять пришло кривым». Это главная страховка.
42. ⚠️ **`<frameset>` (старые сайты).** Контент в отдельных фреймах — скачивается только фреймсет.
    Пока: детект + предупреждение (нет в тест-наборе; полный разбор фреймов — на будущее).

### B. Хеддер / семантика
12. ✅/⚠️ **Нав — голый `<div>`/`<center>` без класса.** `_promote_header` теперь детектит нав ПО
    СОДЕРЖИМОМУ (`_looks_like_nav_block`: 3+ текст-ссылок, без своего заголовка, ссылки доминируют
    >55%) и делает его `<header>` — детерминированно, БЕЗ ИИ-ключа (проверено: sanjhapunjab, голый
    div из 40 ссылок → `<header>`). Убран преждевременный `break` (нав часто идёт ПОСЛЕ лого-бара с
    h1). ⚠️ остаётся: нав из картинок-ссылок (без текста) и «смешанный» блок (isrdc: link/total=0.11)
    — их отдаёт на генерацию `auto_link_menu`/агенту.
13. ✅ **Сайдбар/`<aside>` НЕ принимается за хеддер.** Смешанный блок с заголовками и низкой долей
    ссылок отвергается (isrdc: левый нав-сайдбар остаётся сайдбаром, сайт рендерится верно). Хеддер =
    только доминируемый-ссылками блок сверху.
14. ⚠️ Дубль десктоп+мобайл меню виден (нет CSS, что прячет одно). Частично — зеркалирование.
15. ⚠️ Нет заголовков вообще (табличная вёрстка) → нет якорей для секций.
16. ✅ Скачущие уровни заголовков (h2→h4). `normalize_heading_levels`.
    **Правило владельца (2026-07-21):** заголовки — ОПЦИОНАЛЬНО, свич на карточке лендинга «менять
    заголовки» (по умолчанию ВКЛ). ВЫКЛ → `clean_html_file(..., normalize_headings=False)` →
    заголовки НЕ трогаются ВООБЩЕ: ни выравнивания уровней, ни промоута в `h1` (даже если h1 нет), ни
    удаления пустых `<h*>` — остаются ТОЧЬ-В-ТОЧЬ как в веб-архиве (проверено: список тегов сырьё ==
    очищено). Обе трогающие заголовки функции (`normalize_heading_levels`, `drop_empty_headings`) —
    единственные, что меняют h1-h6, — под этим флагом. Свич: карточка → `/api/entry/clean
    {normalize_headings}` → `_clean_entry` → `clean_html_file`.
17. ✅ **`<main>`/`<section>` детерминированно** (`ensure_landmarks`, без ИИ-ключа): top-level
    content-`<div>` с заголовком → `<section>` (box-model-нейтрально, классы сохранены — проверено
    рендером: gigaworks/danvanhaiphong идентичны); контент оборачивается в `<main>`, если хеддер уже
    есть на этапе очистки. ИИ-проход (`apply_semantic_tags`) уточняет роли/метки.
    ⚠️ `<header>`-тег на ГЛУБОКО-зарытый `ul`-нав (ariyalur/puratoni: нав на 4-5 div вглубь, вперемешку
    с контентом) сознательно НЕ форсируется — репарентинг рискует CSS-каскадом ради SEO-тега при
    идеальном визуале; отдаётся ИИ-проходу.

### C. Ассеты
18. ✅ Картинка не заархивирована → восстановить/отцепить/в `_wayback_removed`.
    **Висячий ЛОКАЛЬНЫЙ ref после карантина** (2026-07-21): `recover_corrupted_local_assets` уносит
    битую «wayback-HTML-вместо-картинки» в `_wayback_removed` ПОСЛЕ записи HTML, а `<img srcset>`/
    `<a href>` на неё остаются → битая иконка на странице (sanjhapunjab `justice.jpg`, которого нет в
    архиве: у `<img>` был только `srcset`, без `src`). Новый пост-проход `drop_dangling_local_media`
    — HTML-двойник `strip_dead_css_urls`: срезает `src`/`srcset` на несуществующий локальный файл,
    дропает `<img>`/`<source>` без источника и мёртвый `<a>`-враппер вокруг убранной картинки.
    Гарантия: даже если recovery промахнулся, на выходе НЕТ битых ref (либо восстановлено, либо снято).
19. ✅ Абсолютные same-domain ссылки на файлы → локально.
20. ✅ `srcset`/`<picture>`/`<source>` с вейбэк-URL.
21. ⚠️ Фоновые картинки в инлайн `style="background:url()"` — проверить покрытие.
22. ✅ SVG `use`/`xlink:href` с вейбэком (unwayback локальных файлов).
23. ✅ Lazy `data-src` → `src`.
24. ✅ Favicon нет → генерим.
25. ✅ Видео/аудио мёртвое → отцепить.

### D. Ссылки / SEO / скрипты / кодировка
26. ✅ Внутренние ссылки на страницы вне экспорта → `#`.
27. ✅ Внешние ссылки/следы владельца/аналитика (GA/GTM/FB)/CMS-boilerplate.
28. ✅ canonical/og со старым доменом; вейбэк-обёртки на href; mailto/tel.
28b. ✅ **`<head>`: canonical + lang + дедуп (владелец, 2026-07-21).** (a) `<link rel=canonical>`
    ГАРАНТИРОВАН и указывает на `https://<домен-папки>/` (`ensure_canonical`, домен = имя папки в
    приоритете над контентом). (b) `<html lang>` ВЫСТАВЛЯЕТСЯ авто (`ensure_html_lang`): валидный
    существующий — оставляем; иначе og:locale (`en_US`→`en`) → content-language meta → доминантный
    скрипт видимого текста (кириллица→ru, гурмукхи→pa, арабица→ar, деванагари→hi, CJK→zh/ja…) →
    фолбэк `en`. Заодно язык кормит перевод копирайта в футере. (c) `dedupe_head_meta` схлопывает
    дубли `<meta property/name>` (sanjhapunjab вёз og:site_name ×3, og:type ×3) — оставляем первый
    каждого ключа; повторяемые (og:image, article:tag…) не трогаем. Проверено: sanjhapunjab −6 дублей.
29. ✅ jQuery-плагин отсутствует → дроп скрипта или self-host jQuery.
30. ✅ charset; мусорный тег между html/head; мусор после `</html>`.
31. ⚠️ **Фолбэк парсера:** нет lxml → `html.parser`, другая разборка (мог быть источник «на другом
    компе нет стилей» — хотя корень был `<base>`). Держать lxml обязательным (в requirements).

### E. Скорость / надёжность
32. ✅ **Скорость: параллельное CSS-восстановление.** Фаза 88 (десятки `url()` × сериализованный CDX)
    была главным тормозом. Теперь `clean_local_linked_files` пре-восстанавливает уникальные `url()`
    КОНКУРЕНТНО (пул 4), потом серийная подстановка из мапы. Семафор `_ARCHIVE_FETCH_LOCK` поднят
    1→4 (глобальный кап). **Проверено: 2 очистки параллельно НЕ виснут** (троттлинг-хэнг не вернулся,
    т.к. кап глобальный), корректность цела (css 7/7). **И медиа-локализация (фаза 60-70)
    распараллелена так же** (пре-фетч same-domain-absolute медиа конкурентно → серийный релинк).
    Итог на самом тяжёлом: **danvanhaiphong 325с → 103с (×3.2)**, css 16/16, рендер идентичный.
33b. ✅ **Длинные имена ассетов крашили ЧИСТИЛЬЩИК** (не только загрузчик): recovery писал файл с
    100+ символьным blogspot-хешем → MAX_PATH → FileNotFoundError рушил всю очистку (puratoni в
    параллели). `_cap_asset_name` (md5-тег) + запись в try/except в `_recover_css_asset` и
    `localize_media_refs`.
33. ✅ **QA результата (структурный + РЕНДЕРНЫЙ):** `_audit_stylesheets`/`_audit_final_output` —
    структурные сигналы (см. 41). ПЛЮС **рендер-QA** (`server._screenshot_local`): после очистки
    страница рендерится в headless-браузере, эвристика ловит «визуально сломано» (высота схлопнулась
    / нет фоновых стилей = голый HTML) → бейдж «⚠ проверь рендер» на карточке. Проверено: голый HTML
    → флаг, реальные сайты → чисто (без ложных). Это визуальная страховка поверх структурной.
35. ✅ **Скачивание падало/пустело на навигации.** Снапшот редиректит (http→https, канонический ts,
    JS-редирект) во время загрузки/скролла → либо краш `page.evaluate` «Execution context destroyed»,
    либо мы хватали ПУСТУЮ (~39 байт) финальную страницу и сохраняли пустой «сайт» (6 из 13!). Флак:
    один и тот же URL то полный, то пустой. Починено (`site_studio/wayback_download.py`): все evaluate
    обёрнуты; загрузка в цикле-ретрае `goto→scroll→content` ПОКА `_has_real_content` (иначе ошибка,
    а не молчаливый пустой результат); финальный `content()` снимается ПОСЛЕ снятия тулбара.
36. ✅ **Длинные имена ассетов → Windows MAX_PATH (260) краш всей загрузки** (puratoni: blogspot-хеши
    100+ симв. + глубокая папка → `write_bytes` FileNotFoundError). Починено: `_sanitize_name`
    укорачивает >64 симв. через md5-тег (уникальность+расширение сохранены) + запись ассета в
    try/except (один битый файл не рушит прогон).
37. ✅ **Скролл опустошал image-heavy галереи** (mikatoronen: арт-галерея → навигация во время скролла
    → пустой DOM → «пустая страница»). Починено: берём ЛУЧШИЙ непустой `content()` до И после скролла
    (pre-scroll граб переживает scroll-навигацию). Итог: 13/13 сайтов качаются по отдельности.
    ⚠️ При ПАРАЛЛЕЛЬНОЙ загрузке (3+ воркера) archive.org троттлит → часть падает в «пустую». Смягчено:
    5 попыток с прогрессивным бэкоффом (1.5→6с). Студия по умолчанию 2 воркера (безопаснее). Если
    массово — качать по 1–2 за раз.
38. ✅ **Браузер не отдал ассет (CSS/JS), на который DOM ссылается** → стиль не грузится (sylhet:
    сайтовые стили не попали в `page.on(response)`). Починено (`wayback_download.py`): 2-й проход —
    любой wayback-реф на ассет, которого нет локально, дотягивается напрямую (`_fetch_one`) + его
    url() фоны. Generic для любого архива, где браузер не отдал стиль.
    ⚠️ ГРАНИЦА: если ассет НИКОГДА не был заархивирован (CDX по всем снапшотам пуст — реальный
    случай sylhet-2010: `new_style.css` отсутствует в архиве) — восстановить нечем, это ограничение
    архива, а не скрипта. Такой сайт рендерится по инлайн-`<style>` + дефолтам.
38b. ✅ **Троттл archive.org ронял ВОССТАНОВИМЫЕ картинки** (2026-07-21, sanjhapunjab). `shortcode-star.png`
    и др. ЕСТЬ в архиве (CDX 200), но recovery валил их в «gone». Две причины: (1) `_fetch_url_bytes` не
    различал транзиентный троттл (timeout/429/5xx — проходит при повторе) и определённый провал (404 —
    навсегда): делал 1 общий ретрай и МЕМОИЗИРОВАЛ таймаут как «мёртвый URL» → временный спайк = вечный
    приговор; (2) CDX-search (тяжёлый запрос, легально 15-20с) дёргался с 8с-таймаутом обычного fetch'а →
    не успевал (замер: 18с на URL с 2 снапшотами). Починка: `_is_transient_fetch_error` — транзиент = до
    3 ретраев с экспон. бэкоффом и НЕ мемоизируется; определённый = валится МГНОВЕННО (даже быстрее
    старого — 404 без ретрая) и мемоизируется; CDX получил свой `RECOVERY_CDX_TIMEOUT=25`. Проверено:
    sanjhapunjab recovery 21→30 ok даже под троттлом моего IP; аудит выхода 0 пропавших/битых/висячих.
    ⚠️ Circuit-breaker `_REC_TL` в воркер-потоках выключен (thread-local, `enabled` ставится только в
    main) — на корректность НЕ влияет (не может ошибочно убить восстановимое), поэтому оставлен как есть.
39. ✅ **Нав/хеддер зарыт в единственной обёртке** (`<center>`/`<div>`/`<table>`, старые лейауты).
    `_promote_header` теперь спускается сквозь одиночную обёртку (игнорируя script/style-сиблинги),
    чтобы дотянуться до меню внутри неё. ⚠️ если нав СМЕШАН с контентом (isrdc, sylhet: 8 кнопок в
    div с 27 ссылками) — не изолируется, отдаётся на генерацию/агента.
34. ✅ **Идемпотентность.** `clean_html_file` всегда чистит из `<file>.bak` (истинного сырья, если он
    есть) и пишет `.bak` РОВНО ОДИН РАЗ. Поэтому «Очистить снова» воспроводит тот же результат, а не
    двойную обработку (перезапись `index-custom.css` темой, дубль `<link>`). Проверено: 3 очистки
    подряд — тема и линки стабильны. *Раньше повторная очистка делала страницу белой.*

---

# ЛЕСТНИЦА ПОСТАНОВКИ ЯКОРЕЙ (владелец, 2026-07-20)

Якорь должен найти куда встать. Порядок попыток, сверху вниз — каждая следующая включается, только
если предыдущая не дала минимум 2 цели:

1. настоящие теги `<section>` (не виджеты сайдбара — `_is_widget_block` их отсекает);
2. блоки с заголовком `h1`-`h6` в контентной области;
3. **визуальные заголовки** — то, что ВЫГЛЯДИТ заголовком: `<b>`, `<strong>`, `<font size>`,
   инлайновый крупный/жирный шрифт, класс `heading1`/`title2`/`entry-title` (`_looks_like_heading`);
4. **агент** (`semantics.find_section_starts`) — читает текст и говорит, где читатель увидит начало
   новой темы. Работает на страницах, где заголовков нет вообще (старая табличная вёрстка);
5. **агент именует** якоря (`semantics.name_anchors`): `#about`, `#products`, `#contact` вместо
   `#section-3536`. Слаг — всегда латиница, даже если сайт на вьетнамском или урду, потому что он
   попадает в URL.

Без ключа работают ступени 1-3, поведение прежнее. `id` создаётся ТОЛЬКО вместе с якорем
(`_ensure_section_anchor`), он же гарантирует непустоту и уникальность.

# СТРУКТУРА СТРАНИЦЫ — ЖЁСТКОЕ ПРАВИЛО (владелец, 2026-07-20)

Ровно так, без исключений:

```
<header>  … </header>
<main>    … ВСЕ секции здесь … </main>
<footer>  … </footer>
```

**Секций снаружи `<main>` быть не должно.** Реальный дефект: `main` закрывался после первых двух
секций, а следующие две оставались его СОСЕДЯМИ:

```
header / main > section section / main закрыт / section section / footer   ← НЕПРАВИЛЬНО
```

Почему это ломает всё остальное: `_content_sections` считает контентом только то, что лежит в
контентной области, а логика меню — только секции. Секция снаружи `<main>` невидима для привязки,
её якорь никуда не ведёт, и меню теряет пункты. То есть один структурный дефект тихо превращается
в «якоря не присосались».

Проверяется контрактом (`verify_output_contract`): ровно один `header`, один `main`, один `footer`;
лендмарк внутри `main` — нарушение; `main` держит ≥25% текста. Чинится `repair_landmarks_in_main`
и добором соседних блоков в `main` при сборке.

# ЯКОРЯ, МЕНЮ, ХЕДЕР, ФУТЕР (перенесено из site_studio/NAV_LINKING.md)

Продуктовые правила владельца. Менять только по его явной просьбе, и обязательно
с датой и пометкой, какое прежнее правило отменяется.

How `site_edit.auto_link_menu` must wire up a restored single-page site's navigation. These are
**product rules** (agreed with the owner), not incidental behavior — getting them wrong has bitten
us before, so change them only on an explicit request.

### Sections
`_content_sections(soup)` returns every real content section in document order (a `<section>`, or
a heading-bearing top-level block on bad-markup exports). **The hero / first block IS a section** —
it is *not* skipped. Each section is flagged `is_hero` (the first section, plus any hero/intro/
banner/masthead/welcome/cover-classed one). The AI pass may attach a clean `kind` + `ai_label`.

### HEADER — curated, capped, hero-guaranteed
- A **curated** menu: keep at most `_HEADER_MAX_LINKS` (4) links, drop the rest.
- Each broken link (`#`, `/`, empty, `javascript:…`) → the best-matching section by word overlap
  (heading/class/id/eyebrow), else the next unused section. Short label (`About`, `How`…).
- **MUST always contain a link to the HERO (the first/top block).** Resolving broken links already
  sends the first one there; if the header had nothing broken to resolve, its first menu item is
  forced onto the hero anchor. There is always a "top" link.
- Real external / real sibling-page links are left alone.

### FOOTER — the FULL sitemap
- **Unlike the header, the footer lists ABSOLUTELY EVERY section**, one in-page anchor each.
- The footer nav block is **rebuilt** from the section list (`_rebuild_footer_sitemap`): existing
  link slots are reassigned, extras are cloned (dividers included), surplus removed — so the
  footer's own styling/separators survive. Labels come from the section name (`ai_label` → short
  heading). `target="_blank"` is stripped (in-page anchors open in place).
- Whatever the original footer links pointed at (dead `privacy-policy.pdf` etc.) is irrelevant —
  the result is always "every section, anchored + labeled". **Never leave an empty `#`.**

### FOOTER — копирайт-строка ГАРАНТИРОВАНА (владелец, 2026-07-21)
Каждая восстановленная страница ОБЯЗАНА заканчиваться копирайтом `© <год> <домен>. All rights
reserved.` (домен = имя папки, год = текущий). Три случая — `_ensure_footer_copyright` + создание
футера в `auto_link_menu`:
- **Футера нет вообще** → создаётся `<footer class="wb-footer">` в конце `<body>` (структура
  header/main/footer — гарантия), и в него пишется копирайт.
- **Футер есть, копирайта нет** (архив часто терял © вместе с виджетом, что его рисовал) → строка
  ДОБАВЛЯЕТСЯ (`<p class="site-copyright">`), на неанглоязычной странице — переводится моделью
  (`semantics.localize_copyright`, если есть ключ; иначе английский фолбэк).
- **Копирайт УЖЕ есть** → wording владельца НЕ трогаем, но ДОПОЛНЯЕМ (`_augment_existing_copyright`):
  бампим год до текущего (в диапазоне `2010-2015` — последний), и если домена в строке нет —
  дописываем ` · <домен>`. Идемпотентно: полная актуальная строка не меняется.

### Anchors & logo
- Section anchor id = existing id, else the AI `kind` (`#services`), else a ≤3-word slug from the
  heading/class. Set once, reused by header + footer + mobile.
- The LOGO link = the absolute canonical-domain home URL (`https://<domain>/`). Every other nav
  href is a relative in-page anchor — never an absolute URL.

### Mobile nav
Duplicate/burger menus mirror the header's resolved href+label onto links with matching text.

### ПРАВИЛО ПОДПИСЕЙ (владелец повторял многократно — читать ПЕРВЫМ)

Пункт меню **ПЕРЕИМЕНОВЫВАЕТСЯ под название секции**, на которую ведёт. Исходная подпись не
сохраняется — она указывала на подстраницу, которой больше нет.

- **ХЕДЕР — КОРОТКО.** Короткая форма названия секции (`About`, `Services`, `Contact`) —
  `_short_label` / `ai_label`. Панель узкая, длинные подписи её ломают.
- **ФУТЕР — ПОЛНОСТЬЮ.** Полное название секции, как в заголовке. Футер — карта сайта, там место
  есть.
- Источник названия: заголовок секции, либо `ai_label`, либо то, что ВЫГЛЯДИТ заголовком
  (`_looks_like_heading`: жирный текст, `<font size>`, класс `heading1`/`title2`).
- Это касается и **dropdown-переключателей** (`href="#"` + `data-toggle`): их выпадающие списки
  удалены вместе с JS, переключать нечего — значит это обычный мёртвый пункт, он получает якорь
  секции и её название.

### ГЛАВНОЕ ПРАВИЛО ЯКОРЕЙ (решение владельца, 2026-07-20 — ОТМЕНЯЕТ часть правил ниже)

**Навигация обязана РАБОТАТЬ. Сохранять исходные подписи пунктов — не цель.**

Если после всех попыток в меню меньше 2 рабочих внутристраничных якорей — меню **пересобирается из
секций**: текст пункта заменяется названием секции, `href` — её якорем. Это касается и CMS-меню:
защита `_is_cms_menu` означает «не удалять меню», а НЕ «оставить его нерабочим».

Почему правило поменялось: на восстановленном одностраничнике исходные пункты ведут на подстраницы,
которых больше нет. Сопоставлять там нечего в принципе. sanjhapunjab выходил с 40 пунктами, у
которых не было ни `href`, ни якоря — ни привязки, ни удаления, просто мёртвые надписи.

Реализация: `_rebuild_nav_from_sections` ПЕРЕИСПОЛЬЗУЕТ существующие теги `<a>`, а не создаёт новые,
поэтому CSS темы продолжает стилизовать панель — меняются только текст и ссылка.

**Якорь ставится ТОЛЬКО вместе с созданием `id` на целевом элементе** (`_ensure_section_anchor`).
Ссылка `#foo` без элемента с `id="foo"` на странице — это баг, проверять после каждой правки.

#### Грабли, из-за которых якоря не ставились (2026-07-20)
1. **Ссылки без `href` были невидимы** для всей привязки: более ранний проход снимает атрибут у
   мёртвых внешних ссылок, а привязка перебирала только `a[href]`. Пункт нельзя было ни привязать,
   ни удалить. Теперь обрабатываются и `<a>` без `href` — но только внутри header/nav/footer.
2. **Секциями становились виджеты сайдбара** («Search», «Featured Posts», «Archives») вместо статей.
   Виджеты исключены (`_is_widget_block`), заголовки контента добавлены как кандидаты: на
   sanjhapunjab секций стало 12 вместо 3.
3. **Сопоставление по словам не работает между языками** — урду-меню поверх английских заголовков
   не совпадёт никогда. Для этого есть `semantics.match_menu_to_sections`, но по правилу выше
   проще пересобрать меню, чем сопоставлять.

### Real CMS menus are LEFT ALONE (`_is_cms_menu`)
If the header or footer nav is a genuine CMS menu — 3+ `<li>` with a `menu-item`/`nav-item` class
(WordPress &c) — auto_link_menu does NOT touch it: no cap-to-4, no relabel-to-section, no
footer-sitemap rebuild, no generated header. That's the site's real multi-page navigation, already
curated and richly labelled; rewriting it guts it (a 7-item Punjabi menu became a single "Snakes
town" section link before this guard). A hand-rolled Bootstrap navbar (no menu-item classes) is NOT
a CMS menu, so it still gets the normal header wiring. Its dead sub-page links are just neutered to
`#` by the cleaner (labels preserved), so the menu still reads like the archive.

### Dead-link sweep (final pass)
After header/footer/mobile, a final sweep catches every link STILL pointing nowhere (`#`, `/`,
empty, `javascript:`) that those passes didn't touch — hero CTA buttons ("Get Started"/"Sign In"),
stray placeholders, icon-only social links. Rule:
- **link WITH visible text** (a CTA/button) → a section anchor: contextual word-match, else the
  first/any section. Never left dead.
- **icon-only link, no text** (social icon) → the dead `href` is **removed** entirely; the empty
  `<a>` stays (so it stops looking clickable), per owner preference.
- real dropdown toggles (`data-toggle`) that legitimately use `#` are left alone.

### Generated header (sites with NO header) — header_gen.py
When a page has no header at all (`_find_header_nav` is None, no `<header>`, AND `_has_site_menu`
is false), `auto_link_menu` GENERATES one via `site_studio/header_gen.py`.

**`_has_site_menu` guard**: never generate a header on top of a site that ALREADY has a menu, even
one `_find_header_nav` can't wire — e.g. an old WordPress `<ul id="navigation" class="dropdown
menu">` of image/text-less links. It matches nav/menu hints in the id OR class and needs 2+ links
or 2+ `.menu-item` `<li>`s. Stacking a generated header on such a menu is the "cleaner grew a
second header" bug. (Related download bug: `wayback_download._strip_wayback_dom` step 5 must NOT
remove `a[href*="archive.org/web/"]` — every archived internal link is wrapped that way, so it
would delete the whole menu and leave empty `<li>`s, which is what made the menu undetectable.)

Otherwise it builds:
- **logo + simple in-page anchor nav + CSS-only burger** (no JS, so it survives script stripping).
  No theme-toggle button.
- **Colour comes from the SITE**: the accent is pulled from the site's own CSS `--primary/accent/
  brand` custom properties, else the hero section's background, else a deterministic per-brand hue
  (so a greyscale site still gets a unique tint). That's the "uniqueness".
- **Auto light/dark**: ships both palettes, switches on `prefers-color-scheme` (+ honours
  `[data-theme]`). No button.
- **Auto logo**: a baked wordmark PNG (`_gen_logo`, cropped to content), coloured with the site
  accent. To cover what a single baked colour can't, it's baked per context: a light-accent +
  dark-accent pair swapped by `prefers-color-scheme`, or a single white one on the solid-accent
  variant (v2). Falls back to a CSS text wordmark if Pillow fails. NOTE the header CSS force-resets
  `.wbh img` (`opacity/visibility/animation/transform !important`) — restored templates often ship
  `img{opacity:0}` + a JS scroll-reveal that never fires once JS is stripped, which would otherwise
  hide the logo.
- **6 variants** (v1 Line, v2 Solid, v3 Glass, v4 Pill, v5 Minimal, v6 Centered); one is picked
  deterministically per domain (`variant_for_domain`) so a site is stable but sites vary.
- First nav item is always **Home** (→ hero); the rest are the sections (labels via AI/`_section_label`).
- Injected as the first child of `<body>` + one scoped `<style>` in `<head>`; idempotent
  (`data-wb-header` marker — a re-clean replaces the old one).

### E-mail domains (cleaner, not this file)
Separately, `clean_wayback_site.normalize_email_domains` rewrites every kept e-mail to the site's
own (folder-named, bare) domain — `info@old.com` on a `gigaworks.in` site → `info@gigaworks.in` —
so contacts match the restored domain. Runs only when contacts are kept.

# ГРАБЛИ: sanjhapunjab, «ноль h2 при 11 найденных разделах» (2026-07-20)

Симптом держался восемь заходов: карта в футере собирала 11 разделов с правильными якорями, а в
контенте не было ни одного `<h2>`, и в шапке оставался ОДИН рабочий пункт из сорока. Три числа
противоречили друг другу — это и есть признак класса «одну величину считают по-разному».

Причин оказалось три, все общие, ни одна не про этот домен.

**1. Кэш классификации заголовков по `id(soup)`.** `classify_headings` кэшировала результат в
словаре с ключом `id(soup)`. `id()` в CPython — адрес в памяти, и он ПЕРЕИСПОЛЬЗУЕТСЯ после сборки
мусора. Чистильщик разбирает документы пачкой, новый `soup` садится на адрес освобождённого,
сторожевая проверка по числу заголовков совпадает — и функция отдаёт теги ЧУЖОЙ, уже мёртвой
страницы. Вызывающий код сверяет их через `id()`, не находит ни одного совпадения в живом
документе, и НИ ОДИН заголовок не признаётся карточкой. Это же объясняет регрессы на
bambooship/ariyalur/puratoni, появившиеся ровно вместе с кэшем.
→ Кэш живёт атрибутом НА объекте документа (`_HEADING_CACHE_ATTR`), он умирает вместе с ним.
**Правило: никогда не использовать `id()` как ключ переживающего вызов словаря.**

**2. Заглушка `href="#"` считалась рабочим якорем.** Пересборка меню из секций запускалась по
условию `href.startswith("#")`. Но чистильщик сам ставит `href="#"` вместо мёртвой ссылки — а `"#"`
начинается с `"#"`. Сорок мёртвых пунктов выглядели как сорок живых якорей, порог «меню в основном
мертво» не срабатывал, и `_rebuild_nav_from_sections` не вызывалась НИ РАЗУ (подтверждено
трассировкой, а не рассуждением).
→ Единый предикат `_is_live_anchor(a, soup)`: якорь живой, только если длиннее одного символа и
цель с таким `id` реально есть в документе. **Заглушка одной стадии не должна быть признаком
успеха для другой.**

**3. Шапка, разорванная на два соседних блока.** CMS почти всегда режет её на `<div id="header">`
(логотип, баннер) и `<div id="nav">` (меню). Шапкой выбирался блок со ссылками, а полоса с
логотипом оставалась снаружи и ВЫШЕ `<header>` — то есть `<header>` переставал быть первым
элементом страницы. Пользователь видит это как «хеддер не встаёт».
→ `_absorb_header_bar()` втягивает соседние СВЕРХУ полосы шапки (названные header/masthead/topbar
либо полосы-логотипы: картинка и почти без текста), максимум три, геройский баннер не трогает.
Вызывается из ВСЕХ веток `_promote_header`, а не из одной — раньше похожая логика была только в
ветке с голым `<nav>`, и её просто не достигали.

Ещё раз про метод: причину нашла ТРАССИРОВКА (подменить функцию и посмотреть, вызывается ли она
вообще), а не чтение кода. Функция `_rebuild_nav_from_sections` выглядела правильной и была
правильной — до неё не доходило управление.

# ГРАБЛЯ: архивная обёртка на tel:/mailto: (2026-07-21)

Найдена на чистой скачке bambooship. Архив заворачивает НЕ только http-ссылки:

    href="https://web.archive.org/web/20240918095351/tel:19003007"

`WAYBACK_PREFIX_RE` требовал за обёрткой `https?://` или `//`, поэтому `tel:`/`mailto:` не
разворачивались никогда — на ЛЮБОМ архивном сайте телефон и почта в шапке вели на archive.org
вместо звонка и письма. Симптом мягкий (страница выглядит целой), поэтому и жил долго.

→ В lookahead добавлен явный список схем: tel|mailto|sms|callto|whatsapp|viber|skype|facetime|
geo|bitcoin. Именно СПИСОК, а не «любое слово с двоеточием»: последнее съело бы обычные пути вроде
`/files/report:final.pdf`. Отрицательные тесты на такие пути обязательны и прогнаны.

# ГРАБЛИ: вёрстка sanjhapunjab «поехала по стилям» (2026-07-22)

Три бага «идеальной копии стилей», все общие.

**A. Шапку разорвало: баннер вложился в стилизованную полосу меню.** CMS режет шапку на два соседа
`<div id="header">` (баннер) и `<div id="nav">` (синяя полоса меню). Старый код переименовывал
`div#nav` → `header#nav` (сохраняя `id="nav"` = CSS синей полосы) и `_absorb_header_bar` засовывал
баннер ВНУТРЬ него → баннер наследовал фон/ширину/высоту полосы меню. Пользователь: «поменялось
местами, поехали стили».
→ `_wrap_nav_in_header`: полосы шапки и nav-блок оборачиваются в СВЕЖИЙ НЕЙТРАЛЬНЫЙ `<header>` (без
id/class) как СОСЕДИ; исходные блоки не трогаются, каждый сохраняет свой CSS 1-в-1. `_make_header`
выбирает: есть полоса сверху → обёртка; нет → переименование на месте (вкладывать не во что).
Правило: НИКОГДА не вкладывать полосу в блок со своим styled-id; только в нейтральный `<header>`.

**B. Ассет есть локально, а ссылка ведёт в архив.** Один стиль скачан дважды (`style.css` +
`style_1.css`); картинка тоже (`accordion_up.png` + `accordion_up_1.png`). `style.css` локализовался
при скачке, `style_1.css` остался с `url(/web/<ts>im_/http://.../accordion_up.png)`. Recovery шёл в
сеть (под троттлом падал → «мёртвая»), хотя файл лежал в той же папке.
→ `_recover_css_asset` ПЕРЕД сетью ищет локальный файл: точное имя и дедуп-вариант
`<stem>_<цифры><ext>`, проверяя, что это не wayback-HTML под именем картинки. Нашёл — берёт его, в
сеть не идёт. Чинит и лишние копии, и троттл-дропы восстановимого.

**C. `strip_dead_css_urls` убивал всё объявление.** При отсутствующем файле резал целиком
`background: #285b8b url(dead) no-repeat` → пропадал и синий цвет, блок «худел» (аккордеон стал
тоньше). → Режем ТОЛЬКО мёртвый `url()`-токен, остальное значение (цвет, позиция) сохраняем; пустое
объявление после выреза убираем — но ТОЛЬКО пустое, не `none` (`display:none` осмысленно). Негативные
тесты: `display:none`, `content:""` не трогаются; `background: #285b8b url(dead)` теряет только url.

Проверено рендером (Playwright): баннер/меню/правая колонка 1-в-1 с архивом. Регресс на 6 сайтах:
меню/карта/мёртвых/битых без изменений.

# ГРАБЛЯ: студия не показывала уже скачанные сайты после рестарта (2026-07-22)

Пользователь: «раньше показывала всё в папке, почему пропало = баг». Карточки студии хранятся в
памяти (`ENTRIES`), плюс последняя папка — тоже только в памяти (`_WB_DEST`). После рестарта сервера
и то, и другое пусто. Эндпоинт `/api/wayback/adopt` СКАНИРУЕТ папку и делает карточки из каждого
`<dest>/<domain>/index.html`, но фронт звал его лишь когда СЕРВЕР помнит папку (`if(data.dest ...)`)
— а он её после рестарта не помнил. Итог: сайты на диске есть, а студия пустая.

→ Две правки: (1) сервер запоминает папку НА ДИСК (`site_studio/.wb_dest`, `_remember_dest`/
`_restore_dest`) и восстанавливает при старте — `data.dest` снова заполнен, adopt срабатывает сам;
(2) фронт (landing.html) дублирует папку в localStorage и зовёт adopt по значению ПОЛЯ, а не только
по памяти сервера. Диск — источник правды, терять нечего. Требует РЕСТАРТ сервера (Jinja кэширует
шаблон) + жёсткий refresh браузера.

# ГРАБЛЯ: удаление href="#" ломает :link-вёрстку (2026-07-22)

Стрелки аккордеона в сайдбаре sanjhapunjab не рисовались, хотя CSS-правило и картинка были целы:
`.widget h4 a:link, .widget h4 a:visited { background: url(accordion_up.png) no-repeat 10px 50% }`.
Причина — НЕ скачивание и НЕ CSS: чистильщик УДАЛЯЛ атрибут href у мёртвых ссылок, а псевдоклассы
`:link`/`:visited` совпадают ТОЛЬКО с `<a>`, у которого ЕСТЬ href. Без href фон-стрелка не
применялась. Нашлось трассировкой (подмена `Tag.__delitem__`/`decompose`): резало в ветке
`_in_protected_cms_menu` (виджет-аккордеон сайдбара ошибочно принят за CMS-меню), плюс в ветках
keep_header_items, icon-only, body — везде `del a["href"]`.

→ Общее правило: у ОСТАВЛЯЕМОГО в DOM `<a>` мёртвую ссылку заменять на `href="#"`, а НЕ удалять
атрибут. href удаляется только вместе с самим элементом (`_remove_nav_item`). Исправлены все 4
ветки в `auto_link_menu`. Проверено рендером: стрелки появились 1-в-1 с архивом. Регресс на 6
сайтах: меню/карта/структура без изменений.

## 4. Как безопасно дорабатывать

1. Тестить на КОПИЯХ в scratchpad, не на папках пользователя.
2. После правки — регресс на `gigaworks.in` + `berhamporegirlscollege.org.in` (отчёт + **рендер**).
3. Не двигать/не удалять контент и классы, на которые есть CSS. Переименование тега — ок.
4. Детерминизм должен держать гарантии БЕЗ ИИ-ключа; агент — только улучшение.
5. Python: `%LOCALAPPDATA%\Programs\Python\Python313\python.exe`, `PYTHONIOENCODING=utf-8`. После
   правок перезапускать студию-сервер.

## 5. Дорожная карта (по приоритету боли)

1. **QA-самопроверка результата** (закрывает сц. 33, 2, 3): в конце очистки авто-проверка — остался
   ли `<base>`, все ли `<link rel=stylesheet>` ведут на существующий и настоящий CSS, есть ли
   `<header>`/`<main>`; авто-фикс стилей-которые-HTML; предупреждения в отчёт.
2. **Скорость** (сц. 32): ограниченный параллелизм восстановления, семафор ~4, перепроверить «2
   параллельно не висит».
3. **Хеддер всегда сверху** (сц. 12, 13): детект нава по содержимому + генерация; 🤖-верификатор по
   скриншоту.
4. **Семантика header→main→section→footer** (сц. 17): детерминированный каркас + 🤖-разметка ролей и
   коротких меток на любом языке.
5. **Рендер-QA агент** (сц. 5, 11, общий): скриншот результата + сравнение с архивом, флаг «сломано»
   до того, как это увидит пользователь.

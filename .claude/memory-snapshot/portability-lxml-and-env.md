---
name: portability-lxml-and-env
description: На другом компе «едет» из-за парсера (lxml vs html.parser) и отсутствия .env/ключа — результат очистки расходится
metadata:
  type: project
---

**Почему очистка одного сайта на двух компах даёт РАЗНЫЙ результат:**

1. **lxml vs html.parser (главное).** Код: `PARSER = "lxml"` если lxml импортится, иначе
   `html.parser`. На кривом архивном HTML они парсят ПО-РАЗНОМУ (lxml добавляет `<html>/<body>`,
   иначе чинит вложенность). Нет lxml на другом компе → другой DOM → другие решения чистильщика →
   вёрстка едет. requirements.txt требует `lxml>=6.1.1`, но если не поставили — расхождение.
2. **.env / ANTHROPIC_API_KEY.** `.env` в gitignore (НЕ в репо). Без него ИИ-проходы (метки секций,
   title/description, проверка лендмарков агентом) — no-op. Каркас детерминированный, но улучшенный
   результат отличается.
3. **Версии библиотек** — в requirements `>=`, не `==`: разные bs4/lxml/Pillow → тонкие различия.
4. **Playwright chromium** ставится отдельно (`playwright install chromium`) — нет → скачка падает.

**Чтобы совпадало:** на обоих компах одинаковый lxml, `.env` с ключом, `pip install -r
requirements.txt`, `playwright install chromium`. Связано: [[test-on-fresh-download]].

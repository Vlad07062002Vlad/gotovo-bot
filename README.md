# DomashkaGotovoBot

🤖 Telegram-бот для школьников 5–11 классов.  
Помогает **решать и объяснять** задания пошагово, с поддержкой фото/OCR, формул, RAG-поиска по учебникам и монетизацией через Free/Trial/Pro/кредиты.

---

## ✨ Возможности
- **Free-режим**: 3 текстовых запроса/день + 1 пробный Pro.
- **Pro-режим**: месячный лимит, приоритетные задачи, фото/сканы.
- **Кредиты**: разовые покупки через Telegram Stars / Карта / ЕРИП.
- **Follow-up**: 1 бесплатное уточнение (15 минут), дальше списание.
- **OCR**: распознавание фото (русский/белорусский/английский и др.).
- **Формулы**:
  - `x^2` → `x²`, `H2O` → `H₂O`, `log10(x)` → `log₁₀(x)`
  - поддержка `TeX: \int_0^1 x^2\,dx` → рендер PNG.
- **RAG-ВБД**: поиск правил в базе учебников (Qdrant Embedded).
- **Админ-фичи**: `/stats`, `/vdbtest`, выгрузка метрик.

---

## 🚀 Быстрый старт (локально)

```bash
# 1. Клонируем
git clone https://github.com/<you>/domashka-gotovo-bot.git
cd domashka-gotovo-bot

# 2. Создаём виртуалку
python -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# 3. Ставим зависимости
pip install -r requirements.txt

# 4. Задаём переменные окружения
cp .env.sample .env
# открой .env и пропиши TELEGRAM_TOKEN, OPENAI_API_KEY, VDB_WEBHOOK_SECRET и т.д.

# 5. Запускаем
python bot.py

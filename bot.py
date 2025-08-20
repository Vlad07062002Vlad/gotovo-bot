import os, io, base64, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import openai  # <-- так надёжнее для разных версий пакета

# ===== ЛОГИ =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("umnik-mvp")

# ===== ОКРУЖЕНИЕ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (задай: flyctl secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (задай: flyctl secrets set OPENAI_API_KEY=...)")

# ===== OpenAI =====
client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# ===== Health-check HTTP для Fly =====
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

def _run_health():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ===== Память настроек (RAM) =====
USER_MODE = defaultdict(lambda: "Готово")  # "Обучение" | "Подсказка" | "Готово"

def mode_hint(mode: str) -> str:
    return {
        "Обучение": "Задавай 2–3 наводящих вопроса, но всё же приведи верное решение.",
        "Подсказка": "Дай 3–5 подсказок и ключевые шаги без полного ответа.",
        "Готово":   "Дай полное решение и краткое объяснение."
    }.get(mode, "Дай полное решение и краткое объяснение.")

# ===== Команды =====
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Приветствие"),
        BotCommand("help", "Как пользоваться"),
        BotCommand("mode", "Режим: Обучение/Подсказка/Готово"),
        BotCommand("solve", "Решить текстовое задание: /solve ТЕКСТ"),
        BotCommand("diag", "Проверка OpenAI"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    m = USER_MODE[update.effective_user.id]
    await update.message.reply_text(
        "👋 Я «Умник» (MVP):\n"
        "• Сфоткай задание — распознаю и объясню по шагам.\n"
        "• Или /solve ТЕКСТ — решу текстом.\n"
        "• /mode Обучение|Подсказка|Готово — формат помощи.\n"
        f"Текущий режим: *{m}*.",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1) Пришли фото задания — я распознаю и объясню.\n"
        "2) /solve ТЕКСТ — решу текстовое задание.\n"
        "3) /mode Обучение|Подсказка|Готово — выбери глубину помощи.\n"
        "Цель — понять, а не списать 😉"
    )

async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text(f"Текущий режим: {USER_MODE[uid]}. Пример: /mode Подсказка")
    val = " ".join(context.args).strip().capitalize()
    if val not in ("Обучение", "Подсказка", "Готово"):
        return await update.message.reply_text("Доступно: Обучение / Подсказка / Готово")
    USER_MODE[uid] = val
    await update.message.reply_text(f"Режим установлен: {val}")

# ===== Диагностика =====
async def diag_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": "Ответь одним словом: ok"},
                {"role": "user", "content": "ping"}
            ],
            temperature=0
        )
        await update.message.reply_text(f"OpenAI OK: {resp.choices[0].message.content.strip()}")
    except Exception as e:
        log.exception("diag")
        await update.message.reply_text(f"OpenAI ERROR: {type(e).__name__}: {e}")

# ===== Решение ТЕКСТОМ =====
async def solve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Дай текст задания: /solve Найдите корень уравнения ...")
    mode = USER_MODE[uid]
    sys = (
        "Ты помощник для школьников (ФГОС, 5–11 класс). Объясняй просто, по шагам, с бытовыми аналогиями. "
        "Структура: Условие (кратко) → Решение по шагам → Кратко (2–3 предложения) → 1–2 проверочных вопроса."
    )
    user = f"Режим: {mode}. {mode_hint(mode)}\nЗадание:\n{text}"
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": user}]
        )
        out = resp.choices[0].message.content.strip()
        await update.message.reply_text(out[:4000])
    except Exception as e:
        log.exception("solve_cmd")
        await update.message.reply_text(f"Не получилось решить: {e}")

# ===== Решение С ФОТО =====
async def photo_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mode = USER_MODE[uid]
    try:
        photo = update.message.photo[-1]
        f = await context.bot.get_file(photo.file_id)
        bio = io.BytesIO()
        await f.download_to_memory(out=bio)
        b64 = base64.b64encode(bio.getvalue()).decode("ascii")

        messages = [
            {"role": "system", "content":
             "Ты помощник для школьников (ФГОС, 5–11 класс). Объясняй просто и по шагам, "
             "без перегруза терминами, добавляй аналогии."},
            {"role": "user", "content": [
                {"type": "text", "text": f"Режим: {mode}. {mode_hint(mode)}\n"
                                         "1) Распознай текст задания (исправь OCR-ошибки по смыслу).\n"
                                         "2) Реши и объясни по шагам.\n"
                                         "3) В конце — краткое резюме (2–3 предложения) и 1–2 проверочных вопроса."},
                {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}
            ]}
        ]

        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.2,
            messages=messages
        )
        out = resp.choices[0].message.content.strip()
        await update.message.reply_text(out[:4000])
    except Exception as e:
        log.exception("photo_handler")
        await update.message.reply_text(f"Не смог обработать фото: {e}")

# ===== main =====
def main():
    threading.Thread(target=_run_health, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("solve", solve_cmd))
    app.add_handler(CommandHandler("diag", diag_cmd))
    app.add_handler(MessageHandler(filters.PHOTO, photo_handler))

    log.info("Umnik MVP is running…")
    # БЕЗ allowed_updates — это может валить запуск на v21
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

import os, base64, asyncio, logging, threading
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer

from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import openai

# ===== ЛОГИ =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("gotovo-bot")

# ===== ОКРУЖЕНИЕ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY  = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (flyctl secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (flyctl secrets set OPENAI_API_KEY=...)")

# OpenAI client
client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# ===== Health-check для Fly =====
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")

def _run_health():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ===== Команды =====
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start",   "🚀 Начать"),
        BotCommand("help",    "ℹ️ Помощь"),
        BotCommand("photo",   "📷 Как прислать задание"),
        BotCommand("essay",   "📝 Написать сочинение"),
        BotCommand("explain", "🧠 Объяснить тему"),
        BotCommand("diag",    "🔧 Проверка OpenAI"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋 Я — *Готово!* 🤖\n"
        "Сфоткай задание — решу и объясню. Или:\n"
        "• /essay ТЕМА\n"
        "• /explain ВОПРОС\n",
        parse_mode="Markdown"
    )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как я помогаю:\n"
        "1) Пришли *фото* задания — распознаю и объясню по шагам.\n"
        "2) /essay тема — сочинение 150–200 слов.\n"
        "3) /explain что непонятно — объясню просто.\n"
        "Цель — ПОНЯТЬ, а не списать 😉", parse_mode="Markdown"
    )

async def photo_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("📸 Сфоткай задание и пришли сюда. Я разберу и объясню по шагам.")

async def essay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip()
    if not topic:
        return await update.message.reply_text("Напиши тему: `/essay Смысл рассказа 'Муму'`", parse_mode="Markdown")
    try:
        await update.message.chat.send_action("typing")
        resp = await client.chat.completions.create(
            model="gpt-4o",  # полнофункциональная, стабильно работает с текстом и vision
            messages=[
                {"role":"system","content":"Ты ИИ-репетитор. Пиши сочинение 150–200 слов, как ученик 8 класса. Простой язык."},
                {"role":"user","content":f"Тема сочинения: {topic}"}
            ],
            temperature=0.7,
            max_tokens=600
        )
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000])
    except Exception as e:
        log.exception("essay")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def explain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Что объяснить? Пример: `/explain как решать квадратные уравнения`", parse_mode="Markdown")
    try:
        await update.message.chat.send_action("typing")
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role":"system","content":"Объясняй просто, как старший брат. По шагам. Дай бытовую аналогию и мини-пример."},
                {"role":"user","content":f"Объясни для 8 класса: {text}"}
            ],
            temperature=0.3,
            max_tokens=800
        )
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000])
    except Exception as e:
        log.exception("explain")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        await update.message.reply_text("🔍 Анализирую задание…")
        f = await update.message.photo[-1].get_file()
        data = await f.download_as_bytearray()
        b64 = base64.b64encode(data).decode("utf-8")

        messages = [
            {"role":"system","content":"Ты ИИ-репетитор. Распознай текст с фото, исправь OCR-ошибки по смыслу. Реши и объясни по шагам. В конце добавь '💡 Совет: ...'."},
            {"role":"user","content":[
                {"type":"text","text":"Реши задание с фото и объясни просто."},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
            ]}
        ]
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages,
            temperature=0.2,
            max_tokens=1200
        )
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000])
    except Exception as e:
        log.exception("photo")
        await update.message.reply_text(f"❌ Ошибка: {e}")

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o", messages=[
                {"role":"system","content":"Ответь 'ok'"},
                {"role":"user","content":"ping"}
            ], temperature=0
        )
        await update.message.reply_text(f"OpenAI OK: {resp.choices[0].message.content.strip()}")
    except Exception as e:
        await update.message.reply_text(f"OpenAI ERROR: {type(e).__name__}: {e}")

def main():
    threading.Thread(target=_run_health, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("photo", photo_hint))
    app.add_handler(CommandHandler("essay", essay_command))
    app.add_handler(CommandHandler("explain", explain_command))
    app.add_handler(CommandHandler("diag", diag))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


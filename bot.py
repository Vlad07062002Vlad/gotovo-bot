import os, base64, logging, threading
from io import BytesIO
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters

import openai

# ===== ЛОГИ =====
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("gotovo-bot")

# ===== ОКРУЖЕНИЕ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))
if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (flyctl secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (flyctl secrets set OPENAI_API_KEY=...)")

# ===== OpenAI =====
client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# ===== Health-check для Fly =====
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
def _run_health():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ===== ПАМЯТЬ (RAM) =====
USER_SUBJECT = defaultdict(lambda: "auto")   # предмет: одно из SUBJECTS или "auto"
USER_GRADE   = defaultdict(lambda: "8")      # класс: "5"–"11"
PARENT_MODE  = defaultdict(lambda: False)    # памятка для родителей: on/off

SUBJECTS = {
    "математика","русский","английский","физика","химия",
    "история","обществознание","биология","информатика",
    "география","литература","auto"
}

# ===== УТИЛИТЫ =====
def kb(uid:int)->ReplyKeyboardMarkup:
    subj = USER_SUBJECT[uid]
    grd  = USER_GRADE[uid]
    pm   = "вкл" if PARENT_MODE[uid] else "выкл"
    return ReplyKeyboardMarkup(
        [
            ["📸 Фото задания", "🧠 Объяснить", "📝 Сочинение"],
            [f"📚 Предмет: {subj}", f"🎓 Класс: {grd}", f"👨‍👩‍👧 Родит.: {pm}"],
        ],
        resize_keyboard=True
    )

def system_prompt(subject: str, grade: str, parent: bool) -> str:
    base = (
        "Ты — ИИ-репетитор для школьников по ФГОС. Отвечай как старший брат: просто, по шагам, "
        "без заумных терминов, с короткими аналогиями. Для каждого ответа: "
        "1) Коротко переформулируй *условие*, 2) Дай *решение по шагам*, 3) Итог *Кратко* (2–3 предложения). "
        "Если уместно — 1–2 проверочных вопроса."
    )
    sub = f"Предмет: {subject}." if subject != "auto" else "Определи подходящий предмет сам."
    grd = f"Класс: {grade}."
    par = "В конце добавь краткую памятку для родителей: на что обратить внимание дома." if parent else ""
    return f"{base} {sub} {grd} {par}"

# ===== КОМАНДЫ =====
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("help", "Как пользоваться"),
        BotCommand("subject", "Задать предмет (или auto)"),
        BotCommand("grade", "Задать класс 5–11"),
        BotCommand("parent", "Режим для родителей: on/off"),
        BotCommand("essay", "Сочинение: /essay ТЕМА"),
        BotCommand("explain", "Объяснить: /explain ТЕКСТ"),
        BotCommand("diag", "Проверка OpenAI"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "👋 Привет! Я — *Готово!* Помогаю понять ДЗ.\n"
        "Пошагово, простым языком, как старший брат.\n\n"
        "Отправь фото задания или используй кнопки ниже 👇",
        reply_markup=kb(uid), parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "Как пользоваться:\n"
        "• Пришли фото задания — распознаю и объясню по шагам.\n"
        "• /explain ТЕКСТ — объясню тему простыми словами.\n"
        "• /essay ТЕМА — сочинение 150–200 слов (как ученик).\n"
        "• /subject <предмет|auto> — математика, русский, английский, физика, химия, история, обществознание, биология, информатика, география, литература.\n"
        "• /grade 5–11 — учту уровень.\n"
        "• /parent on|off — краткая памятка для родителей в конце ответа.",
        reply_markup=kb(uid)
    )

async def subject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Укажи предмет: " + ", ".join(sorted(SUBJECTS)))
    val = " ".join(context.args).strip().lower()
    if val not in SUBJECTS:
        return await update.message.reply_text("Не понял предмет. Доступно: " + ", ".join(sorted(SUBJECTS)))
    USER_SUBJECT[uid] = val
    await update.message.reply_text(f"Предмет установлен: {val}", reply_markup=kb(uid))

async def grade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or context.args[0] not in [str(i) for i in range(5,12)]:
        return await update.message.reply_text("Укажи класс 5–11. Пример: /grade 7")
    USER_GRADE[uid] = context.args[0]
    await update.message.reply_text(f"Класс установлен: {USER_GRADE[uid]}", reply_markup=kb(uid))

async def parent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or context.args[0].lower() not in {"on","off"}:
        return await update.message.reply_text("Используй: /parent on  или  /parent off")
    PARENT_MODE[uid] = (context.args[0].lower() == "on")
    state = "включён" if PARENT_MODE[uid] else "выключен"
    await update.message.reply_text(f"Режим для родителей {state}.", reply_markup=kb(uid))

# ===== ОБРАБОТЧИКИ GPT =====
async def essay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    topic = " ".join(context.args).strip()
    if not topic:
        return await update.message.reply_text("Напиши тему: `/essay Смысл рассказа 'Муму'`", parse_mode="Markdown")
    try:
        await update.message.chat.send_action("typing")
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role":"system","content": system_prompt(USER_SUBJECT[uid], USER_GRADE[uid], PARENT_MODE[uid])},
                {"role":"user","content": f"Напиши сочинение на 150–200 слов по теме: {topic}. Пиши как ученик {USER_GRADE[uid]} класса."}
            ],
            temperature=0.7, max_tokens=700
        )
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000], reply_markup=kb(uid))
    except Exception as e:
        log.exception("essay")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text:
        return await update.message.reply_text("Пример: `/explain как решать квадратные уравнения`", parse_mode="Markdown")
    try:
        await update.message.chat.send_action("typing")
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role":"system","content": system_prompt(USER_SUBJECT[uid], USER_GRADE[uid], PARENT_MODE[uid])},
                {"role":"user","content": f"Объясни простыми словами для {USER_GRADE[uid]} класса: {text}"}
            ],
            temperature=0.3, max_tokens=900
        )
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000], reply_markup=kb(uid))
    except Exception as e:
        log.exception("explain")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        await update.message.reply_text("🔍 Анализирую задание…", reply_markup=kb(uid))
        f = await update.message.photo[-1].get_file()
        data = await f.download_as_bytearray()
        b64 = base64.b64encode(data).decode("utf-8")

        messages = [
            {"role":"system","content": system_prompt(USER_SUBJECT[uid], USER_GRADE[uid], PARENT_MODE[uid])},
            {"role":"user","content":[
                {"type":"text","text":"Распознай текст с фото (исправь OCR-ошибки по смыслу). Реши и объясни по шагам. Если это язык — дай правила и примеры, если история/обществознание — короткие тезисы и ответ."},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
            ]}
        ]
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=messages, temperature=0.2, max_tokens=1200
        )
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000], reply_markup=kb(uid))
    except Exception as e:
        log.exception("photo")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"system","content":"Ответь словом 'ok'."},{"role":"user","content":"ping"}],
            temperature=0
        )
        await update.message.reply_text(f"OpenAI OK: {resp.choices[0].message.content.strip()}")
    except Exception as e:
        await update.message.reply_text(f"OpenAI ERROR: {type(e).__name__}: {e}")

# ===== MAIN =====
def main():
    threading.Thread(target=_run_health, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("subject", subject_cmd))
    app.add_handler(CommandHandler("grade", grade_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("essay", essay_cmd))
    app.add_handler(CommandHandler("explain", explain_cmd))
    app.add_handler(CommandHandler("diag", diag))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    log.info("Gotovo bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()


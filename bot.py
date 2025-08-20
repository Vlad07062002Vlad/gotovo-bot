import os, base64, logging, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict
from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
import openai

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("gotovo-bot")

# ---------- ОКРУЖЕНИЕ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))
if not TELEGRAM_TOKEN: raise SystemExit("Нет TELEGRAM_TOKEN")
if not OPENAI_API_KEY: raise SystemExit("Нет OPENAI_API_KEY")

client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------- Health-check для Fly ----------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self): self.send_response(200); self.end_headers(); self.wfile.write(b"ok")
def _run_health(): HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ---------- ПАМЯТЬ ----------
SUBJECTS = {"математика","русский","английский","физика","химия","история",
            "обществознание","биология","информатика","география","литература","auto"}
USER_SUBJECT = defaultdict(lambda: "auto")
USER_GRADE   = defaultdict(lambda: "8")
PARENT_MODE  = defaultdict(lambda: False)
USER_STATE   = defaultdict(lambda: None)  # None | "AWAIT_EXPLAIN" | "AWAIT_ESSAY"

def kb(uid:int)->ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📸 Фото задания", "🧠 Объяснить", "📝 Сочинение"],
            [f"📚 Предмет: {USER_SUBJECT[uid]}", f"🎓 Класс: {USER_GRADE[uid]}", f"👨‍👩‍👧 Родит.: {'вкл' if PARENT_MODE[uid] else 'выкл'}"],
            ["📋 Меню /menu", "ℹ️ Помощь"]
        ],
        resize_keyboard=True
    )

def sys_prompt(uid:int)->str:
    subject = USER_SUBJECT[uid]
    grade   = USER_GRADE[uid]
    parent  = PARENT_MODE[uid]
    base = ("Ты — ИИ-репетитор, объясняй как старший брат: просто, по шагам, с короткими аналогиями. "
            "Структура ответа: 1) Условие (кратко) → 2) Решение по шагам → 3) Кратко (2–3 предложения). "
            "Добавляй 1–2 проверочных вопроса.")
    sub = f"Предмет: {subject}." if subject != "auto" else "Определи предмет сам по содержанию."
    grd = f"Класс: {grade}."
    par = "В конце добавь краткую памятку для родителей." if parent else ""
    return f"{base} {sub} {grd} {par}"

# ---------- Команды ----------
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start","Запуск"),
        BotCommand("menu","Показать меню"),
        BotCommand("help","Как пользоваться"),
        BotCommand("subject","Задать предмет (или auto)"),
        BotCommand("grade","Задать класс 5–11"),
        BotCommand("parent","Режим для родителей on/off"),
        BotCommand("essay","Сочинение: /essay ТЕМА"),
        BotCommand("explain","Объяснить: /explain ТЕКСТ"),
        BotCommand("diag","Проверка OpenAI"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "👋 Привет! Я — Готово! Помогаю понять ДЗ. Пиши текст, кидай фото или воспользуйся кнопками ниже.",
        reply_markup=kb(uid)
    )

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "Как пользоваться:\n"
        "• Просто напиши, что непонятно — я объясню.\n"
        "• Пришли фото задания — распознаю и решу по шагам.\n"
        "• /essay ТЕМА — сочинение 150–200 слов.\n"
        "• /subject ПРЕДМЕТ|auto\n• /grade 5–11\n• /parent on|off",
        reply_markup=kb(uid)
    )

async def subject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args: return await update.message.reply_text("Доступно: " + ", ".join(sorted(SUBJECTS)))
    val = " ".join(context.args).strip().lower()
    if val not in SUBJECTS: return await update.message.reply_text("Не понял предмет. " + ", ".join(sorted(SUBJECTS)))
    USER_SUBJECT[uid] = val
    await update.message.reply_text(f"Предмет: {val}", reply_markup=kb(uid))

async def grade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or context.args[0] not in [str(i) for i in range(5,12)]:
        return await update.message.reply_text("Пример: /grade 7")
    USER_GRADE[uid] = context.args[0]
    await update.message.reply_text(f"Класс: {USER_GRADE[uid]}", reply_markup=kb(uid))

async def parent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or context.args[0].lower() not in {"on","off"}:
        return await update.message.reply_text("Используй: /parent on  или  /parent off")
    PARENT_MODE[uid] = (context.args[0].lower()=="on")
    await update.message.reply_text(f"Режим для родителей: {'вкл' if PARENT_MODE[uid] else 'выкл'}", reply_markup=kb(uid))

# ---------- GPT-хэндлеры ----------
async def gpt_explain(uid:int, prompt:str)->str:
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role":"system","content": sys_prompt(uid)},
            {"role":"user","content": f"Объясни простыми словами: {prompt}"}
        ],
        temperature=0.3, max_tokens=900
    )
    return resp.choices[0].message.content.strip()

async def gpt_essay(uid:int, topic:str)->str:
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role":"system","content": sys_prompt(uid)},
            {"role":"user","content": f"Напиши сочинение на 150–200 слов. Тема: {topic}. Пиши как ученик {USER_GRADE[uid]} класса."}
        ],
        temperature=0.7, max_tokens=700
    )
    return resp.choices[0].message.content.strip()

async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text: return await ask_explain(update, uid)
    try:
        await update.message.chat.send_action("typing")
        out = await gpt_explain(uid, text)
        await update.message.reply_text(out[:4000], reply_markup=kb(uid))
    except Exception as e:
        log.exception("explain"); await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def essay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    topic = " ".join(context.args).strip()
    if not topic: return await ask_essay(update, uid)
    try:
        await update.message.chat.send_action("typing")
        out = await gpt_essay(uid, topic)
        await update.message.reply_text(out[:4000], reply_markup=kb(uid))
    except Exception as e:
        log.exception("essay"); await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        await update.message.chat.send_action("typing")
        file = await update.message.photo[-1].get_file()
        data = await file.download_as_bytearray()
        b64 = base64.b64encode(data).decode("utf-8")

        msgs = [
            {"role":"system","content": sys_prompt(uid)},
            {"role":"user","content":[
                {"type":"text","text":"Распознай задание с фото (исправь OCR-ошибки по смыслу), реши и объясни по шагам."},
                {"type":"image_url","image_url":{"url":f"data:image/jpeg;base64,{b64}"}}
            ]}
        ]
        resp = await client.chat.completions.create(model="gpt-4o", messages=msgs, temperature=0.2, max_tokens=1200)
        await update.message.reply_text(resp.choices[0].message.content.strip()[:4000], reply_markup=kb(uid))
    except Exception as e:
        log.exception("photo"); await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

# ---------- Кнопки и «умный» текст ----------
async def ask_explain(update: Update, uid:int):
    USER_STATE[uid] = "AWAIT_EXPLAIN"
    await update.message.reply_text("🧠 Что объяснить? Напиши одной фразой.", reply_markup=kb(uid))

async def ask_essay(update: Update, uid:int):
    USER_STATE[uid] = "AWAIT_ESSAY"
    await update.message.reply_text("📝 Тема сочинения?", reply_markup=kb(uid))

async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # Нажатия на кнопки
    if text == "🧠 Объяснить":     return await ask_explain(update, uid)
    if text == "📝 Сочинение":     return await ask_essay(update, uid)
    if text == "📸 Фото задания":  return await update.message.reply_text("Отправь фото сообщением — я его распознаю.", reply_markup=kb(uid))
    if text.startswith("📚 Предмет:"): return await update.message.reply_text("Сменить предмет командой: /subject <название|auto>", reply_markup=kb(uid))
    if text.startswith("🎓 Класс:"):   return await update.message.reply_text("Сменить класс командой: /grade 5–11", reply_markup=kb(uid))
    if text.startswith("👨‍👩‍👧 Родит.:"): return await update.message.reply_text("Включить/выключить: /parent on|off", reply_markup=kb(uid))
    if text in {"📋 Меню /menu", "ℹ️ Помощь"}: return await help_cmd(update, context)

    # Обработка состояний
    state = USER_STATE[uid]
    if state == "AWAIT_EXPLAIN":
        USER_STATE[uid] = None
        return await explain_cmd(update, ContextTypes.DEFAULT_TYPE, )  # будет вызван ниже через «молчаливую» ветку

    if state == "AWAIT_ESSAY":
        USER_STATE[uid] = None
        # подменим аргументы и переиспользуем essay_cmd
        context.args = [text]
        return await essay_cmd(update, context)

    # Молчаливое поведение: любое обычное сообщение — это запрос на объяснение
    context.args = [text]
    return await explain_cmd(update, context)

# ---------- Диагностика ----------
async def diag(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[{"role":"system","content":"Ответь 'ok'."},{"role":"user","content":"ping"}],
            temperature=0
        )
        await update.message.reply_text(f"OpenAI OK: {resp.choices[0].message.content.strip()}")
    except Exception as e:
        await update.message.reply_text(f"OpenAI ERROR: {type(e).__name__}: {e}")

# ---------- MAIN ----------
def main():
    threading.Thread(target=_run_health, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu",  menu_cmd))
    app.add_handler(CommandHandler("help",  help_cmd))
    app.add_handler(CommandHandler("subject", subject_cmd))
    app.add_handler(CommandHandler("grade", grade_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("essay", essay_cmd))
    app.add_handler(CommandHandler("explain", explain_cmd))
    app.add_handler(CommandHandler("diag", diag))

    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Gotovo bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

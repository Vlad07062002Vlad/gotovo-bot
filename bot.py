import os
import io
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from openai import AsyncOpenAI
from PIL import Image
import pytesseract
# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("gotovo-bot")

# ---------- ОКРУЖЕНИЕ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (flyctl secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (flyctl secrets set OPENAI_API_KEY=...)")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------- Health-check для Fly ----------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

def _run_health():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ---------- ПАМЯТЬ (RAM) ----------
SUBJECTS = {
    "математика", "русский", "английский", "физика", "химия",
    "история", "обществознание", "биология", "информатика",
    "география", "литература", "auto",
    "беларуская мова", "беларуская літаратура"
}
USER_SUBJECT = defaultdict(lambda: "auto")
USER_GRADE = defaultdict(lambda: "8")
PARENT_MODE = defaultdict(lambda: False)
USER_STATE = defaultdict(lambda: None)  # None | "AWAIT_EXPLAIN" | "AWAIT_ESSAY" | "AWAIT_FOLLOWUP" | "AWAIT_TEXT_OR_PHOTO_CHOICE"

def kb(uid: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📸 Фото задания", "🧠 Объяснить", "📝 Сочинение"],
            [f"📚 Предмет: {USER_SUBJECT[uid]}", f"🎓 Класс: {USER_GRADE[uid]}", f"👨‍👩‍👧 Родит.: {'вкл' if PARENT_MODE[uid] else 'выкл'}"],
            ["📋 Меню /menu", "ℹ️ Помощь"]
        ],
        resize_keyboard=True
    )

def sys_prompt(uid: int) -> str:
    subject = USER_SUBJECT[uid]
    grade = USER_GRADE[uid]
    parent = PARENT_MODE[uid]

    # Поддержка белорусского языка
    if subject in ["беларуская мова", "беларуская літаратура"]:
        return (
            "Ты — ІІ-памочнік па беларускай мове і літаратуры. "
            "Адказвай на беларускай, калі заданне на беларускай. "
            "Калі на расейскай — адказвай на расейскай. "
            "Памятай пра правілы: літара 'ў', мяккі знак, і інш. "
            "Адказ пішы толькі на беларускай мове. "
            "Выкарыстоўвай толькі HTML-тэгі для фарматавання: <b>тлусты</b>, <i>курсіў</i>, <code>код</code>. "
            "Не выкарыстоўвай Markdown (**, *, `)."
        )

    base = (
        "Ты — ИИ-репетитор. Объясняй как старший брат: просто, по шагам, с короткими аналогиями. "
        "Структура: 1) Условие → 2) Решение по шагам (с объяснением каждого действия) → 3) Кратко (для тех, кто не понял с первого раза). "
        "Добавляй 1–2 вопроса ПО ЭТОМУ ЗАДАНИЮ — с подсказками, не давая полный ответ. "
        "Не задавай общие вопросы. Не уходи в темы, не связанные с заданием. "
        "Не вступай в диалог. Вопрос — это часть объяснения, не приглашение к беседе. "
        "Ответ должен использовать только HTML-теги для форматирования: <b>жирный</b>, <i>курсив</i>, <code>моноширинный</code>. "
        "Не используй Markdown (**, *, `)."
    )
    sub = f"Предмет: {subject}." if subject != "auto" else "Определи сам."
    grd = f"Класс: {grade}."
    par = (
        "<b>Памятка для родителей:</b><br>"
        "1) Какая тема изучается.<br>"
        "2) Что важно проверить у ребёнка.<br>"
        "3) Как мягко помочь, если не понимает."
    ) if parent else ""
    return f"{base} {sub} {grd} {par}"

# ---------- КОМАНДЫ ----------
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Запуск"),
        BotCommand("menu", "Меню"),
        BotCommand("help", "Помощь"),
        BotCommand("subject", "Предмет (или auto)"),
        BotCommand("grade", "Класс 5–11"),
        BotCommand("parent", "Режим родителей on/off"),
        BotCommand("essay", "Сочинение: /essay ТЕМА"),
        BotCommand("explain", "Объяснить: /explain ТЕКСТ"),
        BotCommand("about", "О боте и как пользоваться"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(
        "👋 Привет! Я — <b>Готово!</b> Помогаю понять ДЗ.\n"
        "Пиши текст, кидай фото или жми кнопки ниже.",
        reply_markup=kb(uid),
        parse_mode="HTML"
    )

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await about_cmd(update, context)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "<b>📘 О боте «Готово!»</b>\n\n"
        "Я — школьный помощник, который помогает с домашкой, "
        "объясняя как старший брат: просто, по шагам, без воды.\n\n"

        "<b>🎯 Что я умею:</b>\n"
        "• 📸 Присылай фото задания — я его распознаю, решу и объясню\n"
        "• 🧠 Напиши /explain — объясню любую тему\n"
        "• 📝 Напиши /essay — напишу сочинение\n"
        "• 📚 Можешь выбрать предмет и класс\n"
        "• 👨‍👩‍👧 Включи режим для родителей — получишь памятку\n\n"

        "<b>📌 Как пользоваться:</b>\n"
        "1. Жми кнопки в меню\n"
        "2. Или пиши команду: /help, /essay, /explain\n"
        "3. После ответа — можешь уточнить: «Да» или «Нет»\n\n"

        "<b>💡 Совет:</b> Если фото не распознал — попробуй переснять или напиши текстом.\n\n"

        "Создан для учеников 5–11 классов. © 2025",
        parse_mode="HTML",
        reply_markup=kb(update.effective_user.id)
    )

async def subject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Доступно: " + ", ".join(sorted(SUBJECTS)), reply_markup=kb(uid))
    val = " ".join(context.args).strip().lower()
    if val not in SUBJECTS:
        return await update.message.reply_text("Не понял предмет. Доступно: " + ", ".join(sorted(SUBJECTS)), reply_markup=kb(uid))
    USER_SUBJECT[uid] = val
    await update.message.reply_text(f"Предмет: {val}", reply_markup=kb(uid))

async def grade_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or context.args[0] not in [str(i) for i in range(5, 12)]:
        return await update.message.reply_text("Пример: /grade 7", reply_markup=kb(uid))
    USER_GRADE[uid] = context.args[0]
    await update.message.reply_text(f"Класс: {USER_GRADE[uid]}", reply_markup=kb(uid))

async def parent_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args or context.args[0].lower() not in {"on", "off"}:
        return await update.message.reply_text("Используй: /parent on  или  /parent off", reply_markup=kb(uid))
    PARENT_MODE[uid] = (context.args[0].lower() == "on")
    status = "вкл" if PARENT_MODE[uid] else "выкл"
    await update.message.reply_text(f"Режим для родителей: {status}", reply_markup=kb(uid))

# ---------- GPT-хелперы ----------
async def gpt_explain(uid: int, prompt: str) -> str:
    log.info(f"EXPLAIN uid={uid} subj={USER_SUBJECT[uid]} grade={USER_GRADE[uid]} text={prompt[:60]}")
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": sys_prompt(uid)},
            {"role": "user", "content": f"Объясни простыми словами: {prompt}"}
        ],
        temperature=0.3,
        max_tokens=900
    )
    return resp.choices[0].message.content.strip()

async def gpt_essay(uid: int, topic: str) -> str:
    log.info(f"ESSAY uid={uid} topic={topic[:60]}")
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": f"Ты — ученик {USER_GRADE[uid]} класса. Пиши сочинение как ученик: просто, по делу, 150–200 слов. Без вступлений в диалог. Ответ должен использовать только HTML-теги для форматирования: <b>жирный</b>, <i>курсив</i>, <code>моноширинный</code>. Не используй Markdown (**, *, `)."},
            {"role": "user", "content": f"Напиши сочинение. Тема: {topic}"}
        ],
        temperature=0.7,
        max_tokens=700
    )
    return resp.choices[0].message.content.strip()

# ---------- Обработчики ----------
async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(context.args).strip()
    if not text:
        USER_STATE[uid] = "AWAIT_EXPLAIN"
        return await update.message.reply_text("🧠 Что объяснить? Напиши одной фразой.", reply_markup=kb(uid))
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await gpt_explain(uid, text)
        await update.message.reply_text(out[:4000], reply_markup=kb(uid), parse_mode="HTML", disable_web_page_preview=True)
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Хочешь уточнить что-то по этому заданию?", reply_markup=keyboard)
        USER_STATE[uid] = "AWAIT_FOLLOWUP"
    except Exception as e:
        log.exception("explain")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def essay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    topic = " ".join(context.args).strip()
    if not topic:
        USER_STATE[uid] = "AWAIT_ESSAY"
        return await update.message.reply_text("📝 Тема сочинения?", reply_markup=kb(uid))
    
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        
        # Шаг 1: Написать сочинение
        essay = await gpt_essay(uid, topic)
        await update.message.reply_text(essay[:4000], parse_mode="HTML", disable_web_page_preview=True)

        # Шаг 2: Объяснить, как писать такие сочинения
        explain_prompt = (
            f"Объясни, как написать сочинение на тему: '{topic}'. "
            "Структура: 1) Условие → 2) Решение по шагам (как построить текст) → 3) Кратко. "
            "Добавь 1–2 вопроса для закрепления. "
            "Ответ должен использовать только HTML-теги для форматирования: <b>жирный</b>, <i>курсив</i>, <code>моноширинный</code>. Не используй Markdown (**, *, `)."
        )
        explanation = await gpt_explain(uid, explain_prompt)
        await update.message.reply_text(explanation[:4000], parse_mode="HTML", disable_web_page_preview=True)

        # Шаг 3: Предложить уточнить
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Хочешь уточнить по сочинению?", reply_markup=keyboard)
        USER_STATE[uid] = "AWAIT_FOLLOWUP"

    except Exception as e:
        log.exception("essay")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        file = await update.message.photo[-1].get_file()
        data = await file.download_as_bytearray()
        img = Image.open(io.BytesIO(data))
        ocr_text = pytesseract.image_to_string(img, lang="rus+eng")
        log.info(f"OCR uid={uid} text={ocr_text.strip()!r}")

        if not ocr_text.strip():
            raise ValueError("OCR returned empty text")

        await update.message.reply_text(f"🔎 Распознанный текст:\n{ocr_text.strip()}", reply_markup=kb(uid))

        out = await gpt_explain(uid, ocr_text)
        
        await update.message.reply_text(out[:4000], reply_markup=kb(uid), parse_mode="HTML", disable_web_page_preview=True)
    except Exception as e:
        log.exception("photo")
        keyboard = ReplyKeyboardMarkup(
            [["📸 Решить по фото", "✍️ Напишу текстом"]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await update.message.reply_text(
            "Не удалось обработать фото. Попробуй ещё раз или введи текстом:",
            reply_markup=keyboard
        )
        USER_STATE[uid] = "AWAIT_TEXT_OR_PHOTO_CHOICE"

# ---------- Текст и кнопки ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip().lower()
    state = USER_STATE[uid]

    # Обработка выбора после фото
    if state == "AWAIT_TEXT_OR_PHOTO_CHOICE":
        if text == "📸 решить по фото":
            USER_STATE[uid] = None
            return await update.message.reply_text("Хорошо! Пришли фото задания.", reply_markup=kb(uid))
        elif text == "✍️ напишу текстом":
            USER_STATE[uid] = "AWAIT_EXPLAIN"
            return await update.message.reply_text("Напиши задание текстом — я помогу.", reply_markup=kb(uid))
        else:
            return await update.message.reply_text("Выбери: 'Решить по фото' или 'Напишу текстом'")

    # Обработка уточнения
    if state == "AWAIT_FOLLOWUP":
        if text == "да":
            USER_STATE[uid] = "AWAIT_EXPLAIN"
            return await update.message.reply_text("Что именно непонятно?", reply_markup=kb(uid))
        elif text == "нет":
            USER_STATE[uid] = None
            return await update.message.reply_text("Хорошо! Если что — пиши снова.", reply_markup=kb(uid))
        else:
            return await update.message.reply_text("Ответь: Да или Нет")

    # Кнопки
    if text == "🧠 объяснить":           return await explain_cmd(update, context)
    if text == "📝 сочинение":           return await essay_cmd(update, context)
    if text == "📸 фото задания":        return await update.message.reply_text("Отправь фото сообщением — я распознаю и объясню.", reply_markup=kb(uid))
    if text.startswith("📚 предмет:"):   return await update.message.reply_text("Сменить: /subject <название|auto>", reply_markup=kb(uid))
    if text.startswith("🎓 класс:"):     return await update.message.reply_text("Сменить: /grade 5–11", reply_markup=kb(uid))
    if text.startswith("👨‍👩‍👧 родит.:"): return await update.message.reply_text("Вкл/выкл: /parent on|off", reply_markup=kb(uid))
    if text in {"📋 меню /menu", "ℹ️ помощь"}: return await help_cmd(update, context)

    # Состояния
    if state == "AWAIT_EXPLAIN":
        USER_STATE[uid] = None
        context.args = [text]
        return await explain_cmd(update, context)
    if state == "AWAIT_ESSAY":
        USER_STATE[uid] = None
        context.args = [text]
        return await essay_cmd(update, context)

    # Любой текст = объяснить
    context.args = [text]
    return await explain_cmd(update, context)

# ---------- MAIN ----------
def main():
    threading.Thread(target=_run_health, daemon=True).start()
    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = set_commands

    # Команды
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("subject", subject_cmd))
    app.add_handler(CommandHandler("grade", grade_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("essay", essay_cmd))
    app.add_handler(CommandHandler("explain", explain_cmd))

    # Обработчики
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    log.info("Gotovo bot is running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

import os
import io
import re
import html
import logging
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict

from telegram import Update, BotCommand, ReplyKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters as f
from openai import AsyncOpenAI

from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract
from pytesseract import TesseractError

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("gotovo-bot")

# ---------- ОКРУЖЕНИЕ ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

# OCR конфиги/языки (можно переопределить в env)
TESS_LANGS_DEFAULT = "bel+rus+eng"
TESS_LANGS = os.getenv("TESS_LANGS", TESS_LANGS_DEFAULT)
TESS_CONFIG = os.getenv("TESS_CONFIG", "--oem 3 --psm 6 -c preserve_interword_spaces=1")

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
USER_LANG = defaultdict(lambda: "auto")  # 'auto' | 'ru' | 'be'

def kb(uid: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["📸 Фото задания", "🧠 Объяснить", "📝 Сочинение"],
            [f"📚 Предмет: {USER_SUBJECT[uid]}", f"🎓 Класс: {USER_GRADE[uid]}", f"👨‍👩‍👧 Родит.: {'вкл' if PARENT_MODE[uid] else 'выкл'}"],
            ["📋 Меню /menu", "ℹ️ Помощь"]
        ],
        resize_keyboard=True
    )

# ---------- НАДЁЖНАЯ ОЧИСТКА HTML ----------
ALLOWED_TAGS = {"b", "i", "code", "pre"}  # <a> убираем — не нужен и может ломать Telegram
_TAG_OPEN = {t: f"&lt;{t}&gt;" for t in ALLOWED_TAGS}
_TAG_CLOSE = {t: f"&lt;/{t}&gt;" for t in ALLOWED_TAGS}

def sanitize_html(text: str) -> str:
    """Экранируем всё и точечно возвращаем только <b>, <i>, <code>, <pre>."""
    if not text:
        return ""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    escaped = html.escape(text, quote=False)
    for t in ALLOWED_TAGS:
        escaped = re.sub(
            fr"{_TAG_OPEN[t]}(.*?){_TAG_CLOSE[t]}",
            fr"<{t}>\1</{t}>",
            escaped,
            flags=re.IGNORECASE | re.DOTALL,
        )
    return escaped[:4000]

async def safe_reply_html(message, text: str, **kwargs):
    """Пробуем отправить как HTML; при ошибке Telegram шлём как обычный текст."""
    try:
        return await message.reply_text(
            sanitize_html(text),
            parse_mode="HTML",
            disable_web_page_preview=True,
            **kwargs
        )
    except BadRequest as e:
        if "Can't parse entities" in str(e):
            return await message.reply_text(
                html.escape(text)[:4000],
                disable_web_page_preview=True,
                **kwargs
            )
        raise

# ---------- ЯЗЫК ВВОДА (RU / BE) ----------
def detect_lang(text: str) -> str:
    """Грубая, но надёжная эвристика: если есть 'ў' — это белорусский."""
    t = (text or "").lower()
    if "ў" in t:
        return "be"
    # доп. подсказка: много 'і' и мало 'и'
    if t.count("і") >= 2 and t.count("и") == 0:
        return "be"
    return "ru"

def sys_prompt(uid: int) -> str:
    subject = USER_SUBJECT[uid]
    grade = USER_GRADE[uid]
    parent = PARENT_MODE[uid]

    be_needed = subject in ["беларуская мова", "беларуская літаратура"] or USER_LANG[uid] == "be"

    if be_needed:
        base = (
            "Ты — ІІ-памочнік для школьнікаў і ВЫКАНАЎЦА Д/З. "
            "Калі перад табой школьнае заданне (упражненні, пропускі, скланенні, нумараваныя пункты), "
            "РАБІ ПРАЦУ ПАЛНАСЦЮ: спачатку дай <b>Адказы</b> (гатовыя вынікі па пунктах: устаўленыя літары/канчаткі, адноўленыя словы, "
            "вызначаныя склон/скланенне, спісы і г.д.), потым коратка <b>Тлумачэнне</b> па кроках. "
            "Калі просяць «падфарбаваць/падкрэсліць/злучыць стрэлкамі» — дай тэкставае прадстаўленне (напрыклад: "
            "«слова — 1-е скланенне [сіні]», «злучыць: А→1, Б→3»). "
            "Калі на старонцы некалькі заданняў — выконвай кожнае асобна: <b>Заданне 1</b>, <b>Заданне 2</b>… "
            "Не прапаноўвай карыстальніку нічога рабіць самастойна; адказ павінен быць самадастатковы. "
            "Выкарыстоўвай толькі HTML-тэгі: <b>, <i>, <code>, <pre>."
        )
    else:
        base = (
            "Ты — ИИ-репетитор и ИСПОЛНИТЕЛЬ ДЗ. "
            "Если перед тобой школьное задание (упражнения, пропуски, склонения, нумерованные пункты) — "
            "ВЫПОЛНЯЙ РАБОТУ ПОЛНОСТЬЮ: сначала дай <b>Ответы</b> (готовые результаты по пунктам: вставленные буквы/окончания, "
            "восстановленные слова, определённые падежи/склонения, списки и т.п.), затем краткое <b>Пояснение</b> по шагам. "
            "Если просят «раскрасить/подчеркнуть/соединить стрелками» — выдай текстовое представление (например: "
            "«слово — 1-е склонение [синий]», «соотнести: А→1, Б→3»). "
            "Если на странице несколько заданий — решай каждое отдельно: <b>Задание 1</b>, <b>Задание 2</b>… "
            "Не проси пользователя что-то доделывать; ответ должен быть самодостаточным. "
            "Используй только HTML-теги: <b>, <i>, <code>, <pre>."
        )

    sub = f"Предмет: {subject}." if subject != "auto" else "Определи предмет сам."
    grd = f"Класс: {grade}."
    par = (
        "<b>Памятка для родителей:</b><br>"
        "1) Какая тема изучается.<br>"
        "2) Что важно проверить у ребёнка.<br>"
        "3) Как мягко помочь, если не понимает."
    ) if parent else ""
    return f"{base} {sub} {grd} {par}"


# ---------- OCR: препроцесс и каскад языков ----------
def _preprocess_image(img: Image.Image) -> Image.Image:
    # автоповорот по EXIF
    img = ImageOps.exif_transpose(img)
    # к ч/б + автоконтраст
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    # лёгкое шумоподавление/резкость
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = ImageEnhance.Sharpness(img).enhance(1.2)
    # апскейл для мелкого текста
    max_w = 1800
    if img.width < max_w:
        scale = min(max_w / img.width, 3.0)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img

def _ocr_with_langs(img: Image.Image, langs_list) -> str:
    """Пробуем языки по очереди, пока не получится что-то осмысленное."""
    for langs in langs_list:
        try:
            txt = pytesseract.image_to_string(img, lang=langs, config=TESS_CONFIG)
            if txt and txt.strip():
                log.info(f"OCR success with langs='{langs}': {repr(txt[:60])}")
                return txt.strip()
        except TesseractError as e:
            log.warning(f"OCR langs='{langs}' failed: {e}")
            continue
    return ""

def ocr_image(img: Image.Image) -> str:
    # базовый поворот по EXIF
    base = ImageOps.exif_transpose(img)

    # цепочка языков: env → bel+rus+eng → rus+eng → rus → eng
    langs_chain = []
    if TESS_LANGS:
        langs_chain.append(TESS_LANGS)
    for l in ("bel+rus+eng", "rus+eng", "rus", "eng"):
        if l not in langs_chain:
            langs_chain.append(l)

    # пробуем определить угол через OSD (если доступно)
    angles = []
    try:
        osd = pytesseract.image_to_osd(base, config="--psm 0")
        m = re.search(r"(?:Rotate|Orientation in degrees):\s*(\d+)", osd)
        if m:
            angles.append(int(m.group(1)) % 360)
    except TesseractError as e:
        log.warning(f"OSD failed: {e}")

    # всегда перебираем 0/90/180/270, начиная с угла из OSD (если был)
    tried = set()
    for a in angles + [0, 90, 180, 270]:
        a %= 360
        if a in tried:
            continue
        tried.add(a)
        rot = base.rotate(-a, expand=True)        # поворачиваем «в ноль»
        pimg = _preprocess_image(rot)             # препроцесс
        txt = _ocr_with_langs(pimg, langs_chain)  # каскад языков
        if txt and txt.strip():
            log.info(f"OCR best_angle={a} len={len(txt)}")
            return txt.strip()

    # последний шанс — без препроцесса на оставшихся углах
    for a in (0, 90, 180, 270):
        if a in tried:
            continue
        rot = base.rotate(-a, expand=True)
        txt = _ocr_with_langs(rot, ["rus+eng", "rus", "eng"])
        if txt and txt.strip():
            log.info(f"OCR fallback_angle={a} len={len(txt)}")
            return txt.strip()

    return ""


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
    await safe_reply_html(
        update.message,
        "👋 Привет! Я — <b>Готово!</b> Помогаю понять ДЗ.\n"
        "Пиши текст, кидай фото или жми кнопки ниже.",
        reply_markup=kb(uid)
    )

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start(update, context)

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await about_cmd(update, context)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_html(
        update.message,
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
async def gpt_explain(uid: int, prompt: str, prepend_prompt: bool = True) -> str:
    log.info(f"EXPLAIN/ SOLVE uid={uid} subj={USER_SUBJECT[uid]} grade={USER_GRADE[uid]} text={prompt[:60]}")
    # определяем язык входа (для формулировки задания)
    USER_LANG[uid] = detect_lang(prompt)

    if not prepend_prompt:
        user_content = prompt
    else:
        if USER_LANG[uid] == "be":
            user_content = (
                "Выканацце заданне цалкам. "
                "Спачатку дайце <b>Адказы</b> па пунктах (устаўце прапушчаныя літары/канчаткі, адновіце словы, "
                "вызначце склон/скланенне; калі патрабуецца «падфарбаваць/падкрэсліць/злучыць» — дайце тэкставае прадстаўленне). "
                "Пасля гэтага коратка дайце <b>Тлумачэнне</b> па кроках. "
                f"Тэкст/умова: {prompt}"
            )
        else:
            user_content = (
                "Реши задание полностью. "
                "Сначала дай <b>Ответы</b> по пунктам (вставь пропущенные буквы/окончания, восстанови слова, "
                "определи падеж/склонение; если нужно «раскрасить/подчеркнуть/соединить» — дай текстовое представление). "
                "Затем краткое <b>Пояснение</b> по шагам. "
                f"Текст/условие: {prompt}"
            )

    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": sys_prompt(uid)},
            {"role": "user", "content": user_content}
        ],
        temperature=0.3,
        max_tokens=900
    )
    return resp.choices[0].message.content.strip()


async def gpt_essay(uid: int, topic: str) -> str:
    log.info(f"ESSAY uid={uid} topic={topic[:60]}")
    USER_LANG[uid] = detect_lang(topic)
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": sys_prompt(uid)},
            {"role": "user", "content": f"Напиши сочинение. Тема: {topic}"}
        ],
        temperature=0.7,
        max_tokens=1200
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
        await safe_reply_html(update.message, out, reply_markup=kb(uid))
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
        essay = await gpt_essay(uid, topic)
        await safe_reply_html(update.message, essay, reply_markup=kb(uid))

        plan_prompt = (
            f"Составь нумерованный план сочинения на тему '{topic}'. "
            "Каждый пункт короткий. Используй только HTML-теги <b>, <i>, <code>, <pre>."
        )
        plan = await gpt_explain(uid, plan_prompt, prepend_prompt=False)
        await safe_reply_html(update.message, plan, reply_markup=kb(uid))

        reason_prompt = (
            f"Кратко объясни, почему для сочинения на тему '{topic}' выбран такой план. "
            "Ответ должен использовать только HTML-теги <b>, <i>, <code>, <pre>."
        )
        reason = await gpt_explain(uid, reason_prompt, prepend_prompt=False)
        await safe_reply_html(update.message, reason, reply_markup=kb(uid))

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

        # Берём картинку как из photo, так и из document (если это image/*)
        tg_file = None
        if update.message.photo:
            tg_file = await update.message.photo[-1].get_file()
        elif update.message.document and str(update.message.document.mime_type or "").startswith("image/"):
            tg_file = await update.message.document.get_file()
        else:
            raise ValueError("No image provided")

        data = await tg_file.download_as_bytearray()
        img = Image.open(io.BytesIO(data))

        # OCR
        ocr_text = ocr_image(img)
        log.info(f"OCR uid={uid} text={ocr_text!r}")

        if not ocr_text or not ocr_text.strip():
            raise ValueError("OCR returned empty text")

        # автоопределение языка по фото
        USER_LANG[uid] = detect_lang(ocr_text)

        ocr_text = ocr_text[:4000]  # ограничение длины
        out = await gpt_explain(uid, ocr_text)
        await safe_reply_html(update.message, out, reply_markup=kb(uid))

    except Exception:
        log.exception("photo")
        keyboard = ReplyKeyboardMarkup(
            [["📸 Решить по фото", "✍️ Напишу текстом"]],
            resize_keyboard=True,
            one_time_keyboard=True
        )
        await update.message.reply_text(
            "Не удалось обработать изображение. Попробуй ещё раз или введи текстом:",
            reply_markup=keyboard
        )
        USER_STATE[uid] = "AWAIT_TEXT_OR_PHOTO_CHOICE"


# ---------- Текст и кнопки ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw_text = (update.message.text or "").strip()
    text = raw_text.lower()
    state = USER_STATE[uid]

    # Авто-детект языка для обычного текста
    if raw_text:
        USER_LANG[uid] = detect_lang(raw_text)

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
    if text == "🧠 объяснить":
        return await explain_cmd(update, context)
    if text == "📝 сочинение":
        return await essay_cmd(update, context)
    if text == "📸 фото задания":
        return await update.message.reply_text(
            "Отправь фото сообщением — я распознаю и объясню.",
            reply_markup=kb(uid),
        )
    if text.startswith("📚 предмет:"):
        return await update.message.reply_text("Сменить: /subject <название|auto>", reply_markup=kb(uid))
    if text.startswith("🎓 класс:"):
        return await update.message.reply_text("Сменить: /grade 5–11", reply_markup=kb(uid))
    if text.startswith("👨‍👩‍👧 родит.:"):
        return await update.message.reply_text("Вкл/выкл: /parent on|off", reply_markup=kb(uid))
    if text in {"📋 меню /menu", "ℹ️ помощь"}:
        return await help_cmd(update, context)

    # Состояния
    if state == "AWAIT_EXPLAIN":
        USER_STATE[uid] = None
        context.args = [raw_text]
        return await explain_cmd(update, context)
    if state == "AWAIT_ESSAY":
        USER_STATE[uid] = None
        context.args = [raw_text]
        return await essay_cmd(update, context)

    # Любой текст = объяснить
    context.args = [raw_text]
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
    app.add_handler(MessageHandler(f.PHOTO | f.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(f.TEXT & ~f.COMMAND, on_text))

    log.info("Gotovo bot is running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

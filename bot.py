import os
import io
import re
import html
import json
import time
import tempfile
import logging
import threading
import asyncio
from time import perf_counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, Counter

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

# Путь/период автосейва метрик
DATA_DIR = os.getenv("DATA_DIR", "/data")
METRICS_PATH = os.getenv("METRICS_PATH", os.path.join(DATA_DIR, "metrics.json"))
METRICS_AUTOSAVE_SEC = int(os.getenv("METRICS_AUTOSAVE_SEC", "60"))

# OCR конфиги/языки (можно переопределить в env)
TESS_LANGS_DEFAULT = "rus+bel+eng+deu+fra"
TESS_LANGS = os.getenv("TESS_LANGS", TESS_LANGS_DEFAULT)
TESS_CONFIG = os.getenv("TESS_CONFIG", "--oem 3 --psm 6 -c preserve_interword_spaces=1")

if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (flyctl secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (flyctl secrets set OPENAI_API_KEY=...)")

client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ---------- СТАТИСТИКА ----------
STATS_LOCK = threading.RLock()

class UserStats:
    __slots__ = (
        "uid", "name", "username",
        "first_seen", "last_seen",
        "kinds", "subjects", "langs",
        "gpt_calls", "gpt_time_sum", "tok_prompt", "tok_completion",
        "ocr_ok", "ocr_fail", "bytes_images_in",
    )
    def __init__(self, uid: int):
        self.uid = uid
        self.name = ""
        self.username = ""
        now = time.time()
        self.first_seen = now
        self.last_seen = now
        self.kinds = Counter()       # text_msg/photo_msg/solve_text/solve_photo/essay
        self.subjects = Counter()    # предметы
        self.langs = Counter()       # ru/be/en/de/fr
        self.gpt_calls = 0
        self.gpt_time_sum = 0.0
        self.tok_prompt = 0
        self.tok_completion = 0
        self.ocr_ok = 0
        self.ocr_fail = 0
        self.bytes_images_in = 0

USERS = {}  # uid -> UserStats

def _get_user_stats(uid: int, update: Update | None = None) -> UserStats:
    with STATS_LOCK:
        st = USERS.get(uid)
        if not st:
            st = UserStats(uid)
            USERS[uid] = st
        st.last_seen = time.time()
        if update and update.effective_user:
            st.name = update.effective_user.full_name or st.name
            st.username = update.effective_user.username or st.username
        return st

def stats_snapshot() -> dict:
    with STATS_LOCK:
        snap_users = {}
        totals = {
            "users_count": 0,
            "tasks_total": 0,
            "solve_text": 0,
            "solve_photo": 0,
            "essay": 0,
            "text_msg": 0,
            "photo_msg": 0,
            "ocr_ok": 0,
            "ocr_fail": 0,
            "gpt_calls": 0,
            "gpt_time_sum": 0.0,
            "tok_prompt": 0,
            "tok_completion": 0,
            "bytes_images_in": 0,
            "subjects": {},
            "langs": {},
        }
        subjects_acc = Counter()
        langs_acc = Counter()

        for uid, st in USERS.items():
            u = {
                "name": st.name,
                "username": st.username,
                "first_seen": st.first_seen,
                "last_seen": st.last_seen,
                "kinds": dict(st.kinds),
                "subjects": dict(st.subjects),
                "langs": dict(st.langs),
                "gpt_calls": st.gpt_calls,
                "gpt_time_sum": st.gpt_time_sum,
                "tok_prompt": st.tok_prompt,
                "tok_completion": st.tok_completion,
                "ocr_ok": st.ocr_ok,
                "ocr_fail": st.ocr_fail,
                "bytes_images_in": st.bytes_images_in,
            }
            snap_users[str(uid)] = u

            totals["users_count"] += 1
            totals["solve_text"] += u["kinds"].get("solve_text", 0)
            totals["solve_photo"] += u["kinds"].get("solve_photo", 0)
            totals["essay"] += u["kinds"].get("essay", 0)
            totals["text_msg"] += u["kinds"].get("text_msg", 0)
            totals["photo_msg"] += u["kinds"].get("photo_msg", 0)
            totals["ocr_ok"] += u["ocr_ok"]
            totals["ocr_fail"] += u["ocr_fail"]
            totals["gpt_calls"] += u["gpt_calls"]
            totals["gpt_time_sum"] += u["gpt_time_sum"]
            totals["tok_prompt"] += u["tok_prompt"]
            totals["tok_completion"] += u["tok_completion"]
            totals["bytes_images_in"] += u["bytes_images_in"]
            subjects_acc.update(u["subjects"])
            langs_acc.update(u["langs"])

        totals["tasks_total"] = totals["solve_text"] + totals["solve_photo"] + totals["essay"]
        totals["subjects"] = dict(subjects_acc)
        totals["langs"] = dict(langs_acc)

        return {
            "generated_at": int(time.time()),
            "users": snap_users,
            "totals": totals,
        }

def stats_save(path: str = METRICS_PATH):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    snap = stats_snapshot()
    data = json.dumps(snap, ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".metrics.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass

def stats_load(path: str = METRICS_PATH):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            snap = json.load(f)
        users = snap.get("users", {})
        with STATS_LOCK:
            for uid_s, u in users.items():
                uid = int(uid_s)
                st = USERS.get(uid) or UserStats(uid)
                st.name = u.get("name") or st.name
                st.username = u.get("username") or st.username
                st.first_seen = u.get("first_seen", st.first_seen)
                st.last_seen = u.get("last_seen", st.last_seen)
                st.kinds = Counter(u.get("kinds", {}))
                st.subjects = Counter(u.get("subjects", {}))
                st.langs = Counter(u.get("langs", {}))
                st.gpt_calls = u.get("gpt_calls", 0)
                st.gpt_time_sum = u.get("gpt_time_sum", 0.0)
                st.tok_prompt = u.get("tok_prompt", 0)
                st.tok_completion = u.get("tok_completion", 0)
                st.ocr_ok = u.get("ocr_ok", 0)
                st.ocr_fail = u.get("ocr_fail", 0)
                st.bytes_images_in = u.get("bytes_images_in", 0)
                USERS[uid] = st
        log.info(f"Loaded metrics from {path} (users={len(USERS)})")
    except Exception as e:
        log.warning(f"stats_load failed: {e}")

def _stats_autosave_loop():
    interval = max(10, METRICS_AUTOSAVE_SEC)
    log.info(f"Metrics autosave: every {interval}s -> {METRICS_PATH}")
    while True:
        try:
            stats_save()
        except Exception as e:
            log.warning(f"stats_save failed: {e}")
        time.sleep(interval)

# ---------- Health-check для Fly ----------
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            if self.path == "/stats.json":
                payload = json.dumps(stats_snapshot(), ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        except Exception as e:
            body = f"error: {e}".encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

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
USER_LANG = defaultdict(lambda: "ru")   # 'ru' | 'be' | 'en' | 'de' | 'fr'

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
ALLOWED_TAGS = {"b", "i", "code", "pre"}  # <a> исключаем, чтобы не ловить Telegram-ошибки
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

# ---------- ВИДИМОЕ ОЖИДАНИЕ («часики») ----------
async def start_spinner(update: Update, context: ContextTypes.DEFAULT_TYPE, label: str = "Обрабатываю…", interval: float = 1.8):
    """
    Показывает «крутилку» как редактируемое сообщение: ⏳/⌛/🕒 + label.
    Возвращает (finish, set_label).
    """
    msg = await update.message.reply_text(f"⏳ {label}")
    stop = asyncio.Event()
    current_label = label

    def set_label(new_label: str):
        nonlocal current_label
        current_label = new_label

    async def worker():
        frames = ["⏳", "⌛", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕", "🕖", "🕗", "🕘", "🕙", "🕚", "🕛"]
        i = 0
        while not stop.is_set():
            i = (i + 1) % len(frames)
            try:
                await msg.edit_text(f"{frames[i]} {current_label}")
            except Exception:
                pass
            await asyncio.sleep(interval)

    task = asyncio.create_task(worker())

    async def finish(final_text: str = None, delete: bool = True):
        stop.set()
        try:
            await task
        except Exception:
            pass
        try:
            if final_text:
                await msg.edit_text(final_text)
            if delete:
                await msg.delete()
        except Exception:
            pass

    return finish, set_label

# ---------- ЯЗЫК ВВОДА ----------
def detect_lang(text: str) -> str:
    """Эвристика: определяем язык задания по символам/диакритике."""
    t = (text or "")
    tl = t.lower()

    # белорусский
    if "ў" in tl or (tl.count("і") >= 2 and tl.count("и") == 0):
        return "be"
    # немецкий
    if any(ch in tl for ch in ("ä", "ö", "ü", "ß")):
        return "de"
    # французский
    if any(ch in tl for ch in ("à","â","ä","ç","é","è","ê","ë","î","ï","ô","ö","ù","û","ü","ÿ","œ")):
        return "fr"
    # латиница без явных фр/нем → англ
    cyr = sum('а' <= ch <= 'я' or 'А' <= ch <= 'Я' or ch in 'ёЁ' for ch in t)
    lat = sum('a' <= ch.lower() <= 'z' for ch in t)
    if lat > cyr * 1.2:
        return "en"
    return "ru"

# ---------- СИСТЕМНЫЙ ПРОМПТ ----------
def sys_prompt(uid: int) -> str:
    subject = USER_SUBJECT[uid]
    grade = USER_GRADE[uid]
    parent = PARENT_MODE[uid]

    base = (
        "Ты — школьный помощник и ИСПОЛНИТЕЛЬ Д/З. "
        "Всегда выполняй задание ПОЛНОСТЬЮ: сначала выдай <b>Ответы</b> по пунктам (заполненные пропуски, готовые формы слов, "
        "числовые ответы, соответствия и т.п.), затем краткое <b>Пояснение</b> ПО-РУССКИ, по шагам. "
        "Если в задании нужно «подчеркнуть/раскрасить/соединить стрелками», отдай текстовое представление (например: "
        "«слово — 1-е склонение [синий]», «соотнести: А→1, Б→3»). "
        "Если на странице несколько заданий — оформи как <b>Задание 1</b>, <b>Задание 2</b>… "
        "Используй ТОЛЬКО HTML-теги <b>, <i>, <code>, <pre>. Без Markdown."
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

# ---------- КЛАССИФИКАЦИЯ ПРЕДМЕТА ----------
async def classify_subject(text: str) -> str:
    """Определяем школьный предмет по тексту задания. Возвращаем одно из SUBJECTS (или 'auto')."""
    try:
        choices = ", ".join(sorted(SUBJECTS - {"auto"}))
        prompt = (
            "К какому школьному предмету относится это задание? "
            f"Выбери ровно ОДНО из списка: {choices}. "
            "Если не очевидно — ответь «auto». "
            "Ответь одним словом из списка, без пояснений.\n\n"
            f"Текст задания:\n{text[:3000]}"
        )
        resp = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Классификатор предметов. Отвечай только одним словом из списка или 'auto'."},
                {"role": "user", "content": prompt}
            ],
            temperature=0,
            max_tokens=10,
        )
        ans = (resp.choices[0].message.content or "").strip().lower()
        mapping = {
            "беларуская мова": "беларуская мова",
            "беларуская літаратура": "беларуская літаратура",
            "русский язык": "русский",
            "литература": "литература",
            "математика": "математика",
            "информатика": "информатика",
            "физика": "физика",
            "химия": "химия",
            "история": "история",
            "обществознание": "обществознание",
            "биология": "биология",
            "география": "география",
            "английский": "английский",
            "auto": "auto",
        }
        for k, v in mapping.items():
            if ans == k:
                return v if v in SUBJECTS else "auto"
        return ans if ans in SUBJECTS else "auto"
    except Exception as e:
        log.warning(f"classify_subject failed: {e}")
        return "auto"

# ---------- OCR: препроцесс и каскад языков ----------
def _preprocess_image(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img)
    img = img.convert("L")
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = ImageEnhance.Sharpness(img).enhance(1.25)
    img = img.filter(ImageFilter.UnsharpMask(radius=2, percent=125, threshold=3))
    max_w = 1800
    if img.width < max_w:
        scale = min(max_w / img.width, 3.0)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img

def _ocr_with_langs(img: Image.Image, langs_list) -> str:
    for langs in langs_list:
        try:
            txt = pytesseract.image_to_string(img, lang=langs, config=TESS_CONFIG)
            if txt and txt.strip():
                log.info(f"OCR success with langs='{langs}': {repr(txt[:80])}")
                return txt.strip()
        except TesseractError as e:
            log.warning(f"OCR langs='{langs}' failed: {e}")
            continue
    return ""

def ocr_image(img: Image.Image) -> str:
    base = ImageOps.exif_transpose(img)
    langs_chain = []
    if TESS_LANGS:
        langs_chain.append(TESS_LANGS)
    for l in ("rus+bel+eng+deu+fra", "rus+eng", "rus", "bel", "deu", "fra", "eng"):
        if l not in langs_chain:
            langs_chain.append(l)

    angles = []
    try:
        osd = pytesseract.image_to_osd(base, config="--psm 0")
        m = re.search(r"(?:Rotate|Orientation in degrees):\s*(\d+)", osd)
        if m:
            angles.append(int(m.group(1)) % 360)
    except TesseractError as e:
        log.warning(f"OSD failed: {e}")

    tried = set()
    for a in angles + [0, 90, 180, 270]:
        a %= 360
        if a in tried:
            continue
        tried.add(a)
        rot = base.rotate(-a, expand=True)
        pimg = _preprocess_image(rot)
        txt = _ocr_with_langs(pimg, langs_chain)
        if txt and txt.strip():
            log.info(f"OCR best_angle={a} len={len(txt)}")
            return txt.strip()

    for a in (0, 90, 180, 270):
        if a in tried:
            continue
        rot = base.rotate(-a, expand=True)
        txt = _ocr_with_langs(rot, ["rus+eng", "rus", "eng", "deu", "fra"])
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
    _get_user_stats(uid, update)  # touch
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
    st = _get_user_stats(uid, update)
    with STATS_LOCK:
        st.subjects[val] += 1  # зафиксируем выбор руками
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
def _answers_hint(task_lang: str) -> str:
    if task_lang == "be":
        return "Пиши <b>Ответы</b> на языке задания (белорусском). <b>Пояснение</b> — по-русски."
    if task_lang == "de":
        return "Write the <b>Answers</b> in German. The <b>Explanation</b> must be in Russian."
    if task_lang == "fr":
        return "Écris les <b>Réponses</b> en français. L’<b>Explication</b> doit être en russe."
    if task_lang == "en":
        return "Write the <b>Answers</b> in English. The <b>Explanation</b> must be in Russian."
    return "Пиши <b>Ответы</b> на русском языке. <b>Пояснение</b> — по-русски."

async def gpt_explain(uid: int, prompt: str, prepend_prompt: bool = True) -> str:
    log.info(f"EXPLAIN/SOLVE uid={uid} subj={USER_SUBJECT[uid]} grade={USER_GRADE[uid]} text={prompt[:80]!r}")
    lang = detect_lang(prompt)
    USER_LANG[uid] = lang

    if USER_SUBJECT[uid] == "auto":
        subj = await classify_subject(prompt)
        if subj in SUBJECTS:
            USER_SUBJECT[uid] = subj
            log.info(f"Subject classified as: {subj}")

    if not prepend_prompt:
        user_content = prompt
    else:
        user_content = (
            "Реши школьное задание ПОЛНОСТЬЮ. "
            "Сначала выдай раздел <b>Ответы</b> — готовые результаты по пунктам (вставленные буквы/окончания, готовые формы слов, "
            "соответствия, числовые ответы и пр.) на ЯЗЫКЕ ЗАДАНИЯ. "
            "Затем выдай раздел <b>Пояснение</b> — кратко, ПО-РУССКИ, по шагам. "
            f"{_answers_hint(lang)} "
            "Если нужно «подчеркнуть/раскрасить/соединить стрелками» — дай текстовое представление. "
            "Если на картинке несколько упражнений — оформи как <b>Задание 1</b>, <b>Задание 2</b>… "
            f"Текст/условие:\n{prompt}"
        )

    messages = [
        {"role": "system", "content": sys_prompt(uid)},
        {"role": "user", "content": user_content}
    ]

    t0 = perf_counter()
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=messages,
        temperature=0.2,
        max_tokens=1000
    )
    dt = perf_counter() - t0

    # учёт метрик
    st = _get_user_stats(uid, None)
    with STATS_LOCK:
        st.gpt_calls += 1
        st.gpt_time_sum += dt
        usage = getattr(resp, "usage", None)
        if usage:
            st.tok_prompt += getattr(usage, "prompt_tokens", 0) or 0
            st.tok_completion += getattr(usage, "completion_tokens", 0) or 0
        st.subjects[USER_SUBJECT[uid]] += 1
        st.langs[lang] += 1

    return (resp.choices[0].message.content or "").strip()

async def gpt_essay(uid: int, topic: str) -> str:
    log.info(f"ESSAY uid={uid} topic={topic[:80]!r}")
    USER_LANG[uid] = detect_lang(topic)
    t0 = perf_counter()
    resp = await client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": sys_prompt(uid)},
            {"role": "user", "content": f"Напиши сочинение. Тема: {topic}"}
        ],
        temperature=0.7,
        max_tokens=1200
    )
    dt = perf_counter() - t0
    st = _get_user_stats(uid, None)
    with STATS_LOCK:
        st.gpt_calls += 1
        st.gpt_time_sum += dt
        usage = getattr(resp, "usage", None)
        if usage:
            st.tok_prompt += getattr(usage, "prompt_tokens", 0) or 0
            st.tok_completion += getattr(usage, "completion_tokens", 0) or 0
        st.kinds["essay"] += 1
    return (resp.choices[0].message.content or "").strip()

# ---------- Обработчики ----------
async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _get_user_stats(uid, update)
    text = " ".join(context.args).strip()
    if not text:
        USER_STATE[uid] = "AWAIT_EXPLAIN"
        return await update.message.reply_text("🧠 Что объяснить/решить? Напиши одной фразой.", reply_markup=kb(uid))
    spinner_finish, spinner_set = await start_spinner(update, context, "Думаю над решением…")
    try:
        with STATS_LOCK:
            st.kinds["text_msg"] += 1
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        spinner_set("Определяю предмет…")
        if USER_SUBJECT[uid] == "auto":
            subj = await classify_subject(text)
            if subj in SUBJECTS:
                USER_SUBJECT[uid] = subj
        spinner_set("Решаю задачу…")
        out = await gpt_explain(uid, text)
        with STATS_LOCK:
            st.kinds["solve_text"] += 1
        await safe_reply_html(update.message, out, reply_markup=kb(uid))
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Нужно что-то уточнить по решению?", reply_markup=keyboard)
        USER_STATE[uid] = "AWAIT_FOLLOWUP"
    except Exception as e:
        log.exception("explain")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))
    finally:
        await spinner_finish()

async def essay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _get_user_stats(uid, update)
    topic = " ".join(context.args).strip()
    if not topic:
        USER_STATE[uid] = "AWAIT_ESSAY"
        return await update.message.reply_text("📝 Тема сочинения?", reply_markup=kb(uid))
    spinner_finish, spinner_set = await start_spinner(update, context, "Готовлю сочинение…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        spinner_set("Пишу основной текст…")
        essay = await gpt_essay(uid, topic)
        await safe_reply_html(update.message, essay, reply_markup=kb(uid))

        spinner_set("Делаю план…")
        plan_prompt = (
            f"Составь нумерованный план сочинения на тему '{topic}'. "
            "Каждый пункт короткий. Используй только HTML-теги <b>, <i>, <code>, <pre>."
        )
        plan = await gpt_explain(uid, plan_prompt, prepend_prompt=False)
        await safe_reply_html(update.message, plan, reply_markup=kb(uid))

        spinner_set("Поясняю логику…")
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
    finally:
        await spinner_finish()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _get_user_stats(uid, update)
    spinner_finish, spinner_set = await start_spinner(update, context, "Обрабатываю фото…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)

        # Берём картинку как из photo, так и из document (если это image/*)
        tg_file = None
        if update.message.photo:
            tg_file = await update.message.photo[-1].get_file()
        elif update.message.document and str(update.message.document.mime_type or "").startswith("image/"):
            tg_file = await update.message.document.get_file()
        else:
            raise ValueError("No image provided")

        data = await tg_file.download_as_bytearray()
        size_guess = getattr(tg_file, "file_size", None) or len(data)
        with STATS_LOCK:
            st.kinds["photo_msg"] += 1
            st.bytes_images_in += int(size_guess)

        img = Image.open(io.BytesIO(data))

        spinner_set("Распознаю текст на фото…")
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        ocr_text = ocr_image(img)
        log.info(f"OCR uid={uid} text={ocr_text!r}")

        if not ocr_text or not ocr_text.strip():
            with STATS_LOCK:
                st.ocr_fail += 1
            raise ValueError("OCR returned empty text")

        with STATS_LOCK:
            st.ocr_ok += 1

        USER_LANG[uid] = detect_lang(ocr_text)

        if USER_SUBJECT[uid] == "auto":
            spinner_set("Определяю предмет…")
            subj = await classify_subject(ocr_text)
            if subj in SUBJECTS:
                USER_SUBJECT[uid] = subj
                log.info(f"Subject from photo: {subj}")

        spinner_set("Решаю задание…")
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await gpt_explain(uid, ocr_text[:4000])
        with STATS_LOCK:
            st.kinds["solve_photo"] += 1
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
    finally:
        await spinner_finish()

# ---------- Текст и кнопки ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _get_user_stats(uid, update)
    raw_text = (update.message.text or "").strip()
    text = raw_text.lower()
    state = USER_STATE[uid]

    with STATS_LOCK:
        st.kinds["text_msg"] += 1

    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    if raw_text:
        USER_LANG[uid] = detect_lang(raw_text)

    if state == "AWAIT_TEXT_OR_PHOTO_CHOICE":
        if text == "📸 решить по фото":
            USER_STATE[uid] = None
            return await update.message.reply_text("Хорошо! Пришли фото задания.", reply_markup=kb(uid))
        elif text == "✍️ напишу текстом":
            USER_STATE[uid] = "AWAIT_EXPLAIN"
            return await update.message.reply_text("Напиши задание текстом — я всё сделаю.", reply_markup=kb(uid))
        else:
            return await update.message.reply_text("Выбери: 'Решить по фото' или 'Напишу текстом'")

    if state == "AWAIT_FOLLOWUP":
        if text == "да":
            USER_STATE[uid] = "AWAIT_EXPLAIN"
            return await update.message.reply_text("Что именно непонятно?", reply_markup=kb(uid))
        elif text == "нет":
            USER_STATE[uid] = None
            return await update.message.reply_text("Ок! Если что — пиши снова.", reply_markup=kb(uid))
        else:
            return await update.message.reply_text("Ответь: Да или Нет")

    if text == "🧠 объяснить":
        return await explain_cmd(update, context)
    if text == "📝 сочинение":
        return await essay_cmd(update, context)
    if text == "📸 фото задания":
        return await update.message.reply_text(
            "Отправь фото сообщением — я распознаю и решу.",
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

    if state == "AWAIT_EXPLAIN":
        USER_STATE[uid] = None
        context.args = [raw_text]
        return await explain_cmd(update, context)
    if state == "AWAIT_ESSAY":
        USER_STATE[uid] = None
        context.args = [raw_text]
        return await essay_cmd(update, context)

    context.args = [raw_text]
    return await explain_cmd(update, context)

# ---------- MAIN ----------
def main():
    # загрузим накопленные метрики, если файл уже был
    try:
        stats_load()
    except Exception as e:
        log.warning(f"stats_load on boot failed: {e}")

    # health + автосейв
    threading.Thread(target=_run_health, daemon=True).start()
    threading.Thread(target=_stats_autosave_loop, daemon=True).start()

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

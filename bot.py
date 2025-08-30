# bot.py — R2 (очищено): 7 дней Pro, далее Free (4o-mini); bePaid (карта РБ/ЕРИП); VDB+Admin; OCR; метрики
# Регион: BY. Оплаты: Telegram Stars + bePaid.
# Важное: никаких "1-Pro в день" после триала, Pro только 7 дней новым.
# Исправления: about_cmd (кавычки/многострочник), /vdb/search (таймаут/валидация), убраны дубли _Health/_run_health/vdbtest-хвост.

import os, io, re, html, json, time, sqlite3, tempfile, logging, threading, asyncio
from time import perf_counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, Counter
from typing import Optional

# ---------- ЛОГИ ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
log = logging.getLogger("gotovo-bot")

# ---------- ENV / конфиг ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

TELEGRAM_STARS_ENABLED = os.getenv("TELEGRAM_STARS_ENABLED", "true").lower() == "true"
TELEGRAM_PROVIDER_TOKEN = os.getenv("TELEGRAM_PROVIDER_TOKEN", "")  # для Stars (XTR)

# bePaid (единая витрина: карта РБ/ЕРИП внутри)
BEPAID_CHECKOUT_URL = os.getenv("BEPAID_CHECKOUT_URL", "")
BEPAID_WEBHOOK_SECRET = os.getenv("BEPAID_WEBHOOK_SECRET", "")

# ВБД-хук (админский sanity-тест)
VDB_WEBHOOK_SECRET = os.getenv("VDB_WEBHOOK_SECRET", "")

# Диски / БД / Метрики
DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
METRICS_PATH = os.path.join(DATA_DIR, "metrics.json")
METRICS_AUTOSAVE_SEC = int(os.getenv("METRICS_AUTOSAVE_SEC", "60"))

# ---------- Безопасные импорты RAG / Формулы ----------
try:
    from rag_vdb import search_rules as _search_rules, clamp_words as _clamp_words  # type: ignore
except Exception as e:
    log.warning(f"RAG not available, using fallbacks: {e}")
    async def _search_rules(client, query, subject_key, grade, top_k=5): return []
    def _clamp_words(s: str, n: int) -> str: return " ".join((s or "").split()[:max(1, n)])
search_rules = _search_rules
clamp_words = _clamp_words

try:
    from services.formulas import postprocess_formulas as _ppf, extract_tex_snippets as _ets, render_tex_png as _rtp  # type: ignore
    try:
        from config import RENDER_TEX as _RENDER_TEX  # type: ignore
    except Exception:
        _RENDER_TEX = False
except Exception as e:
    log.info(f"Formulas/TEX module not available, using no-op: {e}")
    def _ppf(text: str) -> str: return text
    def _ets(text: str): return []
    def _rtp(tex: str): raise RuntimeError("TEX renderer not available")
    _RENDER_TEX = False
postprocess_formulas = _ppf
extract_tex_snippets = _ets
render_tex_png = _rtp
RENDER_TEX = _RENDER_TEX

# ---------- Telegram ----------
from telegram import (
    Update, BotCommand, ReplyKeyboardMarkup, InlineKeyboardMarkup, InlineKeyboardButton,
    LabeledPrice, PreCheckoutQuery, Message
)
from telegram.constants import ChatAction
from telegram.error import BadRequest
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, CallbackQueryHandler,
    PreCheckoutQueryHandler, filters as f
)

# ---------- OpenAI ----------
from openai import AsyncOpenAI
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "30"))  # сек
client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT, max_retries=2)

# ---------- OCR ----------
from PIL import Image, ImageOps, ImageFilter, ImageEnhance
import pytesseract
from pytesseract import TesseractError
TESS_LANGS_DEFAULT = "rus+bel+eng+deu+fra"
TESS_LANGS = os.getenv("TESS_LANGS", TESS_LANGS_DEFAULT)
TESS_CONFIG = os.getenv("TESS_CONFIG", "--oem 3 --psm 6 -c preserve_interword_spaces=1")

# ---------- Guard secrets ----------
if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (fly secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (fly secrets set OPENAI_API_KEY=...)")

# ---------- ПАМЯТЬ (RAM) ----------
SUBJECTS = {
    "математика","русский","английский","физика","химия","история","обществознание","биология",
    "информатика","география","литература","auto","беларуская мова","беларуская літаратура",
}
SUBJECT_VDB_KEY = {
    "математика":"math","физика":"physics","химия":"chemistry","биология":"biology","информатика":"informatics",
    "география":"geography","русский":"russian","литература":"literature","английский":"english",
    "обществознание":"social_studies","история":"history","беларуская мова":"bel_mova","беларуская літаратура":"bel_lit",
}
def subject_to_vdb_key(s: str) -> str:
    s = (s or "").strip().lower()
    return SUBJECT_VDB_KEY.get(s, s)

USER_SUBJECT = defaultdict(lambda: "auto")
USER_GRADE = defaultdict(lambda: "8")
PARENT_MODE = defaultdict(lambda: True)
USER_STATE = defaultdict(lambda: None)
USER_LANG = defaultdict(lambda: "ru")
PRO_NEXT = defaultdict(lambda: False)

# ---------- Follow-up контекст в локальной БД ----------
def _db_followup():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS followup_state(
        user_id INTEGER PRIMARY KEY,
        last_task TEXT,
        last_answer TEXT,
        ts INTEGER,
        used_free INTEGER
    )"""
    )
    return conn

FOLLOWUP_FREE_WINDOW_SEC = 15 * 60

def set_followup_context(uid: int, task_text: str, answer_text: str):
    snippet_task = (task_text or "").strip()[:1200]
    snippet_ans = (answer_text or "").strip()[:1200]
    now = int(time.time())
    with _db_followup() as db:
        db.execute(
            """INSERT INTO followup_state(user_id,last_task,last_answer,ts,used_free)
               VALUES(?,?,?,?,0)
               ON CONFLICT(user_id) DO UPDATE SET last_task=?, last_answer=?, ts=?, used_free=0
            """,
            (uid, snippet_task, snippet_ans, now, snippet_task, snippet_ans, now),
        )

def get_followup_context(uid: int) -> Optional[dict]:
    with _db_followup() as db:
        row = db.execute(
            "SELECT last_task,last_answer,ts,used_free FROM followup_state WHERE user_id=?",
            (uid,),
        ).fetchone()
    if not row:
        return None
    return {"task": row[0] or "", "answer": row[1] or "", "ts": row[2] or 0, "used_free": bool(row[3])}

def mark_followup_used(uid: int):
    with _db_followup() as db:
        db.execute("UPDATE followup_state SET used_free=1 WHERE user_id=?", (uid,))

def in_free_window(ctx: dict | None) -> bool:
    return bool(ctx) and (int(time.time()) - int(ctx.get("ts", 0)) <= FOLLOWUP_FREE_WINDOW_SEC)

# ---------- Клавиатура ----------
def kb(uid: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🧠 Объяснить", "📝 Сочинение", "⭐ Pro (след. запрос)"],
            [
                f"📚 Предмет: {USER_SUBJECT[uid]}",
                f"🎓 Класс: {USER_GRADE[uid]}",
                f"👨‍👩‍👧 Родит.: {'вкл' if PARENT_MODE[uid] else 'выкл'}",
            ],
            ["ℹ️ Free vs Pro", "💳 Купить", "🧾 Моя статистика"],
        ],
        resize_keyboard=True,
    )

# ---------- Безопасный HTML ----------
ALLOWED_TAGS = {"b", "i", "code", "pre"}
_TAG_OPEN = {t: f"&lt;{t}&gt;" for t in ALLOWED_TAGS}
_TAG_CLOSE = {t: f"&lt;/{t}&gt;" for t in ALLOWED_TAGS}
def sanitize_html(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\x00", "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.I)
    esc = html.escape(text, quote=False)
    for t in ALLOWED_TAGS:
        esc = re.sub(fr"{_TAG_OPEN[t]}(.*?){_TAG_CLOSE[t]}", fr"<{t}>\1</{t}>", esc, flags=re.I | re.S)
    return esc[:4000]

async def safe_reply_html(message: Message, text: str, **kwargs):
    try:
        return await message.reply_text(sanitize_html(text), parse_mode="HTML", disable_web_page_preview=True, **kwargs)
    except BadRequest as e:
        if "Can't parse entities" in str(e):
            return await message.reply_text(html.escape(text)[:4000], disable_web_page_preview=True, **kwargs)
        raise

# ---------- Спиннер ----------
async def start_spinner(update: Update, context: ContextTypes.DEFAULT_TYPE, label="Обрабатываю…", interval=1.6):
    msg = await update.message.reply_text(f"⏳ {label}")
    stop = asyncio.Event()
    current_label = label
    def set_label(s: str):
        nonlocal current_label
        current_label = s
    async def worker():
        frames = ["⏳","⌛","🕐","🕑","🕒","🕓","🕔","🕕","🕖","🕗","🕘","🕙","🕚","🕛"]
        i = 0
        while not stop.is_set():
            i = (i + 1) % len(frames)
            try: await msg.edit_text(f"{frames[i]} {current_label}")
            except Exception: pass
            await asyncio.sleep(interval)
    task = asyncio.create_task(worker())
    async def finish(final_text: str = None, delete: bool = True):
        stop.set()
        try: await task
        except Exception: pass
        try:
            if final_text: await msg.edit_text(final_text)
            if delete: await msg.delete()
        except Exception: pass
    return finish, set_label

# ---------- Детект языка ----------
def detect_lang(text: str) -> str:
    tl = (text or "").lower()
    if "ў" in tl or (tl.count("і") >= 2 and tl.count("и") == 0): return "be"
    if any(ch in tl for ch in ("ä","ö","ü","ß")): return "de"
    if any(ch in tl for ch in ("à","â","ä","ç","é","è","ê","ë","î","ï","ô","ö","ù","û","ü","ÿ","œ")): return "fr"
    cyr = sum("а" <= ch <= "я" or "А" <= ch <= "Я" or ch in "ёЁ" for ch in tl)
    lat = sum("a" <= ch <= "z" for ch in tl)
    if lat > cyr * 1.2: return "en"
    return "ru"

# ---------- Системный промпт: «разжеванный» ----------
def sys_prompt(uid: int) -> str:
    subject = USER_SUBJECT[uid]; grade = USER_GRADE[uid]; parent = PARENT_MODE[uid]
    base = (
        "Ты — школьный помощник и ИСПОЛНИТЕЛЬ. Сначала выдай <b>Ответы</b> (готовый результат по пунктам), "
        "затем — <b>Подробное Пояснение</b> на простом русском, будто объясняешь «двоечнику». "
        "Требования к Пояснению: 1) Переформулируй условие одним предложением; "
        "2) Объясни, ЗАЧЕМ каждый шаг; 3) Дай решение микро-шагами (1 мысль = 1 строка); "
        "4) Отметь типичные ошибки; 5) Дай самопроверку (критерии/подстановку); "
        "6) Покажи короткий путь, если он есть; 7) Никакой воды, только по делу. "
        "Если материалов ВБД нет — решай по предметным знаниям. Разрешённые HTML-теги: <b>, <i>, <code>, <pre>."
    )
    form_hint = "Ключевые формулы оформи в <pre>. Если уместно — вставь TeX (например: \\int_0^1 x^2\\,dx)."
    sub = f"Предмет: {subject}." if subject != "auto" else "Определи предмет сам."
    grd = f"Класс: {grade}."
    par = ("<b>Памятка для родителей:</b> что спросить у ребёнка; на что смотреть; мини-тренировка (2–3 пункта)."
           if parent else "")
    return f"{base} {form_hint} {sub} {grd} {par}"

def _preprocess_image(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("L")
    img = ImageOps.autocontrast(img)
    img = img.filter(ImageFilter.MedianFilter(size=3))
    img = ImageEnhance.Sharpness(img).enhance(1.2)
    max_w = 1800
    if img.width < max_w:
        scale = min(max_w / img.width, 3.0)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img

def ocr_image(img: Image.Image) -> str:
    base = ImageOps.exif_transpose(img)
    langs_chain = ([TESS_LANGS, "rus+bel+eng", "rus+eng", "rus", "bel", "eng", "deu", "fra"] if TESS_LANGS
                   else ["rus+bel+eng", "rus", "eng"])
    tried = set()
    for angle in [0, 90, 180, 270]:
        if angle in tried: continue
        tried.add(angle)
        rot = base.rotate(-angle, expand=True)
        p = _preprocess_image(rot)
        for langs in langs_chain:
            try:
                txt = pytesseract.image_to_string(p, lang=langs, config=TESS_CONFIG)
                if txt and txt.strip(): return txt.strip()
            except TesseractError:
                continue
    return ""

# ---------- Гибридный роутер моделей ----------
HEAVY_MARKERS = ("докажи","обоснуй","подробно","по шагам","поиндукции","уравнение","система",
                 "дробь","производная","интеграл","доказать","программа","алгоритм","код")

def _local_select_model(prompt: str, mode: str) -> tuple[str, int, str]:
    p = (prompt or "").lower()
    if mode == "free":  # после 7 дней
        return "gpt-4o-mini", 700, "4o-mini"
    if mode == "pro":   # первые 7 дней, подписка или кредиты
        long_input = len(p) > 600
        heavy = long_input or any(k in p for k in HEAVY_MARKERS)
        if heavy: return "o4-mini", 1100, "o4-mini"
        return "gpt-4o-mini", 900, "4o-mini"
    return "gpt-4o-mini", 700, "4o-mini"

select_model = _local_select_model  # может быть переопределён сервисом

async def call_model(uid: int, user_text: str, mode: str) -> str:
    lang = detect_lang(user_text)
    USER_LANG[uid] = lang
    model, max_out, tag = select_model(user_text, mode)
    sys = sys_prompt(uid)

    # ----- ВБД (RAG) -----
    vdb_hints = []
    try:
        subj_key = subject_to_vdb_key(USER_SUBJECT[uid])
        grade_int = int(USER_GRADE[uid]) if str(USER_GRADE[uid]).isdigit() else 8
        query_for_vdb = clamp_words(user_text, 40)

        async def _srch(skey): return await search_rules(client, query_for_vdb, skey, grade_int)
        try:
            rules = await asyncio.wait_for(_srch(subj_key), timeout=3.0)
        except Exception as e:
            log.warning(f"VDB primary timeout/fail: {e}"); rules = []
        if not rules and subj_key != USER_SUBJECT[uid]:
            try:
                rules = await asyncio.wait_for(_srch(USER_SUBJECT[uid]), timeout=3.0)
            except Exception as e:
                log.warning(f"VDB fallback timeout/fail: {e}"); rules = []
        for r in (rules or [])[:5]:
            brief = (r.get("rule_brief") if isinstance(r, dict) else str(r)) or ""
            brief = clamp_words(brief, 120)
            if brief: vdb_hints.append(f"• {brief}")
    except Exception as e:
        log.warning(f"VDB block error: {e}")

    vdb_context = ("\n\n[ВБД-памятка: используй только как справку, без ссылок на книги]\n" + "\n".join(vdb_hints)) if vdb_hints else ""

    content = (
        "Реши задание. Сначала <b>Ответы</b>, затем <b>Пояснение</b> простым русским. "
        f"Текст/условие:\n{user_text}" + vdb_context
    )

    # ----- LLM-вызов -----
    t0 = perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": content}],
            temperature=0.25 if mode in {"free"} else 0.3,
            max_tokens=max_out,
        )
        out_text = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("LLM error")
        out_text = "❌ Не получилось получить ответ от модели. Попробуй ещё раз."
    dt = perf_counter() - t0
    log.info(f"LLM model={model} tag={tag} mode={mode} dt={dt:.2f}s")

    # метрики
    try:
        st = _get_user_stats(uid)
        st.gpt_calls += 1
        st.gpt_time_sum += float(dt)
    except Exception:
        pass

    return out_text

async def call_model_followup(uid: int, prev_task: str, prev_answer: str, follow_q: str, mode_tag: str) -> str:
    sys = sys_prompt(uid)
    prompt = (
        "Коротко и по делу дополни/уточни предыдущее решение.\n\n"
        f"Исходное задание:\n{prev_task[:2000]}\n\n"
        f"Фрагменты ответа:\n{prev_answer[:2000]}\n\n"
        f"Вопрос-уточнение:\n{follow_q[:1200]}\n\n"
        "Дай ТОЛЬКО дополнение, без переписывания."
    )
    model, max_out, tag = select_model(prev_task + " " + follow_q, mode_tag)
    t0 = perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": prompt}],
            temperature=0.25 if mode_tag in {"free"} else 0.3,
            max_tokens=min(600, max_out),
        )
        out = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("LLM followup error")
        out = "❌ Не удалось получить уточнение. Попробуй ещё раз."
    dt = perf_counter() - t0
    log.info(f"LLM followup model={model} tag={tag} mode={mode_tag} dt={dt:.2f}s")
    try:
        st = _get_user_stats(uid); st.gpt_calls += 1; st.gpt_time_sum += float(dt)
    except Exception:
        pass
    return out

# ---------- Формулы ----------
async def reply_with_formulas(message: Message, raw_text: str, reply_markup=None):
    text = postprocess_formulas(raw_text or "")
    await safe_reply_html(message, text, reply_markup=reply_markup)
    if RENDER_TEX:
        try:
            for tex in extract_tex_snippets(text)[:4]:
                png = render_tex_png(tex)
                await message.reply_photo(png, caption="Формула")
        except Exception as e:
            log.warning(f"TEX render fail: {e}")

# ---------- Внутренние метрики ----------
STATS_LOCK = threading.RLock()

class UserStats:
    __slots__ = ("uid","name","username","first_seen","last_seen","kinds","subjects","langs","gpt_calls","gpt_time_sum",
                 "tok_prompt","tok_completion","ocr_ok","ocr_fail","bytes_images_in")
    def __init__(self, uid: int):
        now = time.time()
        self.uid = uid; self.name=""; self.username=""
        self.first_seen = now; self.last_seen = now
        self.kinds = Counter(); self.subjects = Counter(); self.langs = Counter()
        self.gpt_calls = 0; self.gpt_time_sum = 0.0
        self.tok_prompt = 0; self.tok_completion = 0
        self.ocr_ok = 0; self.ocr_fail = 0; self.bytes_images_in = 0

USERS: dict[int, UserStats] = {}

def _get_user_stats(uid: int, update: Update | None = None) -> UserStats:
    with STATS_LOCK:
        st = USERS.get(uid) or UserStats(uid)
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
            "users_count": 0,"tasks_total": 0,"solve_text": 0,"solve_photo": 0,"essay": 0,"text_msg": 0,"photo_msg": 0,
            "ocr_ok": 0,"ocr_fail": 0,"gpt_calls": 0,"gpt_time_sum": 0.0,"tok_prompt": 0,"tok_completion": 0,
            "bytes_images_in": 0,"subjects": {},"langs": {},
        }
        subjects_acc = Counter(); langs_acc = Counter()
        for uid, st in USERS.items():
            u = {
                "name": st.name, "username": st.username,
                "first_seen": st.first_seen, "last_seen": st.last_seen,
                "kinds": dict(st.kinds), "subjects": dict(st.subjects), "langs": dict(st.langs),
                "gpt_calls": st.gpt_calls, "gpt_time_sum": st.gpt_time_sum,
                "tok_prompt": st.tok_prompt, "tok_completion": st.tok_completion,
                "ocr_ok": st.ocr_ok, "ocr_fail": st.ocr_fail, "bytes_images_in": st.bytes_images_in,
            }
            snap_users[str(uid)] = u
            totals["users_count"] += 1
            totals["solve_text"] += u["kinds"].get("solve_text", 0)
            totals["solve_photo"] += u["kinds"].get("solve_photo", 0)
            totals["essay"] += u["kinds"].get("essay", 0)
            totals["ocr_ok"] += u["ocr_ok"]; totals["ocr_fail"] += u["ocr_fail"]
            totals["gpt_calls"] += u["gpt_calls"]; totals["gpt_time_sum"] += u["gpt_time_sum"]
            totals["tok_prompt"] += u["tok_prompt"]; totals["tok_completion"] += u["tok_completion"]
            totals["bytes_images_in"] += u["bytes_images_in"]
            subjects_acc.update(u["subjects"]); langs_acc.update(u["langs"])
        totals["tasks_total"] = totals["solve_text"] + totals["solve_photo"] + totals["essay"]
        totals["subjects"] = dict(subjects_acc); totals["langs"] = dict(langs_acc)
        return {"generated_at": int(time.time()), "users": snap_users, "totals": totals}

def stats_save():
    os.makedirs(os.path.dirname(METRICS_PATH), exist_ok=True)
    data = json.dumps(stats_snapshot(), ensure_ascii=False, indent=2).encode("utf-8")
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(METRICS_PATH), prefix=".metrics.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f: f.write(data)
        os.replace(tmp, METRICS_PATH)
    finally:
        if os.path.exists(tmp):
            try: os.remove(tmp)
            except Exception: pass

def stats_load():
    if not os.path.exists(METRICS_PATH): return
    try:
        snap = json.load(open(METRICS_PATH, "r", encoding="utf-8"))
        users = snap.get("users", {})
        with STATS_LOCK:
            for uid_s, u in users.items():
                uid = int(uid_s)
                st = USERS.get(uid) or UserStats(uid)
                st.name = u.get("name") or st.name; st.username = u.get("username") or st.username
                st.first_seen = u.get("first_seen", st.first_seen); st.last_seen = u.get("last_seen", st.last_seen)
                st.kinds = Counter(u.get("kinds", {})); st.subjects = Counter(u.get("subjects", {})); st.langs = Counter(u.get("langs", {}))
                st.gpt_calls = u.get("gpt_calls", 0); st.gpt_time_sum = u.get("gpt_time_sum", 0.0)
                st.tok_prompt = u.get("tok_prompt", 0); st.tok_completion = u.get("tok_completion", 0)
                st.ocr_ok = u.get("ocr_ok", 0); st.ocr_fail = u.get("ocr_fail", 0)
                st.bytes_images_in = u.get("bytes_images_in", 0)
                USERS[uid] = st
        log.info(f"Loaded metrics (users={len(USERS)})")
    except Exception as e:
        log.warning(f"stats_load failed: {e}")

def _stats_autosave_loop():
    interval = max(10, METRICS_AUTOSAVE_SEC)
    log.info(f"Metrics autosave every {interval}s -> {METRICS_PATH}")
    while True:
        try: stats_save()
        except Exception as e: log.warning(f"stats_save failed: {e}")
        time.sleep(interval)

# ---------- Админы ----------
def _db_admins():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""CREATE TABLE IF NOT EXISTS admin_users(user_id INTEGER PRIMARY KEY, added_ts INTEGER)""")
    return conn
def _env_admin_ids() -> set[int]:
    return {int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()}
def _load_admins_from_db() -> set[int]:
    with _db_admins() as db:
        rows = db.execute("SELECT user_id FROM admin_users").fetchall()
    return {int(r[0]) for r in rows}
def all_admin_ids() -> set[int]:
    return _env_admin_ids() | _load_admins_from_db()
def is_admin(uid: int) -> bool:
    return uid in all_admin_ids()
def add_admin(uid: int) -> bool:
    if not isinstance(uid, int) or uid <= 0: return False
    with _db_admins() as db:
        db.execute("INSERT OR IGNORE INTO admin_users(user_id, added_ts) VALUES(?,?)", (uid, int(time.time())))
    return True
def del_admin(uid: int) -> bool:
    with _db_admins() as db: db.execute("DELETE FROM admin_users WHERE user_id=?", (uid,))
    return True

# ---------- Монетизация (stubs) ----------
# Новые правила:
# - Первые 7 дней с момента first_seen — Pro бесплатно (sub_active=True).
# - После 7 дней: только Free (3/день текст), никакого Trial Pro.
_DAY = lambda: int(time.time() // 86400)
_COUNTS = defaultdict(lambda: {"day": _DAY(), "free": 0})
def _roll(uid: int):
    d = _COUNTS[uid]
    if d["day"] != _DAY():
        d["day"] = _DAY(); d["free"] = 0

def _is_new_user_pro(uid: int) -> bool:
    st = _get_user_stats(uid)
    return (time.time() - st.first_seen) < 7 * 24 * 3600

def get_user_plan(uid: int):
    _roll(uid)
    sub_active = _is_new_user_pro(uid)
    left_free = max(0, 3 - _COUNTS[uid]["free"])
    return {
        "free_left_today": left_free,
        "sub_active": sub_active,
        "sub_left_month": 0,   # если подключим реальную подписку — заполним
        "credits": 0
    }

def consume_request(uid: int, need_pro: bool, allow_trial: bool = False):
    _roll(uid)
    if need_pro:
        if _is_new_user_pro(uid):  # 7 дней Pro
            return True, "pro", ""
        # иначе нужен платёж: подписка/кредиты (в стабе их нет)
        return False, "free", "нужен Pro (подписка/кредиты)"
    # free-запрос
    if _COUNTS[uid]["free"] < 3:
        _COUNTS[uid]["free"] += 1
        return True, "free", ""
    return False, "free", "исчерпан дневной лимит Free"

def add_credits(uid: int, cnt: int): return 0
def activate_sub(uid: int, months: int): return False

def build_buy_keyboard(stars_enabled: bool, bepaid_url: str | None):
    rows = []
    if stars_enabled:
        rows.append([InlineKeyboardButton("⭐ Telegram Stars", callback_data="buy_stars:CREDITS_50")])
    if bepaid_url:
        rows.append([InlineKeyboardButton("💳 bePaid (карта РБ / ЕРИП)", url=bepaid_url)])
    if not rows:
        rows = [[InlineKeyboardButton("Скоро доступна оплата", callback_data="noop")]]
    return InlineKeyboardMarkup(rows)

def apply_payment_payload(uid: int, kind: str) -> str:
    return f"✅ Платёж принят: {kind}. Баланс обновлён (демо)."

def _stars_amount(payload: str) -> int:
    defaults = {
        "CREDITS_50": int(os.getenv("PRICE_CREDITS_50_XTR", "60")),
        "CREDITS_200": int(os.getenv("PRICE_CREDITS_200_XTR", "220")),
        "CREDITS_1000": int(os.getenv("PRICE_CREDITS_1000_XTR", "990")),
        "SUB_MONTH": int(os.getenv("PRICE_SUB_MONTH_XTR", "490")),
    }
    return defaults.get(payload, 100)

# ---------- Команды / меню ----------
async def set_commands(app: Application):
    await app.bot.set_my_commands(
        [
            BotCommand("start", "Запуск"),
            BotCommand("menu", "Меню"),
            BotCommand("help", "Помощь"),
            BotCommand("about", "О боте"),
            BotCommand("subject", "Предмет (или auto)"),
            BotCommand("grade", "Класс 5–11"),
            BotCommand("parent", "Режим родителей on/off"),
            BotCommand("mystats", "Моя статистика"),
            BotCommand("stats", "Статистика (админ)"),
            BotCommand("buy", "Купить Pro/кредиты"),
            BotCommand("explain", "Объяснить: /explain ТЕКСТ"),
            BotCommand("essay", "Сочинение: /essay ТЕМА"),
            BotCommand("vdbtest", "Проверить поиск в ВБД (админ)"),
            BotCommand("whoami", "Показать мой Telegram ID"),
            BotCommand("admin", "Админ-панель"),
            BotCommand("admins", "Список админов"),
        ]
    )

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Твой Telegram ID: <code>{uid}</code>", parse_mode="HTML", reply_markup=kb(uid))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    st = _get_user_stats(uid, update)
    if _is_new_user_pro(uid):
        banner = (
            "👋 Привет! Я — <b>Готово!</b>\n"
            "🎁 <b>7 дней Pro бесплатно</b> для новых пользователей: сложные задачи, фото/сканы, приоритет.\n"
            "После — бесплатный простой режим (текст, GPT-4o-mini).\n\n"
            "Пиши задание или жми кнопки ниже."
        )
    else:
        banner = (
            "👋 Привет! Я — <b>Готово!</b>\n"
            "Сейчас действует бесплатный простой режим (3 текста/день на GPT-4o-mini).\n"
            "Хочешь Pro (сложные задачи, фото) — оформи оплату.\n\n"
            "Пиши задание или жми кнопки ниже."
        )
    await safe_reply_html(update.message, banner, reply_markup=kb(uid))

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id if update and update.effective_user else 0
    txt = """<b>📘 О боте «Готово!»</b>
• 5–11 классы: решаю задачи и объясняю «по-людски». Сначала <b>Ответы</b>, потом <b>Пояснение</b> шагами.
• <b>Родителям</b>: памятка — что спросить у ребёнка, на что смотреть, мини-тренировка.
• Формулы/чертежи: аккуратно, где можно — LaTeX. Фото заданий — в Pro.
• Модели: Free — GPT-4o-mini; Pro — o4-mini/4o по необходимости.
• Оплата: Telegram Stars + <b>bePaid</b> (карта РБ, ЕРИП).

<b>Новые пользователи</b>: 7 дней Pro бесплатно. Затем — бесплатный простой режим (GPT-4o-mini).
Чтобы начать — пришли задание текстом или фото. Если «первый класс как институт 😂» — разложу на <i>микро-шаги</i>."""
    await safe_reply_html(update.message, txt, reply_markup=kb(uid))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await about_cmd(update, context)

async def subject_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        return await update.message.reply_text("Доступно: " + ", ".join(sorted(SUBJECTS)), reply_markup=kb(uid))
    val = " ".join(context.args).strip().lower()
    if val not in SUBJECTS:
        return await update.message.reply_text(
            "Не понял предмет. Доступно: " + ", ".join(sorted(SUBJECTS)), reply_markup=kb(uid)
        )
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
    PARENT_MODE[uid] = context.args[0].lower() == "on"
    await update.message.reply_text(
        f"Режим для родителей: {'вкл' if PARENT_MODE[uid] else 'выкл'}", reply_markup=kb(uid)
    )

async def free_vs_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    plan = get_user_plan(uid)
    msg = (
        "🔹 <b>Free</b>: 3 текстовых запроса/день (GPT-4o-mini).\n"
        "🔸 <b>Pro</b>: сложные задачи, фото/сканы, приоритет; модели o4-mini/4o по необходимости.\n"
        f"🎁 <b>Статус</b>: {'Первые 7 дней — Pro бесплатно' if plan['sub_active'] else 'Сейчас Free. Pro доступен по оплате.'}\n\n"
        f"Сегодня осталось: Free {plan['free_left_today']}."
    )
    await safe_reply_html(update.message, msg, reply_markup=kb(uid))

async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb_i = build_buy_keyboard(
        stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
        bepaid_url=BEPAID_CHECKOUT_URL or None,
    )
    await update.message.reply_text("Выбери способ оплаты:", reply_markup=kb_i)

# ---------- Основные действия ----------
async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(context.args).strip() if context.args else (update.message.text or "").strip()
    if not text:
        USER_STATE[uid] = "AWAIT_EXPLAIN"
        return await update.message.reply_text("🧠 Что объяснить/решить? Напиши одной фразой.", reply_markup=kb(uid))

    need_pro = PRO_NEXT[uid] or False
    ok, mode, reason = consume_request(uid, need_pro=need_pro, allow_trial=False)
    if not ok:
        kb_i = build_buy_keyboard(TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""), BEPAID_CHECKOUT_URL or None)
        return await update.message.reply_text(f"Нужен Pro: {reason}. Оформи оплату:", reply_markup=kb_i)
    PRO_NEXT[uid] = False  # сбрасываем флаг

    if USER_SUBJECT[uid] == "auto":
        subj = await classify_subject(text)
        if subj in SUBJECTS:
            USER_SUBJECT[uid] = subj

    _get_user_stats(uid).kinds["solve_text"] += 1

    spinner_finish, spinner_set = await start_spinner(update, context, "Решаю задачу…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await call_model(uid, text, mode=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        set_followup_context(uid, text, out)
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "Нужно что-то уточнить по решению?\n"
            "ℹ️ <b>1 уточнение — бесплатно в течение 15 минут</b>. Дальше уточнения будут списывать лимит/кредит.",
            reply_markup=keyboard,
        )
        USER_STATE[uid] = "AWAIT_FOLLOWUP_YN"
    except Exception as e:
        log.exception("explain")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))
    finally:
        await spinner_finish()

async def essay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    topic = " ".join(context.args).strip()
    if not topic:
        USER_STATE[uid] = "AWAIT_ESSAY"
        return await update.message.reply_text("📝 Тема сочинения?", reply_markup=kb(uid))

    need_pro = PRO_NEXT[uid] or _is_new_user_pro(uid)  # сочинение лучше в pro, но если нет — пойдём как free
    ok, mode, reason = consume_request(uid, need_pro=need_pro, allow_trial=False)
    if not ok:
        kb_i = build_buy_keyboard(TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""), BEPAID_CHECKOUT_URL or None)
        return await update.message.reply_text(f"Нужен Pro: {reason}. Оформи оплату:", reply_markup=kb_i)
    PRO_NEXT[uid] = False

    _get_user_stats(uid).kinds["essay"] += 1

    spinner_finish, spinner_set = await start_spinner(update, context, "Готовлю сочинение…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await call_model(uid, f"Напиши сочинение по теме: {topic}", mode=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        set_followup_context(uid, topic, out)
        USER_STATE[uid] = "AWAIT_FOLLOWUP_YN"
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "Нужно уточнить/сократить/перефразировать?\n"
            "ℹ️ <b>1 уточнение — бесплатно в течение 15 минут</b>. Дальше — списание лимита/кредита.",
            reply_markup=keyboard,
        )
    except Exception as e:
        log.exception("essay")
        await update.message.reply_text(f"❌ Ошибка: {e}", reply_markup=kb(uid))
    finally:
        await spinner_finish()

async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    ok, mode, reason = consume_request(uid, need_pro=True, allow_trial=False)
    if not ok:
        kb_i = build_buy_keyboard(TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""), BEPAID_CHECKOUT_URL or None)
        return await update.message.reply_text("Фото-решение доступно в Pro. Выбери оплату:", reply_markup=kb_i)

    spinner_finish, spinner_set = await start_spinner(update, context, "Обрабатываю фото…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
        tg_file = None
        if update.message.photo:
            tg_file = await update.message.photo[-1].get_file()
        elif update.message.document and str(update.message.document.mime_type or "").startswith("image/"):
            tg_file = await update.message.document.get_file()
        else:
            raise ValueError("No image")
        data = await tg_file.download_as_bytearray()
        _get_user_stats(uid).bytes_images_in += len(data)
        img = Image.open(io.BytesIO(data))

        spinner_set("Распознаю текст…")
        ocr_text = ocr_image(img)
        if ocr_text.strip():
            _get_user_stats(uid).ocr_ok += 1
        else:
            _get_user_stats(uid).ocr_fail += 1
            return await update.message.reply_text("Не удалось распознать текст. Попробуй переснять или напиши текстом.", reply_markup=kb(uid))

        if USER_SUBJECT[uid] == "auto":
            subj = await classify_subject(ocr_text)
            if subj in SUBJECTS:
                USER_SUBJECT[uid] = subj

        _get_user_stats(uid).kinds["solve_photo"] += 1

        spinner_set("Решаю…")
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await call_model(uid, ocr_text[:4000], mode=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        set_followup_context(uid, ocr_text[:800], out)
        USER_STATE[uid] = "AWAIT_FOLLOWUP_YN"
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "Нужно уточнение к решению?\n"
            "ℹ️ <b>1 уточнение — бесплатно в течение 15 минут</b>. Следующие — со списанием.",
            reply_markup=keyboard,
        )
    except Exception as e:
        log.exception("photo")
        keyboard = ReplyKeyboardMarkup([["📸 Решить по фото", "✍️ Напишу текстом"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Не получилось обработать фото. Попробуй ещё раз или напиши текстом:", reply_markup=keyboard)
        USER_STATE[uid] = "AWAIT_TEXT_OR_PHOTO_CHOICE"
    finally:
        await spinner_finish()

# ---------- Тексты/состояния ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = (update.message.text or "").strip()
    txt = raw.lower()
    state = USER_STATE[uid]

    if raw:
        USER_LANG[uid] = detect_lang(raw)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    if txt == "ℹ️ free vs pro":
        return await free_vs_pro(update, context)
    if txt == "💳 купить" or txt == "/buy":
        return await buy_cmd(update, context)
    if txt in {"🧾 моя статистика","моя статистика"}:
        return await mystats_cmd(update, context)
    if txt == "⭐ pro (след. запрос)":
        plan = get_user_plan(uid)
        if not plan["sub_active"]:
            kb_i = build_buy_keyboard(TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""), BEPAID_CHECKOUT_URL or None)
            return await update.message.reply_text("Pro доступен по оплате. Выбери способ:", reply_markup=kb_i)
        PRO_NEXT[uid] = True
        return await update.message.reply_text("Режим Pro включён для следующего запроса.", reply_markup=kb(uid))

    if state == "AWAIT_TEXT_OR_PHOTO_CHOICE":
        if txt == "📸 решить по фото":
            USER_STATE[uid] = None
            return await update.message.reply_text("Пришли фото задания.", reply_markup=kb(uid))
        if txt == "✍️ напишу текстом":
            USER_STATE[uid] = "AWAIT_EXPLAIN"
            return await update.message.reply_text("Напиши задание текстом — я всё сделаю.", reply_markup=kb(uid))
        return await update.message.reply_text("Выбери: 'Решить по фото' или 'Напишу текстом'")

    # Follow-up: Да/Нет
    if state == "AWAIT_FOLLOWUP_YN":
        if txt == "да":
            ctx = get_followup_context(uid)
            window_ok = in_free_window(ctx) and not (ctx or {}).get("used_free", False)
            USER_STATE[uid] = "AWAIT_FOLLOWUP_FREE" if window_ok else "AWAIT_FOLLOWUP_PAID"
            warn = ("ℹ️ Это уточнение бесплатно (в пределах 15 минут). Напиши, что именно уточнить."
                    if window_ok else "⚠️ Следующее уточнение спишет лимит/кредит. Напиши вопрос — и я продолжу по текущему решению.")
            return await update.message.reply_text(warn)
        if txt == "нет":
            USER_STATE[uid] = None
            return await update.message.reply_text("Ок! Если что — пиши снова.", reply_markup=kb(uid))
        return await update.message.reply_text("Ответь: Да или Нет")

    # Бесплатное уточнение
    if state == "AWAIT_FOLLOWUP_FREE":
        USER_STATE[uid] = "AWAIT_FOLLOWUP_NEXT"  # далее платные
        ctx = get_followup_context(uid)
        if not ctx or not in_free_window(ctx) or ctx.get("used_free", False):
            USER_STATE[uid] = "AWAIT_FOLLOWUP_PAID"
        else:
            out = await call_model_followup(uid, ctx["task"], ctx["answer"], raw, mode_tag="free")
            await reply_with_formulas(update.message, out, reply_markup=kb(uid))
            mark_followup_used(uid)
            keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text("Нужно ещё уточнение?\n⚠️ Дальше уточнения будут списывать лимит/кредит.", reply_markup=keyboard)
            return

    # Платное уточнение
    if state in {"AWAIT_FOLLOWUP_PAID", "AWAIT_FOLLOWUP_NEXT"}:
        ctx = get_followup_context(uid)
        ok, mode, reason = consume_request(uid, need_pro=False, allow_trial=False)
        if not ok:
            kb_i = build_buy_keyboard(TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""), BEPAID_CHECKOUT_URL or None)
            USER_STATE[uid] = None
            return await update.message.reply_text(f"Нужно списание. Оформи Pro/кредиты:", reply_markup=kb_i)
        prev_task = (ctx or {}).get("task", "")
        prev_ans = (ctx or {}).get("answer", "")
        out = await call_model_followup(uid, prev_task or raw, prev_ans, raw, mode_tag=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        USER_STATE[uid] = "AWAIT_FOLLOWUP_NEXT"
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Ещё вопрос по этой задаче? ⚠️ Будет списание лимита/кредита.", reply_markup=keyboard)
        return

    if txt == "🧠 объяснить":
        USER_STATE[uid] = "AWAIT_EXPLAIN"
        return await update.message.reply_text("Напиши текст задания одной фразой.", reply_markup=kb(uid))
    if txt == "📝 сочинение":
        USER_STATE[uid] = "AWAIT_ESSAY"
        return await update.message.reply_text("Тема сочинения?", reply_markup=kb(uid))
    if txt.startswith("📚 предмет:"):
        return await update.message.reply_text("Сменить: /subject <название|auto>", reply_markup=kb(uid))
    if txt.startswith("🎓 класс:"):
        return await update.message.reply_text("Сменить: /grade 5–11", reply_markup=kb(uid))
    if txt.startswith("👨‍👩‍👧 родит.:"):
        return await update.message.reply_text("Вкл/выкл: /parent on|off", reply_markup=kb(uid))

    if USER_STATE[uid] == "AWAIT_EXPLAIN":
        USER_STATE[uid] = None
        context.args = [raw]
        return await explain_cmd(update, context)
    if USER_STATE[uid] == "AWAIT_ESSAY":
        USER_STATE[uid] = None
        context.args = [raw]
        return await essay_cmd(update, context)

    # обычный текст — решаем как Free/Pro в зависимости от статуса и флага
    need_pro = PRO_NEXT[uid]
    ok, mode, reason = consume_request(uid, need_pro=need_pro, allow_trial=False)
    if not ok:
        kb_i = build_buy_keyboard(TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""), BEPAID_CHECKOUT_URL or None)
        return await update.message.reply_text(f"Нужен Pro: {reason}. Оформи оплату:", reply_markup=kb_i)
    PRO_NEXT[uid] = False
    context.args = [raw]
    return await explain_cmd(update, context)

# ---------- Мои метрики ----------
async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        # если будет внешний сервис — обернём to_thread
        plan = get_user_plan(uid)
        s_today = {"free": 3 - plan["free_left_today"], "credit": 0, "sub": 1 if plan["sub_active"] else 0}
    except Exception as e:
        log.warning(f"/mystats fail: {e}")
        s_today = {"free": 0, "credit": 0, "sub": 0}
    await update.message.reply_text(
        f"Сегодня: free {s_today['free']}, sub {'1' if s_today['sub'] else '0'}, credit {s_today['credit']}",
        reply_markup=kb(uid),
    )

# ---------- /stats (админ) ----------
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await mystats_cmd(update, context)
    try:
        text = _format_metrics_for_admin()
        kb_i = InlineKeyboardMarkup([[InlineKeyboardButton("⚙️ Админ-панель", callback_data="admin:menu")]])
        return await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb_i)
    except Exception as e:
        log.exception("/stats")
        return await update.message.reply_text(f"Не получилось собрать метрики: {e}")

def _format_metrics_for_admin() -> str:
    s = stats_snapshot()
    t = s["totals"]
    lines = [
        "<b>Метрики</b>",
        f"Пользователей: {t['users_count']}",
        f"Задач всего: {t['tasks_total']} (text={t['solve_text']}, photo={t['solve_photo']}, essay={t['essay']})",
        f"GPT вызовов: {t['gpt_calls']} за {t['gpt_time_sum']:.1f}s",
        f"OCR ok/fail: {t['ocr_ok']}/{t['ocr_fail']}",
    ]
    return "\n".join(lines)

def admin_kb(page_users: int = 1) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📈 Метрики", callback_data="admin:metrics")],
        [InlineKeyboardButton("👥 Пользователи", callback_data=f"admin:users:{page_users}")],
        [InlineKeyboardButton("💳 Платежи", callback_data="admin:billing")],
        [InlineKeyboardButton("🧠 ВБД", callback_data="admin:vdb")],
        [InlineKeyboardButton("⚙️ Настройки", callback_data="admin:settings")],
    ])

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    await update.message.reply_text("Админ-панель:", reply_markup=admin_kb())

def _paginate_users(page: int, per_page: int = 10) -> tuple[str, InlineKeyboardMarkup]:
    ids = sorted(USERS.keys())
    total = len(ids)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    chunk = ids[start:start + per_page]
    lines = [f"<b>Пользователи</b> (страница {page}/{pages}, всего {total})"]
    for uid in chunk:
        st = USERS[uid]
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.last_seen))
        kinds = ", ".join(f"{k}:{v}" for k, v in st.kinds.items()) or "—"
        lines.append(f"• <code>{uid}</code> — {html.escape(st.name or '')} (@{st.username or '—'})")
        lines.append(f"  seen={seen}; gpt={st.gpt_calls}; {kinds}")
    nav = []
    if page > 1: nav.append(InlineKeyboardButton("« Назад", callback_data=f"admin:users:{page-1}"))
    if page < pages: nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"admin:users:{page+1}"))
    kb_i = InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]) if nav \
        else InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]])
    return "\n".join(lines), kb_i

async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid): return await q.answer("Недостаточно прав.", show_alert=True)
    data = q.data or ""
    log.info(f"ADMIN_CLICK by {uid}: {data}")

    if data == "admin:menu":
        await q.edit_message_text("Админ-панель:", reply_markup=admin_kb()); return
    if data == "admin:metrics":
        text = _format_metrics_for_admin()
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup([
            [InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu"),
             InlineKeyboardButton("JSON", callback_data="admin:metrics_json")]
        ])); return
    if data == "admin:metrics_json":
        snap = json.dumps(stats_snapshot(), ensure_ascii=False)[:3500]
        await q.edit_message_text(f"<pre>{html.escape(snap)}</pre>", parse_mode="HTML",
                                  reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]))
        return
    if data.startswith("admin:users:"):
        try: page = int(data.split(":")[2])
        except Exception: page = 1
        text, kb_i = _paginate_users(page)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_i); return
    if data == "admin:vdb":
        kb_i = InlineKeyboardMarkup([
            [InlineKeyboardButton("📌 Подсказка по /vdbtest", callback_data="admin:vdb:hint")],
            [InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")],
        ])
        await q.edit_message_text("Раздел ВБД.", reply_markup=kb_i); return
    if data == "admin:vdb:hint":
        await q.edit_message_text(
            "Примеры:\n"
            "<code>/vdbtest формула площади трапеции</code>\n"
            "<code>/vdbtest раствор цемента м200 пропорции 5</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]),
        ); return
    if data == "admin:billing":
        await q.edit_message_text(
            "Платежи: интегрируй сводку из services/usage.py + payments.\n"
            "Показывать: выручка, кол-во покупок, конверсия, последние N операций.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]),
        ); return

# ---------- Админ-команды ----------
async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    env_ids = sorted(_env_admin_ids())
    db_ids = sorted(_load_admins_from_db())
    union_ids = sorted(all_admin_ids())
    lines = [
        "<b>Админы (ENV):</b> " + (", ".join(map(str, env_ids)) or "—"),
        "<b>Админы (DB):</b> " + (", ".join(map(str, db_ids)) or "—"),
        "<b>Итого:</b> " + (", ".join(map(str, union_ids)) or "—"),
        "",
        "Добавить: <code>/sudo_add 123456789</code>",
        "Удалить: <code>/sudo_del 123456789</code>",
    ]
    await update.message.reply_html("\n".join(lines))

async def sudo_add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Используй: /sudo_add <telegram_id>")
    target = int(context.args[0]); add_admin(target)
    log.info(f"ADMIN: {uid} added admin {target}")
    await update.message.reply_text(f"Готово. Добавлен admin: {target}")

async def sudo_del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Используй: /sudo_del <telegram_id>")
    target = int(context.args[0]); del_admin(target)
    log.info(f"ADMIN: {uid} removed admin {target}")
    await update.message.reply_text(f"Готово. Удалён admin: {target}")

# ---------- ВБД: тестовый поиск (админ) — БЕЗ дублей ----------
async def vdbtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        return await update.message.reply_text(
            "Использование: /vdbtest запрос…\n"
            "Пример: /vdbtest формула площади трапеции\n"
            "Пример: /vdbtest раствор цемента м200 пропорции 5"
        )
    subj_raw = USER_SUBJECT.get(uid, "auto")
    subj_key = subject_to_vdb_key(subj_raw)
    try:
        grade_int = int(USER_GRADE.get(uid, "8")) if str(USER_GRADE.get(uid, "8")).isdigit() else 8
    except Exception:
        grade_int = 8
    q_clamped = clamp_words(q, 40)
    async def _srch(skey): return await search_rules(client, q_clamped, skey, grade_int, top_k=5)
    try:
        rules = []
        try:
            rules = await asyncio.wait_for(_srch(subj_key), timeout=3.0)
        except Exception as e:
            log.warning(f"/vdbtest primary timeout/fail: {e}")
        if not rules and subj_key != subj_raw:
            try:
                rules = await asyncio.wait_for(_srch(subj_raw), timeout=3.0)
            except Exception as e:
                log.warning(f"/vdbtest fallback timeout/fail: {e}")
        if not rules:
            return await update.message.reply_text("⚠️ Ничего не нашёл в ВБД по этому запросу.")
        lines = []
        for r in (rules or [])[:5]:
            book = (r.get("book") or "").strip(); ch = (r.get("chapter") or "").strip(); pg = r.get("page")
            brief = (r.get("rule_brief") or r.get("text") or r.get("rule") or "").strip()
            meta = " · ".join([x for x in [book, ch, f"стр. {pg}" if pg else ""] if x])
            lines.append(("— " + brief) + (f"\n   ({meta})" if meta else ""))
        out = "\n".join(lines)[:3500]
        return await update.message.reply_text(out or "⚠️ Пусто.")
    except Exception as e:
        log.exception("vdbtest")
        return await update.message.reply_text(f"Ошибка ВБД: {e}")

# =========================
# === БЛОК 6/6 (ФИНАЛ) ===
# Платежи, health-сервер, main()
# =========================

import os, io, json, time, threading, asyncio, logging
from http.server import BaseHTTPRequestHandler, HTTPServer
from telegram import (
    Update, InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler, filters as f,
    ContextTypes, CallbackQueryHandler
)

log = logging.getLogger("gotovo-bot")

# ===== Параметры подписки =====
TRIAL_DAYS = 7
PRO_MONTH_DAYS = 30

STARS_WEBHOOK_SECRET   = os.getenv("STARS_WEBHOOK_SECRET", "")
BEPAID_WEBHOOK_SECRET  = os.getenv("BEPAID_WEBHOOK_SECRET", "")
BEPAID_PUBLIC_CHECKOUT = os.getenv("BEPAID_PUBLIC_CHECKOUT_URL", "")

# ===== Простое хранилище пользователей =====
USERS = {}

def _now(): return time.time()

def get_user(uid: int) -> dict:
    u = USERS.get(str(uid))
    if not u:
        u = {
            "uid": uid,
            "first_seen": _now(),
            "last_seen": _now(),
            "pro_until": 0.0,
            "flags": {"trial_granted": False},
        }
        USERS[str(uid)] = u
    else:
        u["last_seen"] = _now()
    return u

def is_pro(u: dict) -> bool: return u.get("pro_until", 0) > _now()

def ensure_trial(uid: int) -> bool:
    u = get_user(uid)
    if not u["flags"].get("trial_granted"):
        u["flags"]["trial_granted"] = True
        u["pro_until"] = _now() + TRIAL_DAYS * 86400
        return True
    return False

def grant_pro_days(uid: int, days: int):
    u = get_user(uid)
    base = max(u.get("pro_until", 0), _now())
    u["pro_until"] = base + days * 86400

# ===== Команды =====
async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    u = get_user(uid)
    just = ensure_trial(uid)
    if just:
        msg = (
            "<b>👋 Добро пожаловать!</b>\n\n"
            f"Тебе активирован <b>{TRIAL_DAYS}-дневный Pro-триал</b>. "
            "После него останется бесплатный режим (gpt-4o-mini)."
        )
    elif is_pro(u):
        left = int((u["pro_until"] - _now()) / 86400) + 1
        msg = f"✅ У тебя активен <b>Pro</b>, осталось ~{left} дн."
    else:
        msg = "ℹ️ Триал уже использован. Сейчас — Free (gpt-4o-mini). Чтобы вернуться к Pro: /buy"
    await update.message.reply_html(msg)

async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (
        "<b>Оплата Pro</b>\n\n"
        "• <b>bePaid</b> (карта РБ / ЕРИП)\n"
        "• <b>Telegram Stars</b>\n\n"
        f"После оплаты Pro активируется на {PRO_MONTH_DAYS} дней."
    )
    bep_url = BEPAID_PUBLIC_CHECKOUT
    if bep_url:
        sep = "&" if "?" in bep_url else "?"
        bep_url = f"{bep_url}{sep}uid={uid}"
    kb = [
        [InlineKeyboardButton("💳 Оплатить (bePaid)", url=bep_url or "https://example.com/bepaid")],
        [InlineKeyboardButton("⭐ Оплатить Stars", callback_data="buy_stars")],
    ]
    await update.message.reply_html(text, reply_markup=InlineKeyboardMarkup(kb))

async def on_buy_stars_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    await q.edit_message_text("Открыл счёт ⭐ Stars. Заверши оплату в Telegram — статус обновится автоматически.")

# ===== Health-сервер =====
class _Health(BaseHTTPRequestHandler):
    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/": return self._json(200, {"ok": True, "ts": int(_now())})
        if self.path.startswith("/stats.json"):
            total = len(USERS)
            pro = sum(1 for u in USERS.values() if is_pro(u))
            return self._json(200, {"ok": True, "users": total, "pro": pro})
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or "0")
        raw = self.rfile.read(length) if length > 0 else b""
        try: data = json.loads(raw.decode() or "{}")
        except: data = {}

        if self.path.startswith("/vdb/search"):
            q = str(data.get("q") or "").strip()
            if not q: return self._json(400, {"ok": False, "error": "empty_query"})
            try:
                loop = self.server.loop  # type: ignore
                async def _srch(): return [{"text": f"stub: {q}", "score": 1.0}]
                fut = asyncio.run_coroutine_threadsafe(_srch(), loop)
                return self._json(200, {"ok": True, "results": fut.result(3)})
            except Exception as e:
                return self._json(500, {"ok": False, "error": str(e)})

        if self.path.startswith("/webhook/bepaid"):
            if self.headers.get("X-Auth") != BEPAID_WEBHOOK_SECRET:
                return self._json(401, {"ok": False, "error": "unauthorized"})
            uid = int(data.get("uid") or 0)
            if uid: grant_pro_days(uid, PRO_MONTH_DAYS); return self._json(200, {"ok": True})
            return self._json(400, {"ok": False, "error": "bad_payload"})

        if self.path.startswith("/webhook/stars"):
            if self.headers.get("X-Auth") != STARS_WEBHOOK_SECRET:
                return self._json(401, {"ok": False, "error": "unauthorized"})
            uid = int(data.get("uid") or 0)
            if uid: grant_pro_days(uid, PRO_MONTH_DAYS); return self._json(200, {"ok": True})
            return self._json(400, {"ok": False, "error": "bad_payload"})

        return self._json(404, {"ok": False, "error": "not_found"})

class _HealthThread(threading.Thread):
    daemon = True
    def __init__(self, port: int): super().__init__(name="health-thread"); self.port=port; self.loop=None
    def run(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        httpd = HTTPServer(("0.0.0.0", self.port), _Health)
        httpd.loop = self.loop  # type: ignore
        log.info("Health server on 0.0.0.0:%s", self.port)
        httpd.serve_forever()

def _start_health_and_metrics():
    port = int(os.getenv("HEALTH_PORT", os.getenv("PORT", "8080")))
    ht = _HealthThread(port); ht.start(); return ht

# ===== Main =====
def _register_handlers(app: Application):
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CallbackQueryHandler(on_buy_stars_cb, pattern=r"^buy_stars"))

def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token: raise SystemExit("Нет TELEGRAM_TOKEN")

    _start_health_and_metrics()

    app = Application.builder().token(token).build()
    _register_handlers(app)

    log.info("Bot started (long-polling)")
    app.run_polling()

if __name__ == "__main__":
    main()
# =========================
# === КОНЕЦ БЛОКА 6/6 ====
# =========================

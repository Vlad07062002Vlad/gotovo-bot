# bot.py — R2: стабильность + анти-OOM + логи + таймауты + безопасные обёртки + ВБД (Qdrant) + Админка
# Регион: Беларусь. Оплаты: Telegram Stars / Карта РБ / ЕРИП. (BePaid — заготовка, выключено флагом)
# Follow-up: 1 бесплатное уточнение (15 минут) → дальше списание.
# Важно: сохранена вся логика и промпты; добавлены только живучесть/логи/ограничители и мелкие фиксы.

import os
import io
import re
import gc
import html
import json
import time
import sqlite3
import tempfile
import logging
import threading
import asyncio
import concurrent.futures
from time import perf_counter
from http.server import BaseHTTPRequestHandler, HTTPServer
from collections import defaultdict, Counter
from typing import Optional, Tuple

from rag_vdb import search_rules, clamp_words  # RAG

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

from openai import AsyncOpenAI

from PIL import Image, ImageOps, ImageFilter, ImageEnhance, ImageFile
Image.MAX_IMAGE_PIXELS = int(float(os.getenv("MAX_IMAGE_PIXELS", "12000000")))  # ≤12MP
ImageFile.LOAD_TRUNCATED_IMAGES = True

import pytesseract
from pytesseract import TesseractError

# === Формулы (юнникод + опциональный TeX→PNG) ===
from services.formulas import postprocess_formulas, extract_tex_snippets, render_tex_png
from config import RENDER_TEX

# ---------- ЛОГИ ----------
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(level=getattr(logging, LOG_LEVEL, logging.INFO),
                    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
log = logging.getLogger("gotovo-bot")
for name in ("telegram", "httpx", "openai", "PIL", "qdrant_client"):
    logging.getLogger(name).setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

def rid() -> str:
    return f"r{int(time.time()*1000)%1000000:06d}"

# ---------- ENV / конфиг ----------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

# Платежи
TELEGRAM_STARS_ENABLED = os.getenv("TELEGRAM_STARS_ENABLED", "true").lower() == "true"
TELEGRAM_PROVIDER_TOKEN = os.getenv("TELEGRAM_PROVIDER_TOKEN", "")  # для Stars должен быть пустой

CARD_CHECKOUT_URL = os.getenv("CARD_CHECKOUT_URL", "")
ERIP_CHECKOUT_URL = os.getenv("ERIP_CHECKOUT_URL", "")
CARD_WEBHOOK_SECRET = os.getenv("CARD_WEBHOOK_SECRET", "")
ERIP_WEBHOOK_SECRET = os.getenv("ERIP_WEBHOOK_SECRET", "")

# BePaid (заготовка, выключено)
BEPAID_ENABLED = os.getenv("BEPAID_ENABLED", "false").lower() == "true"
BEPAID_WEBHOOK_SECRET = os.getenv("BEPAID_WEBHOOK_SECRET", "")
BEPAID_CHECKOUT_URL = os.getenv("BEPAID_CHECKOUT_URL", "")

# ВБД-хук
VDB_WEBHOOK_SECRET = os.getenv("VDB_WEBHOOK_SECRET", "")

# Таймауты сервисов (сек)
CONSUME_TIMEOUT = float(os.getenv("CONSUME_TIMEOUT", "2.5"))
MY_STATS_TIMEOUT = float(os.getenv("MY_STATS_TIMEOUT", "2.5"))
SUMMARY_TIMEOUT = float(os.getenv("SUMMARY_TIMEOUT", "3.0"))

# Диски / БД / Метрики
DATA_DIR = os.getenv("DATA_DIR", "/data")
DB_PATH = os.path.join(DATA_DIR, "app.db")
METRICS_PATH = os.path.join(DATA_DIR, "metrics.json")
METRICS_AUTOSAVE_SEC = int(os.getenv("METRICS_AUTOSAVE_SEC", "60"))

# OCR
TESS_LANGS_DEFAULT = "rus+bel+eng+deu+fra"
TESS_LANGS = os.getenv("TESS_LANGS", TESS_LANGS_DEFAULT)
TESS_CONFIG = os.getenv("TESS_CONFIG", "--oem 3 --psm 6 -c preserve_interword_spaces=1")
MAX_IMG_SIDE = int(os.getenv("MAX_IMG_SIDE", "2000"))

if not TELEGRAM_TOKEN:
    raise SystemExit("Нет TELEGRAM_TOKEN (fly secrets set TELEGRAM_TOKEN=...)")
if not OPENAI_API_KEY:
    raise SystemExit("Нет OPENAI_API_KEY (fly secrets set OPENAI_API_KEY=...)")

# --- OpenAI клиент с таймаутом/ретраями ---
OPENAI_TIMEOUT = float(os.getenv("OPENAI_TIMEOUT", "30"))  # сек
client = AsyncOpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT, max_retries=2)

# --- Ограничим дефолтный executor (меньше RAM-пик) ---
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "4"))
asyncio.get_event_loop().set_default_executor(concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS))

# ---------- Только followup-таблица ----------
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

def _now_day_ym():
    now = int(time.time()); day = now // 86400
    ym = time.strftime("%Y%m", time.gmtime(now))
    return now, day, ym

# ---------- Админы ----------
def _db_admins():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """CREATE TABLE IF NOT EXISTS admin_users(
            user_id INTEGER PRIMARY KEY,
            added_ts INTEGER
        )"""
    ); return conn

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
    with _db_admins() as db:
        db.execute("DELETE FROM admin_users WHERE user_id=?", (uid,))
    return True

# ---------- ПАМЯТЬ (RAM) ----------
SUBJECTS = {"математика","русский","английский","физика","химия","история","обществознание","биология",
            "информатика","география","литература","auto","беларуская мова","беларуская літаратура"}
SUBJECT_VDB_KEY = {"математика":"math","физика":"physics","химия":"chemistry","биология":"biology",
                   "информатика":"informatics","география":"geography","русский":"russian","литература":"literature",
                   "английский":"english","обществознание":"social_studies","история":"history",
                   "беларуская мова":"bel_mova","беларуская літаратура":"bel_lit"}
def subject_to_vdb_key(s: str) -> str:
    s = (s or "").strip().lower(); return SUBJECT_VDB_KEY.get(s, s)

USER_SUBJECT = defaultdict(lambda: "auto")
USER_GRADE = defaultdict(lambda: "8")
PARENT_MODE = defaultdict(lambda: True)
USER_STATE = defaultdict(lambda: None)
USER_LANG = defaultdict(lambda: "ru")
PRO_NEXT = defaultdict(lambda: False)

# ---- Follow-up контекст ----
FOLLOWUP_FREE_WINDOW_SEC = 15 * 60

def set_followup_context(uid: int, task_text: str, answer_text: str):
    snippet_task = (task_text or "").strip()[:1200]
    snippet_ans = (answer_text or "").strip()[:1200]
    now = int(time.time())
    with _db_followup() as db:
        db.execute(
            """INSERT INTO followup_state(user_id,last_task,last_answer,ts,used_free)
               VALUES(?,?,?,?,0)
               ON CONFLICT(user_id) DO UPDATE SET last_task=?, last_answer=?, ts=?, used_free=0""",
            (uid, snippet_task, snippet_ans, now, snippet_task, snippet_ans, now),
        )

def get_followup_context(uid: int):
    with _db_followup() as db:
        row = db.execute("SELECT last_task,last_answer,ts,used_free FROM followup_state WHERE user_id=?", (uid,)).fetchone()
    if not row: return None
    return {"task": row[0] or "", "answer": row[1] or "", "ts": row[2] or 0, "used_free": bool(row[3])}

def mark_followup_used(uid: int):
    with _db_followup() as db:
        db.execute("UPDATE followup_state SET used_free=1 WHERE user_id=?", (uid,))

def in_free_window(ctx: dict | None) -> bool:
    if not ctx: return False
    return int(time.time()) - int(ctx.get("ts", 0)) <= FOLLOWUP_FREE_WINDOW_SEC

# ---------- Клавиатура ----------
def kb(uid: int) -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [
            ["🧠 Объяснить", "📝 Сочинение", "⭐ Pro (след. запрос)"],
            [f"📚 Предмет: {USER_SUBJECT[uid]}", f"🎓 Класс: {USER_GRADE[uid]}", f"👨‍👩‍👧 Родит.: {'вкл' if PARENT_MODE[uid] else 'выкл'}"],
            ["ℹ️ Free vs Pro", "💳 Купить", "🧾 Моя статистика"],
        ], resize_keyboard=True,
    )

# ---------- Безопасный HTML ----------
ALLOWED_TAGS = {"b", "i", "code", "pre"}
_TAG_OPEN = {t: f"&lt;{t}&gt;" for t in ALLOWED_TAGS}
_TAG_CLOSE = {t: f"&lt;/{t}&gt;" for t in ALLOWED_TAGS}

def sanitize_html(text: str) -> str:
    if not text: return ""
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
        nonlocal current_label; current_label = s
    async def worker():
        frames = ["⏳","⌛","🕐","🕑","🕒","🕓","🕔","🕕","🕖","🕗","🕘","🕙","🕚","🕛"]; i = 0
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

# ---------- Системный промпт ----------
def sys_prompt(uid: int) -> str:
    subject = USER_SUBJECT[uid]; grade = USER_GRADE[uid]; parent = PARENT_MODE[uid]
    base = (
        "Ты — школьный помощник и ИСПОЛНИТЕЛЬ домашнего задания. "
        "Сначала выдай <b>Ответы</b> (готовый результат по пунктам), затем обязательно дай <b>Подробное Пояснение</b> — по шагам, простым русским, "
        "как терпеливый репетитор для самого слабого ученика. "
        "1) Переформулируй условие коротко; 2) Объясни, <i>зачем</i> нужен каждый шаг; 3) Покажи ход решения с мини-шагами; "
        "4) Укажи типичные ошибки и как их избежать; 5) Дай проверку/самопроверку; 6) Если есть альтернативы — покажи самый простой способ. "
        "Если в базе учебников (ВДБ) нет точного материала по теме, всё равно решай задание, опираясь на свои предметные знания и общепринятые методики по этому предмету "
        "(не ссылайся на конкретный учебник). Для схем используй текстовые блоки. Разрешённые HTML-теги: <b>, <i>, <code>, <pre>."
    )
    form_hint = "Ключевые формулы выделяй отдельной строкой в блоке <pre> и при возможности добавляй: TeX: \\int_0^1 x^2\\,dx"
    sub = f"Предмет: {subject}." if subject != "auto" else "Определи предмет сам."
    grd = f"Класс: {grade}."
    par = ("<b>Памятка для родителей:</b> на что смотреть при проверке; что должен проговорить ребёнок; типичные ошибки; мини-тренировка."
           if parent else "")
    return f"{base} {form_hint} {sub} {grd} {par}"

def _answers_hint(task_lang: str) -> str:
    return {"be":"Адказы — па-беларуску. Тлумачэнне — па-руску.","de":"Antworten auf Deutsch. Erklärung auf Russisch.",
            "fr":"Réponses en français. Explication en russe.","en":"Answers in English. Explanation in Russian."}.get(task_lang,
            "Ответы — по-русски. Пояснение — по-русски.")

# ---------- Служебные утилиты памяти ----------
def meminfo_mb() -> tuple[int, int]:
    try:
        with open("/proc/meminfo") as f:
            d = {k: int(v.split()[0]) for k, v in (line.split(":", 1) for line in f)}
        return d.get("MemTotal", 0)//1024, d.get("MemAvailable", 0)//1024
    except Exception:
        return 0, 0

LOW_MEM_MB = int(os.getenv("LOW_MEM_MB", "120"))

# ---------- СЕРВИСНЫЕ ФУНКЦИИ (импорт + безопасные обёртки) ----------
try:
    from services.usage import consume_request, my_stats, daily_summary, get_user_plan, add_credits, activate_sub
    from services.payments import build_buy_keyboard, apply_payment_payload, get_stars_amount as _get_stars_amount_ext
    from services.router import select_model as _select_model_ext
    if _select_model_ext:  # type: ignore
        select_model = _select_model_ext  # type: ignore
    log.info("services/* connected")
except Exception as e:
    log.info(f"services/* not connected: {e}")
    try:
        import usage as _u, payments as _p, router as _r
        consume_request = _u.consume_request; my_stats = _u.my_stats
        daily_summary = getattr(_u, "daily_summary", None) or (lambda: {"day":0,"dau":0,"free_total":0,"paid":0,"credit":0,"sub":0})
        get_user_plan = _u.get_user_plan; add_credits = _u.add_credits; activate_sub = _u.activate_sub
        build_buy_keyboard = _p.build_buy_keyboard; apply_payment_payload = _p.apply_payment_payload
        try: select_model = _r.select_model
        except Exception: pass
        _get_stars_amount_ext = getattr(_p, "get_stars_amount", None)
        log.info("root modules connected")
    except Exception as e2:
        log.error(f"usage/payments/router not available: {e2}")
        raise SystemExit("Критично: нет модулей лимитов/оплат.")

async def _call_blocking(label: str, func, *args, timeout: float, default=None):
    r = rid(); t0 = perf_counter()
    try:
        res = await asyncio.wait_for(asyncio.to_thread(func, *args), timeout=timeout)
        log.debug(f"[{r}] {label}: ok dt={perf_counter()-t0:.2f}s")
        return res
    except Exception as e:
        log.warning(f"[{r}] {label}: fail {e} dt={perf_counter()-t0:.2f}s")
        return default

async def safe_consume_request(uid: int, need_pro: bool, allow_trial: bool):
    res = await _call_blocking("consume_request", consume_request, uid, need_pro, allow_trial,
                               timeout=CONSUME_TIMEOUT, default=None)
    if isinstance(res, tuple) and len(res) == 3:
        return res
    return (False, None, "temporary_unavailable")

async def safe_my_stats(uid: int):
    return await _call_blocking("my_stats", my_stats, uid, timeout=MY_STATS_TIMEOUT, default=None)

async def safe_daily_summary():
    return await _call_blocking("daily_summary", daily_summary, timeout=SUMMARY_TIMEOUT, default=None)

def _stars_amount(payload: str) -> int:
    if callable(globals().get("_get_stars_amount_ext")):
        try: return int(globals()["_get_stars_amount_ext"](payload))  # type: ignore
        except Exception: pass
    defaults = {"CREDITS_50": int(os.getenv("PRICE_CREDITS_50_XTR", "60")),
                "CREDITS_200": int(os.getenv("PRICE_CREDITS_200_XTR", "220")),
                "CREDITS_1000": int(os.getenv("PRICE_CREDITS_1000_XTR", "990")),
                "SUB_MONTH": int(os.getenv("PRICE_SUB_MONTH_XTR", "490"))}
    return defaults.get(payload, 100)

# ---------- OCR ----------
def _preprocess_image(img: Image.Image) -> Image.Image:
    img = ImageOps.exif_transpose(img).convert("L")
    img = ImageOps.autocontrast(img)
    img = ImageFilter.MedianFilter(size=3)(img)
    img = ImageEnhance.Sharpness(img).enhance(1.2)
    if max(img.width, img.height) > MAX_IMG_SIDE:
        scale = MAX_IMG_SIDE / max(img.width, img.height)
        img = img.resize((int(img.width * scale), int(img.height * scale)), Image.LANCZOS)
    return img

def ocr_image(img: Image.Image) -> str:
    base = ImageOps.exif_transpose(img)
    langs_chain = ([TESS_LANGS, "rus+bel+eng", "rus+eng", "rus", "bel", "eng", "deu", "fra"] if TESS_LANGS
                   else ["rus+bel+eng", "rus", "eng"])
    tried = set()
    for angle in (0, 90, 180, 270):
        if angle in tried: continue
        tried.add(angle)
        rot = base.rotate(-angle, expand=True)
        p = _preprocess_image(rot)
        try:
            txt = pytesseract.image_to_string(p, lang=langs_chain[0], config=TESS_CONFIG)
            if txt and txt.strip(): return txt.strip()
        except Exception: pass
        for langs in langs_chain[1:]:
            try:
                txt = pytesseract.image_to_string(p, lang=langs, config=TESS_CONFIG)
                if txt and txt.strip(): return txt.strip()
            except TesseractError:
                continue
    return ""

# ---------- Роутер моделей ----------
HEAVY_MARKERS = ("докажи","обоснуй","подробно","по шагам","поиндукции","уравнение","система","дробь","производная","интеграл","доказать","программа","алгоритм","код")
def select_model(prompt: str, mode: str) -> tuple[str, int, str]:
    p = (prompt or "").lower()
    if mode == "free": return "gpt-4o-mini", 700, "4o-mini"
    if mode == "trial": return "gpt-4o", 1200, "4o"
    long_input = len(p) > 600; heavy = long_input or any(k in p for k in HEAVY_MARKERS)
    return ("o4-mini", 1100, "o4-mini") if heavy else ("gpt-4o-mini", 900, "4o-mini")

async def call_model(uid: int, user_text: str, mode: str) -> str:
    lang = detect_lang(user_text); USER_LANG[uid] = lang
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

        for rr in (rules or [])[:5]:
            brief = (rr.get("rule_brief") if isinstance(rr, dict) else str(rr)) or ""
            brief = clamp_words(brief, 120)
            if brief: vdb_hints.append(f"• {brief}")
    except Exception as e:
        log.warning(f"VDB block error: {e}")

    vdb_context = ("\n\n[ВБД-памятка: используй только как справку, не ссылайся на конкретные книги]\n"
                   + "\n".join(vdb_hints)) if vdb_hints else ""

    content = ("Реши задание. Сначала <b>Ответы</b>, затем <b>Пояснение</b> по-русски. "
               f"{_answers_hint(lang)}\n\nТекст/условие:\n{user_text}" + vdb_context)

    # ---- Анти-OOM перед LLM ----
    tot, avail = meminfo_mb()
    if avail and avail < LOW_MEM_MB:
        log.warning(f"LOWMEM before LLM: avail={avail}MB (tot={tot}MB) — forcing gc")
        gc.collect()

    # ----- LLM -----
    t0 = perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": content}],
            temperature=0.25 if mode in {"free", "trial"} else 0.3,
            max_tokens=max_out,
        )
        out_text = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("LLM error")
        out_text = ("❌ Не получилось получить ответ от модели. "
                    "Попробуй ещё раз через минуту. Тех. детали в логах.")
    dt = perf_counter() - t0
    log.info(f"LLM model={model} tag={tag} mode={mode} dt={dt:.2f}s")
    return out_text

async def call_model_followup(uid: int, prev_task: str, prev_answer: str, follow_q: str, mode_tag: str) -> str:
    sys = sys_prompt(uid)
    prompt = (
        "Коротко и по делу уточни/дополни предыдущее решение.\n\n"
        f"Исходное задание:\n{prev_task[:2000]}\n\n"
        f"Ключевые фрагменты твоего ответа:\n{prev_answer[:2000]}\n\n"
        f"Вопрос-уточнение:\n{follow_q[:1200]}\n\n"
        "Дай только дополнение/уточнение, не переписывай всё решение. Если просят проверить шаг — проверь, укажи точечные правки."
    )
    model, max_out, tag = select_model(prev_task + " " + follow_q, mode_tag)
    t0 = perf_counter()
    try:
        resp = await client.chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": sys}, {"role": "user", "content": prompt}],
            temperature=0.25 if mode_tag in {"free","trial"} else 0.3,
            max_tokens=min(600, max_out),
        )
        out = (resp.choices[0].message.content or "").strip()
    except Exception:
        log.exception("LLM followup error")
        out = ("❌ Не удалось получить уточнение от модели. "
               "Попробуй ещё раз или переформулируй вопрос.")
    dt = perf_counter() - t0
    log.info(f"LLM followup model={model} tag={tag} mode={mode_tag} dt={dt:.2f}s")
    return out

# ---------- Отправка с формулами ----------
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
    __slots__ = ("uid","name","username","first_seen","last_seen","kinds","subjects","langs",
                 "gpt_calls","gpt_time_sum","tok_prompt","tok_completion","ocr_ok","ocr_fail","bytes_images_in")
    def __init__(self, uid: int):
        now = time.time()
        self.uid = uid; self.name=""; self.username=""; self.first_seen=now; self.last_seen=now
        self.kinds=Counter(); self.subjects=Counter(); self.langs=Counter()
        self.gpt_calls=0; self.gpt_time_sum=0.0; self.tok_prompt=0; self.tok_completion=0
        self.ocr_ok=0; self.ocr_fail=0; self.bytes_images_in=0

USERS: dict[int, UserStats] = {}
MAX_USERS_IN_RAM = int(os.getenv("MAX_USERS_IN_RAM", "4000"))
def _evict_users_if_needed():
    if len(USERS) <= MAX_USERS_IN_RAM: return
    extra = len(USERS) - MAX_USERS_IN_RAM
    old = sorted(USERS.items(), key=lambda kv: kv[1].last_seen)[:extra]
    for uid, _ in old: USERS.pop(uid, None)

def _get_user_stats(uid: int, update: Update | None = None) -> UserStats:
    with STATS_LOCK:
        st = USERS.get(uid) or UserStats(uid); USERS[uid]=st; st.last_seen=time.time()
        if update and update.effective_user:
            st.name = update.effective_user.full_name or st.name
            st.username = update.effective_user.username or st.username
        _evict_users_if_needed()
        return st

def stats_snapshot() -> dict:
    with STATS_LOCK:
        snap_users = {}; totals = {"users_count":0,"tasks_total":0,"solve_text":0,"solve_photo":0,"essay":0,
                                   "text_msg":0,"photo_msg":0,"ocr_ok":0,"ocr_fail":0,"gpt_calls":0,"gpt_time_sum":0.0,
                                   "tok_prompt":0,"tok_completion":0,"bytes_images_in":0,"subjects":{},"langs":{}}
        subjects_acc=Counter(); langs_acc=Counter()
        for uid, st in USERS.items():
            u = {"name":st.name,"username":st.username,"first_seen":st.first_seen,"last_seen":st.last_seen,
                 "kinds":dict(st.kinds),"subjects":dict(st.subjects),"langs":dict(st.langs),
                 "gpt_calls":st.gpt_calls,"gpt_time_sum":st.gpt_time_sum,"tok_prompt":st.tok_prompt,
                 "tok_completion":st.tok_completion,"ocr_ok":st.ocr_ok,"ocr_fail":st.ocr_fail,"bytes_images_in":st.bytes_images_in}
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
                st.kinds = Counter(u.get("kinds", {})); st.subjects = Counter(u.get("subjects", {}))
                st.langs = Counter(u.get("langs", {}))
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

# ---------- Команды ----------
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start","Запуск"), BotCommand("menu","Меню"), BotCommand("help","Помощь"),
        BotCommand("about","О боте"), BotCommand("subject","Предмет (или auto)"),
        BotCommand("grade","Класс 5–11"), BotCommand("parent","Режим родителей on/off"),
        BotCommand("mystats","Моя статистика"), BotCommand("stats","Статистика (админ)"),
        BotCommand("buy","Купить Pro/кредиты"), BotCommand("explain","Объяснить: /explain ТЕКСТ"),
        BotCommand("essay","Сочинение: /essay ТЕМА"), BotCommand("vdbtest","Проверить поиск в ВБД (админ)"),
        BotCommand("whoami","Показать мой Telegram ID"), BotCommand("admin","Админ-панель"),
        BotCommand("admins","Список админов"),
    ])

async def whoami_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Твой Telegram ID: <code>{uid}</code>", parse_mode="HTML", reply_markup=kb(uid))

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; _get_user_stats(uid, update)
    await safe_reply_html(update.message,
        "👋 Привет! Я — <b>Готово!</b>\n"
        "Free: 3 запроса/день (текст, GPT-4o-mini) + 1 «Пробный Pro»/день.\n"
        "Pro: месячный лимит (жёсткий), приоритет, сложные задачи на o4-mini/4o.\n\n"
        "Пиши задание или жми кнопки ниже.", reply_markup=kb(uid))

async def menu_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await start_cmd(update, context)

async def about_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply_html(update.message,
        "<b>📘 О боте «Готово!»</b>\n"
        "• Решаю и объясняю школьные задания 5–11 классов.\n"
        "• Free: 3/день (текст) + 1 Trial Pro/день.\n"
        "• Pro: больше лимитов и приоритет; тяжёлые задачи → o4-mini/4o.\n"
        "• Оплата: Telegram Stars / Карта РБ / ЕРИП.",
        reply_markup=kb(update.effective_user.id))

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await about_cmd(update, context)

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
    if not context.args or context.args[0].lower() not in {"on","off"}:
        return await update.message.reply_text("Используй: /parent on  или  /parent off", reply_markup=kb(uid))
    PARENT_MODE[uid] = context.args[0].lower() == "on"
    await update.message.reply_text(f"Режим для родителей: {'вкл' if PARENT_MODE[uid] else 'выкл'}", reply_markup=kb(uid))

async def free_vs_pro(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id; plan = get_user_plan(uid)
    msg = (
        "🔹 <b>Free</b>: 3 запроса/день (только текст) + 1 «Пробный Pro»/день.\n"
        "🔸 <b>Pro</b>: месячный лимит, приоритет; сложные задачи → o4-mini/4o; доступны фото/сканы.\n"
        "🔖 <b>Кредиты</b>: оплата «за штуку» запросов.\n\n"
        f"Сегодня осталось: Free {plan['free_left_today']}, Trial {plan['trial_left_today']}."
        f"\nПодписка: {'активна' if plan['sub_active'] else 'нет'}; остаток в мес.: {plan['sub_left_month']}"
    )
    await safe_reply_html(update.message, msg, reply_markup=kb(uid))

async def buy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb_i = build_buy_keyboard(
        stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
        card_url=CARD_CHECKOUT_URL or None, erip_url=ERIP_CHECKOUT_URL or None,
        # при необходимости позже добавим кнопку BePaid через payments
    )
    await update.message.reply_text("Выбери способ оплаты:", reply_markup=kb_i)

# ---------- Основные действия ----------
async def explain_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = " ".join(context.args).strip() if context.args else (update.message.text or "").strip()
    if not text:
        USER_STATE[uid] = "AWAIT_EXPLAIN"
        return await update.message.reply_text("🧠 Что объяснить/решить? Напиши одной фразой.", reply_markup=kb(uid))

    ok, mode, reason = await safe_consume_request(uid, need_pro=False, allow_trial=True)
    if not ok:
        kb_i = build_buy_keyboard(stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
                                  card_url=CARD_CHECKOUT_URL or None, erip_url=ERIP_CHECKOUT_URL or None)
        return await update.message.reply_text(f"Лимит недоступен сейчас ({reason}). Оформи Pro или купи кредиты:", reply_markup=kb_i)

    if USER_SUBJECT[uid] == "auto":
        subj = await classify_subject(text)
        if subj in SUBJECTS: USER_SUBJECT[uid] = subj

    spinner_finish, spinner_set = await start_spinner(update, context, "Решаю задачу…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await call_model(uid, text, mode=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        set_followup_context(uid, text, out)
        keyboard = ReplyKeyboardMarkup([["Да","Нет"]], resize_keyboard=True, one_time_keyboard=True)
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

    ok, mode, reason = await safe_consume_request(uid, need_pro=False, allow_trial=True)
    if not ok:
        kb_i = build_buy_keyboard(stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
                                  card_url=CARD_CHECKOUT_URL or None, erip_url=ERIP_CHECKOUT_URL or None)
        return await update.message.reply_text(f"Лимит недоступен ({reason}). Оформи Pro или купи кредиты:", reply_markup=kb_i)

    spinner_finish, spinner_set = await start_spinner(update, context, "Готовлю сочинение…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await call_model(uid, f"Напиши сочинение по теме: {topic}", mode=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        set_followup_context(uid, topic, out)
        USER_STATE[uid] = "AWAIT_FOLLOWUP_YN"
        keyboard = ReplyKeyboardMarkup([["Да","Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "Нужно что-то уточнить/сократить/перефразировать?\n"
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
    ok, mode, reason = await safe_consume_request(uid, need_pro=True, allow_trial=True)
    if not ok:
        kb_i = build_buy_keyboard(stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
                                  card_url=CARD_CHECKOUT_URL or None, erip_url=ERIP_CHECKOUT_URL or None)
        return await update.message.reply_text("Фото-решение доступно в Pro/Trial/кредитах. Выбери оплату:", reply_markup=kb_i)

    spinner_finish, spinner_set = await start_spinner(update, context, "Обрабатываю фото…")
    try:
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.UPLOAD_PHOTO)
        tg_file = None
        if update.message.photo: tg_file = await update.message.photo[-1].get_file()
        elif update.message.document and str(update.message.document.mime_type or "").startswith("image/"):
            tg_file = await update.message.document.get_file()
        else:
            raise ValueError("No image")

        data = await tg_file.download_as_bytearray()
        bio = io.BytesIO(data); del data
        with Image.open(bio) as src:
            src = ImageOps.exif_transpose(src)
            scale = min(MAX_IMG_SIDE / max(src.width, src.height), 1.0)
            if scale < 1.0:
                src = src.resize((int(src.width*scale), int(src.height*scale)), Image.LANCZOS)
            img = src.convert("L").copy()
        del bio

        spinner_set("Распознаю текст…")
        ocr_text = ocr_image(img); del img

        if not ocr_text.strip():
            return await update.message.reply_text("Не удалось распознать текст. Попробуй переснять или напиши текстом.", reply_markup=kb(uid))

        if USER_SUBJECT[uid] == "auto":
            subj = await classify_subject(ocr_text)
            if subj in SUBJECTS: USER_SUBJECT[uid] = subj

        # анти-OOM после OCR
        tot, avail = meminfo_mb()
        if avail and avail < LOW_MEM_MB:
            gc.collect(); log.warning(f"LOWMEM after OCR: avail={avail}MB (tot={tot}MB)")

        spinner_set("Решаю…")
        await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)
        out = await call_model(uid, ocr_text[:4000], mode=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        set_followup_context(uid, ocr_text[:800], out)
        USER_STATE[uid] = "AWAIT_FOLLOWUP_YN"
        keyboard = ReplyKeyboardMarkup([["Да","Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "Нужно уточнение к решению?\n"
            "ℹ️ <b>1 уточнение — бесплатно в течение 15 минут</b>. Следующие — со списанием.",
            reply_markup=keyboard,
        )
    except Exception as e:
        log.exception("photo")
        keyboard = ReplyKeyboardMarkup([["📸 Решить по фото","✍️ Напишу текстом"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text("Не получилось обработать фото. Попробуй ещё раз или напиши текстом:", reply_markup=keyboard)
        USER_STATE[uid] = "AWAIT_TEXT_OR_PHOTO_CHOICE"
    finally:
        await spinner_finish()

# ---------- Тексты и кнопки ----------
async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    raw = (update.message.text or "").strip(); txt = raw.lower(); state = USER_STATE[uid]
    if raw: USER_LANG[uid] = detect_lang(raw)
    await context.bot.send_chat_action(update.effective_chat.id, ChatAction.TYPING)

    if txt == "ℹ️ free vs pro": return await free_vs_pro(update, context)
    if txt == "💳 купить" or txt == "/buy": return await buy_cmd(update, context)
    if txt == "⭐ pro (след. запрос)":
        plan = get_user_plan(uid)
        if plan["trial_left_today"] <= 0 and not plan["sub_active"] and plan["credits"] <= 0:
            kb_i = build_buy_keyboard(stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
                                      card_url=CARD_CHECKOUT_URL or None, erip_url=ERIP_CHECKOUT_URL or None)
            return await update.message.reply_text("Trial уже использован. Доступны подписка или кредиты:", reply_markup=kb_i)
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

    if state == "AWAIT_FOLLOWUP_YN":
        if txt == "да":
            ctx = get_followup_context(uid)
            window_ok = in_free_window(ctx) and not (ctx or {}).get("used_free", False)
            USER_STATE[uid] = "AWAIT_FOLLOWUP_FREE" if window_ok else "AWAIT_FOLLOWUP_PAID"
            warn = ("ℹ️ Это уточнение бесплатно (в пределах 15 минут). Напиши, что именно уточнить."
                    if window_ok else
                    "⚠️ Следующее уточнение спишет лимит/кредит. Напиши вопрос — и я продолжу по текущему решению.")
            return await update.message.reply_text(warn)
        if txt == "нет":
            USER_STATE[uid] = None
            return await update.message.reply_text("Ок! Если что — пиши снова.", reply_markup=kb(uid))
        return await update.message.reply_text("Ответь: Да или Нет")

    if state == "AWAIT_FOLLOWUP_FREE":
        USER_STATE[uid] = "AWAIT_FOLLOWUP_NEXT"
        ctx = get_followup_context(uid)
        if not ctx or not in_free_window(ctx) or ctx.get("used_free", False):
            USER_STATE[uid] = "AWAIT_FOLLOWUP_PAID"
        else:
            out = await call_model_followup(uid, ctx["task"], ctx["answer"], raw, mode_tag="free")
            await reply_with_formulas(update.message, out, reply_markup=kb(uid))
            mark_followup_used(uid)
            keyboard = ReplyKeyboardMarkup([["Да","Нет"]], resize_keyboard=True, one_time_keyboard=True)
            await update.message.reply_text("Нужно ещё уточнение?\n⚠️ Дальше уточнения будут списывать лимит/кредит.", reply_markup=keyboard)
            return

    if state in {"AWAIT_FOLLOWUP_PAID","AWAIT_FOLLOWUP_NEXT"}:
        ctx = get_followup_context(uid)
        ok, mode, reason = await safe_consume_request(uid, need_pro=False, allow_trial=True)
        if not ok:
            kb_i = build_buy_keyboard(stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
                                      card_url=CARD_CHECKOUT_URL or None, erip_url=ERIP_CHECKOUT_URL or None)
            USER_STATE[uid] = None
            return await update.message.reply_text(f"Нужно списание ({reason}). Оформи Pro/Trial или купи кредиты:", reply_markup=kb_i)
        prev_task = (ctx or {}).get("task", ""); prev_ans = (ctx or {}).get("answer", "")
                out = await call_model_followup(uid, prev_task or raw, prev_ans, raw, mode_tag=mode)
        await reply_with_formulas(update.message, out, reply_markup=kb(uid))
        USER_STATE[uid] = "AWAIT_FOLLOWUP_NEXT"
        keyboard = ReplyKeyboardMarkup([["Да", "Нет"]], resize_keyboard=True, one_time_keyboard=True)
        await update.message.reply_text(
            "Ещё вопрос по этой задаче? ⚠️ Будет списание лимита/кредита.", reply_markup=keyboard
        )
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

    # Если ждём текст для /explain или /essay — перенаправляем
    if state == "AWAIT_EXPLAIN":
        USER_STATE[uid] = None
        context.args = [raw]
        return await explain_cmd(update, context)

    if state == "AWAIT_ESSAY":
        USER_STATE[uid] = None
        context.args = [raw]
        return await essay_cmd(update, context)

    # Режим «следующий Pro»
    need_pro = PRO_NEXT[uid]
    if need_pro:
        PRO_NEXT[uid] = False
        ok, mode, reason = await safe_consume_request(uid, need_pro=True, allow_trial=True)
        if not ok:
            kb_i = build_buy_keyboard(
                stars_enabled=TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == ""),
                card_url=CARD_CHECKOUT_URL or None,
                erip_url=ERIP_CHECKOUT_URL or None,
            )
            return await update.message.reply_text(
                f"Pro недоступен ({reason}). Выбери оплату:", reply_markup=kb_i
            )
        context.args = [raw]
        return await explain_cmd(update, context)

        # По умолчанию — обычное объяснение
    context.args = [raw]
    return await explain_cmd(update, context)

# ---------- Мои метрики ----------
async def mystats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    try:
        # если есть безопасная обёртка — используем, иначе прямая
        if "safe_my_stats" in globals():
            s = await safe_my_stats(uid)  # type: ignore
            if not s:
                return await update.message.reply_text(
                    "⚠️ Сервис статистики занят. Попробуй позже.", reply_markup=kb(uid)
                )
        else:
            s = my_stats(uid)  # type: ignore
        await update.message.reply_text(
            f"Сегодня: free {s['today']['free']}, trial {s['today']['trial']}, "
            f"credit {s['today']['credit']}, sub {s['today']['sub']}\n"
            f"7 дней:  free {s['last7']['free']}, trial {s['last7']['trial']}, "
            f"credit {s['last7']['credit']}, sub {s['last7']['sub']}\n"
            f"30 дней: free {s['last30']['free']}, trial {s['last30']['trial']}, "
            f"credit {s['last30']['credit']}, sub {s['last30']['sub']}",
            reply_markup=kb(uid),
        )
    except Exception as e:
        log.exception("mystats")
        await update.message.reply_text("Не удалось получить статистику. Попробуй позже.", reply_markup=kb(uid))

# ---------- Админ-команды ----------
async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Недостаточно прав.")
    try:
        if "safe_daily_summary" in globals():
            s = await safe_daily_summary()  # type: ignore
        else:
            s = daily_summary()  # type: ignore
        if not s:
            return await update.message.reply_text("Сервис статистики недоступен. Попробуй позже.")
        await update.message.reply_text(
            f"День {s['day']}: DAU={s['dau']}\n"
            f"Бесплатные (free+trial)={s['free_total']}, платные={s['paid']} "
            f"(credit={s['credit']}, sub={s['sub']})"
        )
    except Exception as e:
        log.exception("stats")
        await update.message.reply_text("Ошибка получения сводки.")

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
    target = int(context.args[0])
    add_admin(target)
    log.info(f"ADMIN: {uid} added admin {target}")
    await update.message.reply_text(f"Готово. Добавлен admin: {target}")

async def sudo_del_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    if not context.args or not context.args[0].isdigit():
        return await update.message.reply_text("Используй: /sudo_del <telegram_id>")
    target = int(context.args[0])
    del_admin(target)
    log.info(f"ADMIN: {uid} removed admin {target}")
    await update.message.reply_text(f"Готово. Удалён admin: {target}")

# ---------- ВБД: тестовый поиск (админ) ----------
async def vdbtest_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        return await update.message.reply_text("Недостаточно прав.")
    q = " ".join(context.args).strip() if context.args else ""
    if not q:
        return await update.message.reply_text("Использование: /vdbtest запрос...  (исп-ся текущие Предмет/Класс)")
    subj_key = subject_to_vdb_key(USER_SUBJECT[update.effective_user.id])
    try:
        grade_int = int(USER_GRADE[update.effective_user.id]) if str(USER_GRADE[update.effective_user.id]).isdigit() else 8
        q_clamped = clamp_words(q, 40)
        rules = await search_rules(client, q_clamped, subj_key, grade_int, top_k=5)
        if not rules and subj_key != USER_SUBJECT[update.effective_user.id]:
            rules = await search_rules(client, q_clamped, USER_SUBJECT[update.effective_user.id], grade_int, top_k=5)
        if not rules:
            return await update.message.reply_text("⚠️ Ничего не нашёл в ВБД по этому запросу.")
        lines = []
        for r in rules:
            book = r.get("book") or ""
            ch = r.get("chapter") or ""
            pg = r.get("page")
            brief = r.get("rule_brief") or r.get("text") or r.get("rule") or ""
            meta = " · ".join([x for x in [book, ch, f"стр. {pg}" if pg else ""] if x])
            lines.append(("— " + brief) + (f"\n   ({meta})" if meta else ""))
        await update.message.reply_text("\n".join(lines[:5])[:3500])
    except Exception as e:
        log.exception("vdbtest")
        await update.message.reply_text(f"Ошибка ВБД: {e}")

# ---------- /admin панель ----------
def admin_kb(page_users: int = 1) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("📈 Метрики", callback_data="admin:metrics")],
            [InlineKeyboardButton("👥 Пользователи", callback_data=f"admin:users:{page_users}")],
            [InlineKeyboardButton("💳 Платежи", callback_data="admin:billing")],
            [InlineKeyboardButton("🧠 ВБД", callback_data="admin:vdb")],
            [InlineKeyboardButton("⚙️ Настройки", callback_data="admin:settings")],
        ]
    )

async def admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not is_admin(uid):
        return await update.message.reply_text("Недостаточно прав.")
    await update.message.reply_text("Админ-панель:", reply_markup=admin_kb())

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

def _paginate_users(page: int, per_page: int = 10) -> tuple[str, InlineKeyboardMarkup]:
    ids = sorted(USERS.keys())
    total = len(ids)
    pages = max(1, (total + per_page - 1) // per_page)
    page = max(1, min(page, pages))
    start = (page - 1) * per_page
    chunk = ids[start : start + per_page]
    lines = [f"<b>Пользователи</b> (страница {page}/{pages}, всего {total})"]
    for uid in chunk:
        st = USERS[uid]
        seen = time.strftime("%Y-%m-%d %H:%M", time.localtime(st.last_seen))
        kinds = ", ".join(f"{k}:{v}" for k, v in st.kinds.items()) or "—"
        lines.append(f"• <code>{uid}</code> — {html.escape(st.name or '')} (@{st.username or '—'})")
        lines.append(f"  seen={seen}; gpt={st.gpt_calls}; {kinds}")
    nav = []
    if page > 1:
        nav.append(InlineKeyboardButton("« Назад", callback_data=f"admin:users:{page-1}"))
    if page < pages:
        nav.append(InlineKeyboardButton("Вперёд »", callback_data=f"admin:users:{page+1}"))
    kb_i = (
        InlineKeyboardMarkup([nav, [InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]])
        if nav
        else InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]])
    )
    return "\n".join(lines), kb_i

async def on_admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    if not is_admin(uid):
        return await q.answer("Недостаточно прав.", show_alert=True)
    data = q.data or ""
    log.info(f"ADMIN_CLICK by {uid}: {data}")

    if data == "admin:menu":
        await q.edit_message_text("Админ-панель:", reply_markup=admin_kb())
        return

    if data == "admin:metrics":
        text = _format_metrics_for_admin()
        await q.edit_message_text(
            text,
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu"),
                  InlineKeyboardButton("JSON", callback_data="admin:metrics_json")]]
            ),
        )
        return

    if data == "admin:metrics_json":
        snap = json.dumps(stats_snapshot(), ensure_ascii=False)[:3500]
        await q.edit_message_text(
            f"<pre>{html.escape(snap)}</pre>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]),
        )
        return

    if data.startswith("admin:users:"):
        try:
            page = int(data.split(":")[2])
        except Exception:
            page = 1
        text, kb_i = _paginate_users(page)
        await q.edit_message_text(text, parse_mode="HTML", reply_markup=kb_i)
        return

    if data == "admin:vdb":
        kb_i = InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("📌 Подсказка по /vdbtest", callback_data="admin:vdb:hint")],
                [InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")],
            ]
        )
        await q.edit_message_text("Раздел ВБД.", reply_markup=kb_i)
        return

    if data == "admin:vdb:hint":
        await q.edit_message_text(
            "Примеры:\n"
            "<code>/vdbtest формула площади трапеции</code>\n"
            "<code>/vdbtest раствор цемента м200 пропорции 5</code>",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]),
        )
        return

    if data == "admin:billing":
        await q.edit_message_text(
            "Платежи: интегрируй сводку из services/usage.py + payments.\n"
            "Показывать: выручка, кол-во покупок, конверсия, последние N операций.",
            reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="admin:menu")]]),
        )
        return

# ---------- Callbacks / Telegram Payments ----------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data or ""
    if data.startswith("buy_stars:"):
        payload = data.split(":", 1)[1]
        # Если Stars через Telegram недоступны (требуется провайдер-токен) — применяем payload напрямую
        if not (TELEGRAM_STARS_ENABLED and (TELEGRAM_PROVIDER_TOKEN == "")):
            msg = apply_payment_payload(q.from_user.id, payload)
            return await q.edit_message_text(msg + "\nПроверить баланс: /mystats")

        amount = _stars_amount(payload)
        await context.bot.send_invoice(
            chat_id=q.from_user.id,
            title="Покупка ДомашкаГотово",
            description=payload.replace("_", " "),
            payload=payload,
            provider_token=TELEGRAM_PROVIDER_TOKEN,
            currency="XTR",
            prices=[LabeledPrice("Услуга", amount)],
            start_parameter="gotovo",
            is_flexible=False,
        )
        return await q.edit_message_text("Открыл счёт в Telegram. Заверши оплату, пожалуйста.")
    return None

async def precheckout_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query: PreCheckoutQuery = update.pre_checkout_query
    await query.answer(ok=True)

async def successful_payment_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    payload = (
        update.message.successful_payment.invoice_payload
        if update.message and update.message.successful_payment
        else None
    )
    if payload:
        msg = apply_payment_payload(uid, payload)
        await update.message.reply_text(msg + "\nПроверить баланс: /mystats")

# ---------- Health + webhooks (карта/ЕРИП) + VDB ----------
class _Health(BaseHTTPRequestHandler):
    def _ok(self, body: bytes, ctype="text/plain; charset=utf-8"):
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _err(self, code: int, msg: str, ctype="text/plain; charset=utf-8"):
        body = (msg if isinstance(msg, str) else json.dumps(msg, ensure_ascii=False)).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        try:
            if self.path == "/":
                # быстрый healthcheck
                return self._ok(b"ok")
            if self.path == "/stats.json":
                payload = json.dumps(stats_snapshot(), ensure_ascii=False).encode("utf-8")
                return self._ok(payload, "application/json; charset=utf-8")
            return self._err(404, "not found")
        except Exception as e:
            log.exception("HTTP GET")
            return self._err(500, f"error: {e}")

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            raw = self.rfile.read(length) if length > 0 else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            auth = self.headers.get("X-Auth", "")
            path = self.path

            if path == "/vdb/search":
                q = str(data.get("q", "") or "").strip()
                if not q:
                    return self._err(400, {"ok": False, "error": "empty q"}, "application/json; charset=utf-8")
                top_k = data.get("top_k", 5)
                try:
                    top_k = int(top_k)
                except Exception:
                    top_k = 5
                top_k = max(1, min(20, top_k))

                subject_in = str(data.get("subject", "") or "").strip().lower()
                grade_in = data.get("grade", None)
                subj_key = subject_to_vdb_key(subject_in) if subject_in else "auto"
                try:
                    grade_int = int(grade_in) if grade_in is not None else 8
                except Exception:
                    grade_int = 8

                q_clamped = clamp_words(q, 40)
                try:
                    rules = asyncio.run(search_rules(client, q_clamped, subj_key, grade_int, top_k=top_k))
                    if not rules and subj_key != "auto":
                        rules = asyncio.run(
                            search_rules(client, q_clamped, subject_in or "auto", grade_int, top_k=top_k)
                        )
                except Exception as e:
                    return self._err(
                        500, {"ok": False, "error": f"search failed: {e}"}, "application/json; charset=utf-8"
                    )

                items = []
                for r in rules or []:
                    items.append(
                        {
                            "id": r.get("id"),
                            "score": float(r.get("score", 0) or 0),
                            "rule": r.get("rule_brief") or r.get("text") or r.get("rule") or "",
                            "source": " · ".join(
                                [
                                    x
                                    for x in [
                                        (r.get("book") or ""),
                                        (r.get("chapter") or ""),
                                        f"стр. {r.get('page')}" if r.get("page") else "",
                                    ]
                                    if x
                                ]
                            ),
                            "meta": {
                                "book": r.get("book"),
                                "chapter": r.get("chapter"),
                                "page": r.get("page"),
                                "subject": subject_in or subj_key,
                                "grade": grade_int,
                            },
                        }
                    )
                payload = {"ok": True, "count": len(items), "items": items}
                return self._ok(json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json; charset=utf-8")

            if path == "/webhook/card":
                if auth != CARD_WEBHOOK_SECRET or not CARD_WEBHOOK_SECRET:
                    return self._err(401, "bad auth")
                uid = int(data.get("user_id", 0) or 0)
                kind = data.get("kind")
                if not uid or not kind:
                    return self._err(400, "bad payload")
                msg = apply_payment_payload(uid, kind)
                return self._ok(msg.encode("utf-8"))

            if path == "/webhook/erip":
                if auth != ERIP_WEBHOOK_SECRET or not ERIP_WEBHOOK_SECRET:
                    return self._err(401, "bad auth")
                uid = int(data.get("user_id", 0) or 0)
                kind = data.get("kind")  # НЕ резать!
                if not uid or not kind:
                    return self._err(400, "bad payload")
                msg = apply_payment_payload(uid, kind)
                return self._ok(msg.encode("utf-8"))

            if path == "/vdb/upsert":
                if auth != VDB_WEBHOOK_SECRET or not VDB_WEBHOOK_SECRET:
                    return self._err(401, "bad auth")
                rules = data.get("rules") or []
                try:
                    if not rules:
                        return self._ok(b"VDB upsert ok (0)")
                    from rag_vdb import upsert_rules
                    asyncio.run(upsert_rules(client, rules))
                    return self._ok(b"VDB upsert ok")
                except Exception as e:
                    return self._err(500, f"upsert failed: {e}")

            return self._err(404, "not found")
        except Exception as e:
            log.exception("HTTP POST")
            return self._err(500, f"error: {e}")

def _run_health():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ---------- MAIN ----------
class _HealthThread(threading.Thread):
    def run(self):
        _run_health()

def main():
    try:
        stats_load()
    except Exception as e:
        log.warning(f"stats_load failed: {e}")

    # Health HTTP
    _HealthThread(daemon=True).start()

    # Автосейв метрик
    threading.Thread(target=_stats_autosave_loop, daemon=True).start()

    # Приложение Telegram
    app = Application.builder().token(TELEGRAM_TOKEN).post_init(set_commands).build()

    # Команды
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("menu", menu_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("about", about_cmd))
    app.add_handler(CommandHandler("subject", subject_cmd))
    app.add_handler(CommandHandler("grade", grade_cmd))
    app.add_handler(CommandHandler("parent", parent_cmd))
    app.add_handler(CommandHandler("mystats", mystats_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("buy", buy_cmd))
    app.add_handler(CommandHandler("explain", explain_cmd))
    app.add_handler(CommandHandler("essay", essay_cmd))
    app.add_handler(CommandHandler("vdbtest", vdbtest_cmd))
    app.add_handler(CommandHandler("whoami", whoami_cmd))
    app.add_handler(CommandHandler("admin", admin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("sudo_add", sudo_add_cmd))
    app.add_handler(CommandHandler("sudo_del", sudo_del_cmd))

    # Колбэки
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_callback, pattern=r"^buy_stars:"))
    app.add_handler(PreCheckoutQueryHandler(precheckout_handler))
    app.add_handler(MessageHandler(f.SUCCESSFUL_PAYMENT, successful_payment_handler))

    # Контент
    app.add_handler(MessageHandler(f.PHOTO | f.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(f.TEXT & ~f.COMMAND, on_text))

    log.info("Gotovo bot running…")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()

    


import os
import io
import json
import base64
import sqlite3
import logging
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional, Tuple

from telegram import Update, BotCommand, InputFile
from telegram.ext import (
    Application, CommandHandler, MessageHandler, ContextTypes, filters
)

from openai import AsyncOpenAI

# ===== ЛОГИ =====
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("umnik")

# ===== ОКРУЖЕНИЕ =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
PORT = int(os.getenv("PORT", "8080"))

if not TELEGRAM_TOKEN:
    raise SystemExit("TELEGRAM_TOKEN не задан. flyctl secrets set TELEGRAM_TOKEN=...")
if not OPENAI_API_KEY:
    raise SystemExit("OPENAI_API_KEY не задан. flyctl secrets set OPENAI_API_KEY=...")

# ===== OpenAI =====
client = AsyncOpenAI(api_key=OPENAI_API_KEY)

# ===== ХРАНИЛИЩЕ (SQLite в файле). На Fly можно прикрутить volume /data =====
DB_PATH = os.getenv("DB_PATH", "data/umnik.db")
os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)

def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    conn = db()
    with conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS users(
          uid INTEGER PRIMARY KEY,
          mode TEXT DEFAULT 'Обучение',
          level TEXT DEFAULT '4',
          style TEXT DEFAULT 'как ученик'
        )""")
        conn.execute("""
        CREATE TABLE IF NOT EXISTS history(
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          uid INTEGER,
          created_at TEXT,
          subject TEXT,
          mode TEXT,
          level TEXT,
          style TEXT,
          source TEXT,          -- 'text' | 'image' | 'voice'
          problem TEXT,
          solution TEXT,
          pdf BLOB
        )""")
    conn.close()

init_db()

# ===== Health-check HTTP (для Fly) =====
class _Health(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

def _run_health():
    HTTPServer(("0.0.0.0", PORT), _Health).serve_forever()

# ===== Утилиты =====
def get_or_create_user(uid: int) -> Tuple[str, str, str]:
    conn = db()
    row = conn.execute("SELECT mode, level, style FROM users WHERE uid=?", (uid,)).fetchone()
    if not row:
        with conn:
            conn.execute("INSERT INTO users(uid) VALUES(?)", (uid,))
        row = conn.execute("SELECT mode, level, style FROM users WHERE uid=?", (uid,)).fetchone()
    conn.close()
    return row["mode"], row["level"], row["style"]

def update_user(uid: int, **kwargs):
    allowed = {"mode", "level", "style"}
    sets, vals = [], []
    for k, v in kwargs.items():
        if k in allowed and v:
            sets.append(f"{k}=?")
            vals.append(v)
    if not sets:
        return
    vals.append(uid)
    conn = db()
    with conn:
        conn.execute(f"UPDATE users SET {', '.join(sets)} WHERE uid=?", vals)
    conn.close()

def add_history(uid: int, subject: str, mode: str, level: str, style: str,
                source: str, problem: str, solution: str, pdf_bytes: Optional[bytes]):
    conn = db()
    with conn:
        conn.execute("""
            INSERT INTO history(uid, created_at, subject, mode, level, style, source, problem, solution, pdf)
            VALUES(?,?,?,?,?,?,?,?,?,?)
        """, (
            uid, datetime.utcnow().isoformat(), subject, mode, level, style, source, problem, solution, pdf_bytes
        ))
    conn.close()

def fetch_history(uid: int, limit: int = 5):
    conn = db()
    rows = conn.execute("""
        SELECT id, created_at, subject, mode, level, style, source, problem, substr(solution,1,300)||'…' as solution
        FROM history WHERE uid=? ORDER BY id DESC LIMIT ?
    """, (uid, limit)).fetchall()
    conn.close()
    return rows

def fetch_history_pdf(uid: int, item_id: int) -> Optional[bytes]:
    conn = db()
    row = conn.execute("SELECT pdf FROM history WHERE uid=? AND id=?", (uid, item_id)).fetchone()
    conn.close()
    return None if not row else row["pdf"]

def detect_subject(text: str) -> str:
    """Простейшая эвристика, модель потом уточнит."""
    t = text.lower()
    if any(x in t for x in ["sin", "cos", "tg", "угол", "площадь", "уравн", "дроб", "функц", "квадрат"]):
        return "математика"
    if any(x in t for x in ["атом", "реакц", "кислота", "основан", "валент", "соль"]):
        return "химия"
    if any(x in t for x in ["скорост", "сила", "энерг", "джоуль", "ньютон", "тело", "движ"]):
        return "физика"
    if any(x in t for x in ["part of speech", "translate", "grammar", "essay"]) or "англ" in t:
        return "английский"
    if any(x in t for x in ["подлежащее", "сказуемое", "морфолог", "орфограф", "пунктуац", "сочинение"]):
        return "русский язык"
    if any(x in t for x in ["пётр", "революц", "великая", "вов", "древний", "князь", "истор"]):
        return "история"
    if any(x in t for x in ["общество", "право", "эконом", "полит", "социолог"]):
        return "обществознание"
    return "общий"

# ===== OpenAI вызовы =====
async def openai_ocr_and_solve(image_b64: str, mode: str, level: str, style: str) -> Tuple[str, str]:
    """
    Возвращает (subject, solution_md)
    """
    system = (
        "Ты обучающий помощник для школьников (ФГОС, 5–11 классы). "
        "Всегда объясняй по шагам и простыми аналогиями, без формул ради формул. "
        "Сначала кратко перепиши условие (если нужно — исправь OCR-ошибки), затем решение, затем "
        "блок 'Кратко' на 2–3 предложения. В конце предложи 1–2 проверочных вопроса."
    )
    mode_hint = {
        "Обучение": "Задавай 2–3 наводящих вопроса по ходу решения, но всё же дай полное решение.",
        "Подсказка": "Дай только подсказки и ключевые шаги без полного ответа, но с направлением.",
        "Готово": "Дай полное решение и краткое объяснение."
    }.get(mode, "Дай полное решение и краткое объяснение.")
    user = [
        {"type": "text", "text": f"Режим: {mode}. Уровень: {level}. Стиль: {style}. {mode_hint}\n"
                                 f"1) Распознай текст задания с фото.\n"
                                 f"2) Определи предмет.\n"
                                 f"3) Реши или объясни пошагово.\n"
                                 f"4) В конце выдай метку предмета в формате: [ПРЕДМЕТ: ...]."},
        {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"}}
    ]

    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    )
    out = resp.choices[0].message.content.strip()
    # Выдернем предмет
    subj = "общий"
    marker = "[ПРЕДМЕТ:"
    if marker in out:
        tail = out.split(marker, 1)[-1]
        subj = tail.split("]", 1)[0].strip().lower()
    return subj, out

async def openai_solve_text(problem: str, mode: str, level: str, style: str) -> str:
    system = (
        "Ты обучающий помощник для школьников (ФГОС, 5–11 классы). "
        "Объясняй по шагам, простыми словами, с бытовыми аналогиями. "
        "Структура ответа: Условие (кратко) → Решение по шагам → Кратко (2–3 предложения) → "
        "Проверочные вопросы (1–2)."
    )
    mode_hint = {
        "Обучение": "Задавай по ходу 2–3 наводящих вопроса, но далее покажи правильный ход мыслей.",
        "Подсказка": "Дай 3–5 подсказок и направления без полного ответа.",
        "Готово": "Дай полное решение и краткое объяснение."
    }.get(mode, "Дай полное решение и краткое объяснение.")
    user = (
        f"Режим: {mode}. Уровень: {level}. Стиль: {style}. {mode_hint}\n"
        f"Задание:\n{problem}"
    )
    resp = await client.chat.completions.create(
        model="gpt-4o-mini",
        temperature=0.2,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user}
        ]
    )
    return resp.choices[0].message.content.strip()

async def openai_transcribe(file_bytes: bytes, mime: str) -> str:
    # Whisper-1 стабилен для STT
    return (await client.audio.transcriptions.create(
        model="whisper-1",
        file=("audio", file_bytes, mime),
        response_format="text",
        temperature=0.0,
        language="ru"
    ))

async def openai_tts(text: str) -> bytes:
    # Озвучка объяснения
    speech = await client.audio.speech.create(
        model="tts-1",
        voice="alloy",
        input=text
    )
    return speech.read()

# ===== PDF экспорт =====
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import cm
from reportlab.pdfgen import canvas

def make_pdf(title: str, problem: str, solution: str) -> bytes:
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=A4)
    width, height = A4
    x, y = 2*cm, height-2*cm

    def txt(s: str, size=11, leading=14):
        nonlocal y
        c.setFont("Helvetica", size)
        for line in s.splitlines():
            for sub in _wrap(line, 90):
                c.drawString(x, y, sub)
                y -= leading
                if y < 2*cm:
                    c.showPage(); y = height-2*cm; c.setFont("Helvetica", size)

    def _wrap(s, n):
        return [s[i:i+n] for i in range(0, len(s), n)]

    c.setTitle(title)
    c.setFont("Helvetica-Bold", 14); c.drawString(x, y, title); y -= 22
    c.setFont("Helvetica-Bold", 12); c.drawString(x, y, "Условие:"); y -= 18
    txt(problem, 11, 15); y -= 8
    c.setFont("Helvetica-Bold", 12); c.drawString(x, y, "Решение:"); y -= 18
    txt(solution, 11, 15)
    c.showPage(); c.save()
    return buf.getvalue()

# ===== Команды =====
async def set_commands(app: Application):
    await app.bot.set_my_commands([
        BotCommand("start", "Приветствие"),
        BotCommand("help", "Как пользоваться"),
        BotCommand("mode", "Режим: Обучение/Подсказка/Готово"),
        BotCommand("level", "Уровень: 3/4/5"),
        BotCommand("style", "Стиль: как ученик/углублённо"),
        BotCommand("solve", "Решить текстовое задание"),
        BotCommand("essay", "Сочинение/эссе"),
        BotCommand("doc", "Доклад (тема)"),
        BotCommand("pres", "Презентация (кратко)"),
        BotCommand("history", "Показать последние решения"),
        BotCommand("saylast", "Озвучить последнее объяснение"),
    ])

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mode, level, style = get_or_create_user(uid)
    await update.message.reply_text(
        "👋 Привет! Я «Умник» — помогу с ДЗ так, чтобы ты понял.\n\n"
        "➕ Сфоткай задание — распознаю и объясню.\n"
        "🎙 Можешь прислать голосом.\n"
        "⚙ /mode /level /style — настрой режим помощи.\n"
        "🗂 /history — последние решения.\n\n"
        f"Текущие настройки: режим *{mode}*, уровень *{level}*, стиль *{style}*.",
        parse_mode="Markdown"
    )

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Как пользоваться:\n"
        "1) Сделай фото задания и пришли сюда.\n"
        "2) Или пришли текст /solve ТЕКСТ.\n"
        "3) /mode Обучение|Подсказка|Готово — выбирай формат помощи.\n"
        "4) /level 3|4|5 — глубина ответа.\n"
        "5) /style «как ученик»|«углублённо» — стиль.\n"
        "6) Голос — продиктуй задание (я распознаю)."
    )

async def mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        val = " ".join(context.args).strip().capitalize()
        if val in ["Обучение", "Подсказка", "Готово"]:
            update_user(uid, mode=val)
            await update.message.reply_text(f"Режим установлен: {val}")
            return
    await update.message.reply_text("Укажи один из режимов: Обучение / Подсказка / Готово")

async def level_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args and context.args[0] in ["3", "4", "5"]:
        update_user(uid, level=context.args[0])
        await update.message.reply_text(f"Уровень установлен: {context.args[0]}")
    else:
        await update.message.reply_text("Укажи уровень: 3 / 4 / 5")

async def style_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if context.args:
        val = " ".join(context.args).strip().lower()
        if val in ["как ученик", "углублённо"]:
            update_user(uid, style=val)
            await update.message.reply_text(f"Стиль установлен: {val}")
            return
    await update.message.reply_text('Укажи стиль: "как ученик" или "углублённо"')

async def solve_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mode, level, style = get_or_create_user(uid)
    problem = " ".join(context.args).strip()
    if not problem:
        await update.message.reply_text("Дай текст задания: /solve ТЕКСТ_ЗАДАНИЯ")
        return
    await update.message.chat.send_action("typing")
    try:
        subject = detect_subject(problem)
        solution = await openai_solve_text(problem, mode, level, style)
        pdf = make_pdf(f"Умник — {subject}", problem, solution)
        add_history(uid, subject, mode, level, style, "text", problem, solution, pdf)
        await update.message.reply_text(solution[:4000])
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename="solution.pdf"),
            caption="Экспорт в PDF"
        )
    except Exception as e:
        log.exception("solve_cmd")
        await update.message.reply_text(f"Не получилось решить: {e}")

async def essay_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text("Укажи тему: /essay ТЕМА")
        return
    await update.message.chat.send_action("typing")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.7,
            messages=[
                {"role": "system", "content": "Пиши сочинение 180–220 слов, по-русски, без плагиата, логично и структурно."},
                {"role": "user", "content": f"Тема: {topic}"}
            ]
        )
        out = resp.choices[0].message.content.strip()
        await update.message.reply_text(out[:4000])
    except Exception as e:
        log.exception("essay")
        await update.message.reply_text(f"Сбой при генерации: {e}")

async def doc_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text("Укажи тему: /doc ТЕМА")
        return
    await update.message.chat.send_action("typing")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            messages=[
                {"role": "system", "content": "Сделай краткий доклад 350–500 слов, с подзаголовками и тезисами, по-русски."},
                {"role": "user", "content": f"Тема доклада: {topic}"}
            ]
        )
        out = resp.choices[0].message.content.strip()
        pdf = make_pdf(f"Доклад — {topic}", topic, out)
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename="report.pdf"),
            caption="Доклад в PDF"
        )
    except Exception as e:
        log.exception("doc")
        await update.message.reply_text(f"Сбой при докладе: {e}")

async def pres_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    topic = " ".join(context.args).strip()
    if not topic:
        await update.message.reply_text("Укажи тему: /pres ТЕМА")
        return
    await update.message.chat.send_action("typing")
    try:
        resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.5,
            messages=[
                {"role": "system", "content": "Сделай 6–8 слайдов-прослайдовку в виде Markdown: #Title + bullets."},
                {"role": "user", "content": f"Тема презентации: {topic}"}
            ]
        )
        md = resp.choices[0].message.content.strip()
        pdf = make_pdf(f"Презентация — {topic}", topic, md)
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename="presentation.pdf"),
            caption="Презентация (PDF-версия)"
        )
    except Exception as e:
        log.exception("pres")
        await update.message.reply_text(f"Сбой при презентации: {e}")

async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mode, level, style = get_or_create_user(uid)
    await update.message.chat.send_action("typing")
    try:
        photo = update.message.photo[-1]
        f = await context.bot.get_file(photo.file_id)
        bio = io.BytesIO()
        await f.download_to_memory(out=bio)
        b64 = base64.b64encode(bio.getvalue()).decode("ascii")

        subject, solution = await openai_ocr_and_solve(b64, mode, level, style)
        # OCR-извлечение условия для PDF (просим модель отдельно)
        p_resp = await client.chat.completions.create(
            model="gpt-4o-mini",
            temperature=0.0,
            messages=[
                {"role": "system", "content": "Из ответа выдели краткое условие задачи (2–4 строки), без решения."},
                {"role": "user", "content": solution}
            ]
        )
        problem = p_resp.choices[0].message.content.strip()
        pdf = make_pdf(f"Умник — {subject}", problem, solution)
        add_history(uid, subject, mode, level, style, "image", problem, solution, pdf)

        await update.message.reply_text(solution[:4000])
        await update.message.reply_document(
            document=InputFile(io.BytesIO(pdf), filename="solution.pdf"),
            caption="Экспорт в PDF"
        )
    except Exception as e:
        log.exception("photo")
        await update.message.reply_text(f"Не смог обработать фото: {e}")

async def on_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    mode, level, style = get_or_create_user(uid)
    await update.message.chat.send_action("typing")
    try:
        voice = update.message.voice or update.message.audio
        if not voice:
            return
        f = await context.bot.get_file(voice.file_id)
        bio = io.BytesIO()
        await f.download_to_memory(out=bio)
        # Telegram обычно присылает OGG/OPUS; для whisper-1 нормально.
        transcript = await openai_transcribe(bio.getvalue(), mime="audio/ogg")
        subject = detect_subject(transcript)
        solution = await openai_solve_text(transcript, mode, level, style)
        pdf = make_pdf(f"Умник — {subject}", transcript, solution)
        add_history(uid, subject, mode, level, style, "voice", transcript, solution, pdf)
        await update.message.reply_text(solution[:4000])
    except Exception as e:
        log.exception("voice")
        await update.message.reply_text(f"С голосом не вышло: {e}")

async def history_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    rows = fetch_history(uid, limit=5)
    if not rows:
        await update.message.reply_text("История пуста. Пришли фото или /solve текст.")
        return
    text = ["Последние решения:"]
    for r in rows:
        text.append(
            f"#{r['id']} • {r['created_at'][:19]} • {r['subject']} • {r['mode']}/{r['level']}/{r['style']} • "
            f"{r['source']}\n— {r['problem'][:80].replace(os.linesep,' ')}"
        )
    await update.message.reply_text("\n\n".join(text[:4096]))

async def saylast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    conn = db()
    row = conn.execute(
        "SELECT id, solution FROM history WHERE uid=? ORDER BY id DESC LIMIT 1", (uid,)
    ).fetchone()
    conn.close()
    if not row:
        await update.message.reply_text("Пока нет записей в истории.")
        return
    try:
        audio_bytes = await openai_tts(row["solution"][:2000])
        await update.message.reply_voice(voice=InputFile(io.BytesIO(audio_bytes), filename="explain.ogg"))
    except Exception as e:
        log.exception("tts")
        await update.message.reply_text(f"Не смог озвучить: {e}")

async def exportpdf_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if not context.args:
        await update.message.reply_text("Укажи ID из /history: /exportpdf 12")
        return
    try:
        item_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID должен быть числом. Пример: /exportpdf 12")
        return
    pdf = fetch_history_pdf(uid, item_id)
    if not pdf:
        await update.message.reply_text("Не нашёл PDF по этому ID.")
        return
    await update.message.reply_document(
        document=InputFile(io.BytesIO(pdf), filename=f"solution_{item_id}.pdf"),
        caption="Экспорт из истории"
    )

def main():
    threading.Thread(target=_run_health, daemon=True).start()

    app = Application.builder().token(TELEGRAM_TOKEN).build()
    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("mode", mode_cmd))
    app.add_handler(CommandHandler("level", level_cmd))
    app.add_handler(CommandHandler("style", style_cmd))
    app.add_handler(CommandHandler("solve", solve_cmd))
    app.add_handler(CommandHandler("essay", essay_cmd))
    app.add_handler(CommandHandler("doc", doc_cmd))
    app.add_handler(CommandHandler("pres", pres_cmd))
    app.add_handler(CommandHandler("history", history_cmd))
    app.add_handler(CommandHandler("saylast", saylast_cmd))
    app.add_handler(CommandHandler("exportpdf", exportpdf_cmd))

    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, on_voice))

    log.info("Umnik bot is running…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()

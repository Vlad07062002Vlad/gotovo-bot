import asyncio
import base64
import os
from io import BytesIO
from telegram import Update, BotCommand
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes
import openai

# === ТВОИ КЛЮЧИ ЧЕРЕЗ ПЕРЕМЕННЫЕ ОКРУЖЕНИЯ ===
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")

# Инициализация OpenAI
client = openai.AsyncOpenAI(api_key=OPENAI_API_KEY)

# Устанавливаем меню команд
async def set_commands(app):
    await app.bot.set_my_commands([
        BotCommand("start", "🚀 Начать"),
        BotCommand("help", "ℹ️ Помощь"),
        BotCommand("photo", "📷 Как прислать задание"),
        BotCommand("essay", "📝 Написать сочинение"),
        BotCommand("explain", "🧠 Объяснить тему"),
    ])

# /start
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Привет! 👋 Я — *Готово!* 🤖\n"
        "Твой ИИ-репетитор, который помогает с домашкой.\n\n"
        "Выбери, что нужно:\n"
        "📷 Сфоткай задание\n"
        "📝 Напиши сочинение\n"
        "🧠 Объясни тему\n\n"
        "Используй меню ниже 👇",
        parse_mode='Markdown'
    )

# /help
async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "💡 Как я помогу:\n\n"
        "1️⃣ Чтобы решить задачу — пришли **фото задания**\n"
        "2️⃣ Чтобы написать сочинение — напиши: `/essay тема`\n"
        "3️⃣ Чтобы объяснить тему — напиши: `/explain что непонятно`\n\n"
        "Я отвечу быстро и понятно — как старший брат 😎"
        parse_mode='Markdown'
    )

# /photo
async def photo_hint(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📸 Сфоткай задание в тетради или учебнике и отправь сюда.\n"
        "Я решу и объясню — за 20 секунд."
    )

# /essay
async def essay_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = " ".join(context.args)
    if not user_input:
        await update.message.reply_text("Напиши тему, например:\n`/essay Смысл рассказа 'Муму'`", parse_mode='Markdown')
        return

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "Ты — ИИ-репетитор. Пиши сочинение на 150–200 слов, как школьник 8 класса. Просто, по делу, без воды."
                },
                {
                    "role": "user",
                    "content": f"Напиши сочинение на тему: {user_input}"
                }
            ],
            max_tokens=500
        )
        essay = response.choices[0].message.content
        await update.message.reply_text(essay)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# /explain
async def explain_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_input = " ".join(context.args)
    if not user_input:
        await update.message.reply_text("Что нужно объяснить? Например:\n`/explain как решать квадратные уравнения`", parse_mode='Markdown')
        return

    try:
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "system",
                    "content": "Ты — ИИ-репетитор. Объясняй просто, как старший брат. Без сложных слов. С примером из жизни."
                },
                {
                    "role": "user",
                    "content": f"Объясни, как если бы мне 14 лет: {user_input}"
                }
            ],
            max_tokens=700
        )
        explanation = response.choices[0].message.content
        await update.message.reply_text(explanation)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# Обработка фото
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔍 Анализирую задание...")

    # Скачиваем фото
    photo_file = await update.message.photo[-1].get_file()
    image_bytes = await photo_file.download_as_bytearray()
    image_base64 = base64.b64encode(image_bytes).decode('utf-8')

    try:
        # Отправляем в GPT-4o
        response = await client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Ты — ИИ-репетитор для школьника. "
                                "Объясняй просто, как старший брат. "
                                "Реши задание и объясни по шагам. "
                                "Если это сочинение — напиши кратко, на 150 слов. "
                                "Не используй сложные термины. "
                                "В конце добавь: 💡 Совет: ..."
                            ),
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/jpeg;base64,{image_base64}"
                            },
                        },
                    ],
                }
            ],
            max_tokens=1000,
        )
        ai_response = response.choices[0].message.content
        await update.message.reply_text(ai_response)
    except Exception as e:
        await update.message.reply_text(f"❌ Ошибка: {str(e)}")

# Запуск
def main():
    print("🚀 Бот 'Готово!' запущен в облаке!")
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.post_init = set_commands

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("photo", photo_hint))
    app.add_handler(CommandHandler("essay", essay_command))
    app.add_handler(CommandHandler("explain", explain_command))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    app.run_polling()

if __name__ == '__main__':
    main()

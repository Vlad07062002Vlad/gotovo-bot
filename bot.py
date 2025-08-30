# =========[ БЛОК 6/6 — ФИНАЛ ]=================================================
# Платежи (Stars + bePaid) уже подключены выше.
# Здесь: health-сервер старт, error-handler, рег. хэндлеров, main()

# --- Фолбэк-классификатор предмета (если не определён выше) ---
if "classify_subject" not in globals():
    try:
        from services.classifier import classify_subject as _clf_subject  # type: ignore
        async def classify_subject(text: str) -> str:
            try:
                return await _clf_subject(text)
            except Exception:
                return "auto"
    except Exception:
        async def classify_subject(text: str) -> str:
            return "auto"

# --- Единый error-handler телеграм-бота ---
async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    try:
        log.exception("Unhandled error in handler", exc_info=context.error)
        if isinstance(update, Update) and update.effective_message:
            await update.effective_message.reply_text("⚠️ Упс, что-то пошло не так. Попробуй ещё раз.")
    except Exception:
        pass

# --- Старт health-сервера и авто-сейва метрик ---
def _start_health_and_metrics():
    # health: отдельный поток с собственным event loop
    port = int(os.getenv("HEALTH_PORT", os.getenv("PORT", "8080")))
    ht = _HealthThread(port)
    ht.start()

    # автосохранение метрик
    t = threading.Thread(target=_stats_autosave_loop, name="stats-autosave", daemon=True)
    t.start()

    return ht, t

# --- Регистрация всех хэндлеров ---
def _register_handlers(app: Application):
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

    # Callback-кнопки
    app.add_handler(CallbackQueryHandler(on_admin_callback, pattern=r"^admin:"))
    app.add_handler(CallbackQueryHandler(on_buy_stars_cb, pattern=r"^buy_stars"))

    # Фото и тексты
    app.add_handler(MessageHandler(f.PHOTO | f.Document.IMAGE, handle_photo))
    app.add_handler(MessageHandler(f.TEXT & ~f.COMMAND, on_text))

    # Ошибки
    app.add_error_handler(on_error)

# --- MAIN ---
def main():
    token = os.getenv("TELEGRAM_TOKEN")
    if not token:
        raise SystemExit("Нет TELEGRAM_TOKEN (fly secrets set TELEGRAM_TOKEN=...)")

    # Грузим накопленные метрики, поднимаем служебные потоки
    stats_load()
    _start_health_and_metrics()

    # Билдим приложение Telegram
    app = Application.builder().token(token).concurrent_updates(True).build()

    # Команды в меню
    try:
        app.job_queue.run_once(lambda *_: None, when=0.0)  # «прогрев» JobQueue от PTB
        app.create_task(set_commands(app))
    except Exception as e:
        log.warning(f"set_commands failed: {e}")

    # Хэндлеры
    _register_handlers(app)

    log.info("Bot is starting (long-polling). Health on port %s", os.getenv("HEALTH_PORT", os.getenv("PORT", "8080")))
    # В проде на Fly.io оставляем long-polling: health-сервер держит открытый HTTP-порт для liveness.
    app.run_polling(close_loop=False, drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("Shutdown requested by user")
    except Exception:
        log.exception("Fatal in main")
        raise
# =========[ КОНЕЦ БЛОКА 6/6 ]==================================================

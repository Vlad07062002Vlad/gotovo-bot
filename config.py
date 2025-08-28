# config.py — простая обёртка над ENV (без дубликатов)
import os
from typing import List

def _get_bool(name: str, default: bool=False) -> bool:
    v = os.getenv(name)
    if v is None:
        return default
    return v.strip().lower() in ("1","true","yes","y","on")

class Settings:
    # Базовые ключи
    telegram_token: str = os.getenv("TELEGRAM_TOKEN", "")
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    sqlite_path: str = os.getenv("SQLITE_PATH", "/data/app.db")

    # Лимиты / планы
    freemium_daily: int = int(os.getenv("FREEMIUM_DAILY", "3"))
    pro_trial_daily: int = int(os.getenv("PRO_TRIAL_DAILY", "1"))
    subscription_limit: int = int(os.getenv("SUBSCRIPTION_LIMIT", "600"))
    admin_ids: List[int] = [int(x) for x in os.getenv("ADMIN_IDS","").split(",") if x.strip().isdigit()]

    # Оплаты
    telegram_stars_enabled: bool = _get_bool("TELEGRAM_STARS_ENABLED", True)
    telegram_provider_token: str = os.getenv("TELEGRAM_PROVIDER_TOKEN", "")

    card_checkout_url: str = os.getenv("CARD_CHECKOUT_URL", "")
    erip_checkout_url: str = os.getenv("ERIP_CHECKOUT_URL", "")
    card_webhook_secret: str = os.getenv("CARD_WEBHOOK_SECRET", "")
    erip_webhook_secret: str = os.getenv("ERIP_WEBHOOK_SECRET", "")

# Рендер формул и OCR-лабы (флаги)
RENDER_TEX = _get_bool("RENDER_TEX", False)
USE_MATHPIX = _get_bool("USE_MATHPIX", False)
MATHPIX_APP_ID  = os.getenv("MATHPIX_APP_ID", "")
MATHPIX_APP_KEY = os.getenv("MATHPIX_APP_KEY", "")

settings = Settings()

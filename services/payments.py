# services/payments.py — витрина и применение платежей
from typing import Optional
from telegram import InlineKeyboardMarkup, InlineKeyboardButton
from services.usage import add_credits, activate_sub

# Витрина (BYN). Stars — чуть дороже (комиссии магазинов).
BUY_PRESETS = {
    "credits": [
        ("CREDITS_50",   "50 кредитов",    "5 BYN",  "6 BYN"),   # (карта/ЕРИП, Stars)
        ("CREDITS_200",  "200 кредитов",  "18 BYN", "22 BYN"),
        ("CREDITS_1000", "1000 кредитов", "80 BYN", "99 BYN"),
    ],
    "subs": [
        ("SUB_MONTH", "Подписка Pro (1 мес)", "39 BYN", "49 BYN"),
    ]
}

def build_buy_keyboard(stars_enabled: bool, card_url: Optional[str], erip_url: Optional[str]) -> InlineKeyboardMarkup:
    rows = []
    # Кредиты
    for pid, title, price_card, price_stars in BUY_PRESETS["credits"]:
        btns = []
        if stars_enabled:
            btns.append(InlineKeyboardButton(f"⭐ {title} · {price_stars}", callback_data=f"buy_stars:{pid}"))
        if card_url:
            btns.append(InlineKeyboardButton(f"💳 {title} · {price_card}", url=card_url))
        if erip_url:
            btns.append(InlineKeyboardButton(f"ЕРИП {title}", url=erip_url))
        rows.append(btns)
    # Подписка
    for pid, title, price_card, price_stars in BUY_PRESETS["subs"]:
        btns = []
        if stars_enabled:
            btns.append(InlineKeyboardButton(f"⭐ {title} · {price_stars}", callback_data=f"buy_stars:{pid}"))
        if card_url:
            btns.append(InlineKeyboardButton(f"💳 {title} · {price_card}", url=card_url))
        if erip_url:
            btns.append(InlineKeyboardButton(f"ЕРИП {title}", url=erip_url))
        rows.append(btns)
    return InlineKeyboardMarkup(rows)

def apply_payment_payload(user_id: int, payload: str) -> str:
    if payload.startswith("CREDITS_"):
        n = int(payload.split("_")[1])
        add_credits(user_id, n)
        return f"Зачислено {n} кредитов."
    if payload == "SUB_MONTH":
        activate_sub(user_id, 31)
        return "Подписка Pro активирована на 31 день."
    return "Платёж зачтён."

def get_stars_amount(payload: str) -> int:
    """Вернуть стоимость товара в Stars по идентификатору payload."""
    if payload.startswith("CREDITS_"):
        for pid, _, _, price_stars in BUY_PRESETS["credits"]:
            if pid == payload:
                return int(price_stars.split()[0]) * 10
    if payload.startswith("SUB_"):
        for pid, _, _, price_stars in BUY_PRESETS["subs"]:
            if pid == payload:
                return int(price_stars.split()[0]) * 10
    raise ValueError("Unknown payload")

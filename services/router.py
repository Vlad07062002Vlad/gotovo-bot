# services/router.py — гибридный выбор модели для R1+VDB (4o-mini / o4-mini / 4o)
from typing import Tuple

HEAVY_MARKERS = (
    "докажи","обоснуй","подробно","по шагам","поиндукции",
    "уравнение","система","дробь","производная","интеграл",
    "доказать","программа","алгоритм","код"
)

def select_model(prompt: str, mode: str) -> Tuple[str, int, str]:
    """
    Возвращает (model, max_tokens, tag)
      tag: '4o-mini'|'o4-mini'|'4o'
    Правила:
      - free  -> gpt-4o-mini (жёсткий кап)
      - trial -> gpt-4o (средний кап)
      - paid  -> эвристика: o4-mini для логики/математики/длинного ввода, иначе 4o-mini
    """
    p = (prompt or "").lower()
    if mode == "free":
        return "gpt-4o-mini", 700, "4o-mini"
    if mode == "trial":
        return "gpt-4o", 1200, "4o"
    # paid (credit/sub)
    long_input = len(p) > 600
    heavy = long_input or any(k in p for k in HEAVY_MARKERS)
    if heavy:
        return "o4-mini", 1100, "o4-mini"
    return "gpt-4o-mini", 900, "4o-mini"

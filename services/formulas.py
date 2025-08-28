# services/formulas.py — формулы: prettify + опциональный рендер TeX→PNG
from __future__ import annotations
import io, re
from typing import List, Tuple

_SUPERS = str.maketrans("0123456789+-=()n", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿ")
_SUBS   = str.maketrans("0123456789+-=()aeoxhklmnpst", "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₕₖₗₘₙₚₛₜ")

def _sup(s: str) -> str: return "".join(ch.translate(_SUPERS) for ch in s)
def _sub(s: str) -> str: return "".join(ch.translate(_SUBS)   for ch in s)

def prettify_chem(text: str) -> str:
    # H2O, SO4^2-, Fe3+ → H₂O, SO₄²⁻, Fe³⁺
    t = re.sub(r'([A-Za-zА-Яа-я])(\d{1,3})', lambda m: m.group(1) + _sub(m.group(2)), text)
    t = re.sub(r'\^([0-9+-]+)',       lambda m: _sup(m.group(1)), t)   # ^2-, ^+, ^3
    return t

def prettify_math(text: str) -> str:
    t = text.replace("sqrt(", "√(").replace("+-", "±")
    # x^2, a^(n+1) → x², aⁿ⁺¹
    t = re.sub(r'([A-Za-zА-Яа-я0-9\)])\^(\d+)', lambda m: m.group(1) + _sup(m.group(2)), t)
    t = re.sub(r'\^\(([^)]+)\)',      lambda m: _sup(m.group(1)), t)
    # немного нормализуем интегралы
    t = t.replace("∫ ", "∫")
    return t

def postprocess_formulas(text: str) -> str:
    if not text: return text
    return prettify_math(prettify_chem(text))

# --------- опциональный рендер TeX→PNG (без TeX-сервера) ----------
def extract_tex_snippets(text: str) -> List[str]:
    """Ищем строки вида `TeX: ...`; при желании можно расширить на $...$."""
    out = []
    for line in text.splitlines():
        m = re.match(r'\s*TeX:\s*(.+)', line)
        if m:
            out.append(m.group(1).strip())
    return out

def render_tex_png(tex: str) -> bytes:
    from matplotlib import mathtext
    from matplotlib.font_manager import FontProperties
    from PIL import Image
    import numpy as np
    parser = mathtext.MathTextParser("agg")
    ft = FontProperties(size=22)
    rgba, _ = parser.to_rgba(f"${tex}$", dpi=220, fontset="stix", fontsize=22)
    img = Image.fromarray((rgba * 255).astype("uint8"))
    bbox = img.getbbox()
    if bbox: img = img.crop(bbox)
    buf = io.BytesIO(); img.save(buf, format="PNG")
    return buf.getvalue()

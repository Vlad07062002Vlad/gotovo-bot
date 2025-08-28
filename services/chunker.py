# services/chunker.py — извлечение текста из PDF и нарезка в чанки для ВБД
from __future__ import annotations
import os, re, json, hashlib
from pathlib import Path
from typing import Iterable, Dict, List
from pdfminer.high_level import extract_pages
from pdfminer.layout import LTTextContainer

def _clean_text(s: str) -> str:
    s = s.replace("\xa0"," ").replace("\t"," ")
    s = re.sub(r'[ \u200b]{2,}', ' ', s)
    return s.strip()

def read_pdf_pages(path: str) -> List[str]:
    pages = []
    for page_layout in extract_pages(path):
        texts = []
        for element in page_layout:
            if isinstance(element, LTTextContainer):
                texts.append(element.get_text())
        pages.append(_clean_text("\n".join(texts)))
    return pages

def chunk_text(txt: str, size=900, overlap=120) -> List[str]:
    txt = re.sub(r'\n{3,}', '\n\n', txt)
    paras = re.split(r'\n{2,}', txt)
    chunks, cur = [], ""
    for p in paras:
        if len(cur) + len(p) + 1 <= size:
            cur = (cur + "\n\n" + p).strip()
        else:
            if cur: chunks.append(cur)
            cur = p
    if cur: chunks.append(cur)
    # добавим перекрытие (грубо)
    if overlap and len(chunks) > 1:
        joined = []
        for i, ch in enumerate(chunks):
            prev = chunks[i-1][-overlap:] if i>0 else ""
            joined.append((prev + " " + ch).strip())
        chunks = joined
    return chunks

def file_to_items(pdf_path: Path, subject: str, grade: str) -> List[Dict]:
    pages = read_pdf_pages(str(pdf_path))
    items = []
    book = pdf_path.name
    for pnum, page in enumerate(pages, start=1):
        for j, ch in enumerate(chunk_text(page)):
            uid = f"{subject}/{grade}/{book}#{pnum:03d}-{j:02d}"
            hid = hashlib.md5(uid.encode()).hexdigest()
            items.append({
                "id": hid,
                "text": ch,
                "meta": {"subject": subject, "grade": grade, "book": book, "page": pnum}
            })
    return items

def walk_pdfs(root: str) -> Iterable[Path]:
    for p in Path(root).rglob("*.clean.pdf"):
        yield p

def guess_subject_grade(path: Path) -> (str, str):
    # ожидаем /data/pdfs/<subject>/<grade>/<file.clean.pdf>
    subject = path.parts[-3]; grade = path.parts[-2]
    return subject, grade

def build_rules_batch(root="/data/pdfs", out_json="data_out/rules_batch.json"):
    Path(Path(out_json).parent).mkdir(parents=True, exist_ok=True)
    all_items = []
    for pdf in walk_pdfs(root):
        subj, grade = guess_subject_grade(pdf)
        all_items.extend(file_to_items(pdf, subj, grade))
    with open(out_json, "w", encoding="utf-8") as f:
        json.dump({"items": all_items}, f, ensure_ascii=False)
    return out_json, len(all_items)

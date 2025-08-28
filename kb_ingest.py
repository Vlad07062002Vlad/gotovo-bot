# kb_ingest.py — импорт заранее подготовленных правил (JSONL) в Qdrant
import asyncio, json, orjson, os, glob
from openai import AsyncOpenAI
from rag_vdb import upsert_rules

AI = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))

def read_jsonl(path):
    for line in open(path, "r", encoding="utf-8"):
        line=line.strip()
        if not line: continue
        try:
            yield orjson.loads(line)
        except:
            yield json.loads(line)

async def main():
    base = os.getenv("KB_DIR", "data/kb")
    files = glob.glob(os.path.join(base, "**/*.jsonl"), recursive=True)
    total=0
    for f in files:
        batch=[]
        for rec in read_jsonl(f):
            # ожидаем поля: id, subject, grade, book, chapter, page, rule_brief (≤40 слов)
            if not rec.get("rule_brief"): continue
            batch.append(rec)
            if len(batch)>=256:
                await upsert_rules(AI, batch); total+=len(batch); batch=[]
        if batch:
            await upsert_rules(AI, batch); total+=len(batch)
        print(f"[OK] {f}: импортировано")
    print(f"Готово. Всего импортировано правил: {total}")

if __name__ == "__main__":
    asyncio.run(main())

# rag_vdb.py — Qdrant (embedded) + OpenAI embeddings (1536)
import os, json
from typing import List, Dict, Optional
from qdrant_client import QdrantClient
from qdrant_client.models import VectorParams, Distance, PointStruct, Filter, FieldCondition, MatchValue
from openai import AsyncOpenAI

EMBED_MODEL = os.getenv("EMBED_MODEL", "text-embedding-3-small")
VDB_PATH    = os.getenv("VDB_PATH", "/data/vdb")
COLL        = os.getenv("VDB_COLLECTION", "school_rules")
DIM         = 1536  # text-embedding-3-*

_client: Optional[QdrantClient] = None

def vdb() -> QdrantClient:
    global _client
    if _client is None:
        os.makedirs(VDB_PATH, exist_ok=True)
        _client = QdrantClient(path=VDB_PATH)
        # ensure collection
        cols = [c.name for c in _client.get_collections().collections]
        if COLL not in cols:
            _client.recreate_collection(COLL, vectors_config=VectorParams(size=DIM, distance=Distance.COSINE))
    return _client

async def embed_texts(ai: AsyncOpenAI, texts: List[str]) -> List[List[float]]:
    # batched embeddings
    resp = await ai.embeddings.create(model=EMBED_MODEL, input=texts)
    return [d.embedding for d in resp.data]

async def upsert_rules(ai: AsyncOpenAI, rules: List[Dict]):
    """
    rule item:
      {"id": "math_7_2021_algebra_p045_r01",
       "rule_brief": "25–40 слов своими словами...",
       "subject":"math","grade":7,"book":"Алгебра 7 (Иванов, 2021)","chapter":"Линейные уравнения","page":45}
    """
    vecs = await embed_texts(ai, [r["rule_brief"] for r in rules])
    points = []
    for r, v in zip(rules, vecs):
        payload = {
            "rule_brief": r["rule_brief"],
            "subject": r["subject"], "grade": r["grade"],
            "book": r["book"], "chapter": r.get("chapter",""), "page": r.get("page", None),
            "topic": r.get("topic","")
        }
        points.append(PointStruct(id=r["id"], vector=v, payload=payload))
    vdb().upsert(COLL, points=points)

async def search_rules(ai: AsyncOpenAI, query: str, subject: str, grade: int, top_k=5) -> List[Dict]:
    qv = (await embed_texts(ai, [query]))[0]
    flt = Filter(must=[
        FieldCondition(key="subject", match=MatchValue(value=subject)),
        FieldCondition(key="grade",   match=MatchValue(value=int(grade))),
    ])
    res = vdb().search(COLL, query_vector=qv, query_filter=flt, limit=top_k, with_payload=True)
    out=[]
    for r in res:
        p = r.payload or {}
        out.append({
            "score": r.score,
            "book": p.get("book",""),
            "chapter": p.get("chapter",""),
            "page": p.get("page", None),
            "rule_brief": p.get("rule_brief","")
        })
    return out

def clamp_words(s: str, max_words=40) -> str:
    w = (s or "").split()
    return " ".join(w[:max_words]).rstrip(",.;:") + ("…" if len(w) > max_words else "")

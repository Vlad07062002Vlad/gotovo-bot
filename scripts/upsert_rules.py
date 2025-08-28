# upsert_rules.py — надёжная заливка правил в ВБД с батчами, ретраями и резюмом
from __future__ import annotations
import os, sys, json, time, gzip, math, signal, hashlib, argparse
from pathlib import Path
from typing import List, Dict, Iterable, Tuple
import requests

DEFAULT_URL = os.getenv("UPSERT_URL", "").strip()
DEFAULT_SECRET = os.getenv("VDB_WEBHOOK_SECRET", "").strip()
DEFAULT_TIMEOUT = int(os.getenv("UPSERT_TIMEOUT", "1200"))
CHECKPOINT = Path(os.getenv("UPSERT_CHECKPOINT", "data_out/upsert_checkpoint.json"))

def _read_json_any(path: Path) -> dict:
    data: bytes
    if path.suffix == ".gz":
        with gzip.open(path, "rb") as f:
            data = f.read()
    else:
        data = path.read_bytes()
    try:
        return json.loads(data.decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"Bad JSON in {path}: {e}")

def _iter_sources(src: Path) -> Iterable[Path]:
    if src.is_file():
        yield src
        return
    for p in src.rglob("*.json"):
        yield p
    for p in src.rglob("*.json.gz"):
        yield p

def _normalize_items(obj: dict) -> List[Dict]:
    items = obj.get("rules") or obj.get("items") or []
    if not isinstance(items, list) or not items:
        return []
    out = []
    for r in items:
        if not isinstance(r, dict): 
            continue
        # минимальная валидация
        id_ = r.get("id")
        brief = (r.get("rule_brief") or "").strip()
        subj = r.get("subject")
        grade = r.get("grade")
        book = r.get("book")
        if not (id_ and isinstance(id_, (str,int))):
            continue
        if not (brief and subj and (grade is not None) and book):
            continue
        out.append({
            "id": str(id_),
            "rule_brief": brief,
            "subject": str(subj),
            "grade": int(grade),
            "book": str(book),
            "chapter": r.get("chapter") or "",
            "page": r.get("page", None),
            "topic": r.get("topic", "")
        })
    return out

def _load_all_items(src: Path) -> List[Dict]:
    total = []
    for p in _iter_sources(src):
        try:
            obj = _read_json_any(p)
            part = _normalize_items(obj)
            if part:
                total.extend(part)
                print(f"[+] {p} → {len(part)}")
            else:
                print(f"[!] {p} → 0 (no items)")
        except Exception as e:
            print(f"[ERR] {p}: {e}")
    # дедуп по id
    seen = set()
    uniq = []
    for r in total:
        if r["id"] in seen: 
            continue
        seen.add(r["id"])
        uniq.append(r)
    print(f"[OK] total items: {len(total)}, unique by id: {len(uniq)}")
    return uniq

def _post_batch(url: str, secret: str, batch: List[Dict], timeout: int) -> Tuple[int,str]:
    body = json.dumps({"rules": batch}, ensure_ascii=False).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "X-Auth": secret
    }
    resp = requests.post(url, headers=headers, data=body, timeout=timeout)
    return resp.status_code, (resp.text or "")

def _save_checkpoint(done_ids: List[str], src_sig: str):
    CHECKPOINT.parent.mkdir(parents=True, exist_ok=True)
    tmp = {"done": done_ids, "src_sig": src_sig, "ts": int(time.time())}
    CHECKPOINT.write_text(json.dumps(tmp, ensure_ascii=False, indent=2), encoding="utf-8")

def _load_checkpoint() -> dict | None:
    if not CHECKPOINT.exists(): 
        return None
    try:
        return json.loads(CHECKPOINT.read_text(encoding="utf-8"))
    except Exception:
        return None

def _signature(items: List[Dict]) -> str:
    # хэш набора id + количество
    h = hashlib.md5()
    h.update(str(len(items)).encode())
    for r in items[:10000]:  # ограничимся первыми 10k
        h.update( (r["id"]+"\n").encode() )
    return h.hexdigest()

def graceful_kill_handler(signum, frame):
    print(f"\n[WARN] interrupted: signal={signum}. Checkpoint saved (if enabled).")
    sys.exit(130)

for s in (signal.SIGINT, signal.SIGTERM):
    try: signal.signal(s, graceful_kill_handler)
    except Exception: pass

def main():
    ap = argparse.ArgumentParser(description="Upsert rules to VDB (/vdb/upsert) with batching and retries")
    ap.add_argument("--src", default="data_out/rules_batch.json", help="Файл JSON (rules/items) или папка с *.json[.gz]")
    ap.add_argument("--url", default=DEFAULT_URL, help="URL эндпоинта /vdb/upsert")
    ap.add_argument("--secret", default=DEFAULT_SECRET, help="X-Auth секрет")
    ap.add_argument("--batch", type=int, default=256, help="Размер батча")
    ap.add_argument("--sleep", type=float, default=0.25, help="Пауза между батчами, сек")
    ap.add_argument("--retries", type=int, default=5, help="Повторы на ошибках")
    ap.add_argument("--resume", action="store_true", help="Резюмировать по чекпоинту (если совпадает сигнатура)")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="HTTP timeout сек")
    args = ap.parse_args()

    url = args.url.strip()
    secret = args.secret.strip()
    if not url or not secret:
        print("[FATAL] Need --url and --secret (or UPSERT_URL / VDB_WEBHOOK_SECRET in env)")
        return 2

    src = Path(args.src)
    items = _load_all_items(src)
    if not items:
        print("[FATAL] No items to upsert")
        return 2

    sig = _signature(items)
    start_index = 0
    if args.resume:
        cp = _load_checkpoint()
        if cp and cp.get("src_sig") == sig:
            done = set(cp.get("done") or [])
            if done:
                # найдём первую неотправленную позицию
                for i, r in enumerate(items):
                    if r["id"] not in done:
                        start_index = i
                        break
                print(f"[RESUME] checkpoint: {len(done)} done, resume from index {start_index}")
        else:
            print("[RESUME] no valid checkpoint (signature mismatch)")

    total = len(items)
    done_ids: List[str] = []
    if start_index:
        done_ids = [r["id"] for r in items[:start_index]]

    # батч-цикл
    i = start_index
    while i < total:
        batch = items[i:i+args.batch]
        # ретраи с экспоненциальным бэкоффом
        ok = False
        err_txt = ""
        for a in range(args.retries+1):
            code, txt = _post_batch(url, secret, batch, timeout=args.timeout)
            if 200 <= code < 300:
                ok = True
                break
            err_txt = f"HTTP {code}: {txt[:300]}"
            wait = min(30.0, (2.0 ** a) * 0.5)
            print(f"[WARN] batch {i//args.batch+1}: {err_txt} → retry in {wait:.1f}s")
            time.sleep(wait)
        if not ok:
            print(f"[FATAL] batch failed at index={i}: {err_txt}")
            _save_checkpoint(done_ids, sig)
            return 1

        # успех
        done_ids.extend([r["id"] for r in batch])
        i += len(batch)
        _save_checkpoint(done_ids, sig)
        pct = (len(done_ids) / total) * 100.0
        print(f"[OK] upserted {len(done_ids)}/{total} ({pct:.1f}%)")
        if args.sleep:
            time.sleep(args.sleep)

    print("[DONE] all items upserted ✓")
    return 0

if __name__ == "__main__":
    sys.exit(main())

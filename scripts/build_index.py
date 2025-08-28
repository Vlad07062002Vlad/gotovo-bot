# scripts/build_index.py — делаем json и (опционально) шлём его в /vdb/upsert
import json, os, sys, requests
from services.chunker import build_rules_batch

def main():
    root = os.getenv("PDF_ROOT", "data_out/pdfs")
    out  = os.getenv("RULES_JSON", "data_out/rules_batch.json")
    url  = os.getenv("UPsertURL") or os.getenv("UPSERT_URL")  # если хочешь слать сразу
    secret = os.getenv("VDB_WEBHOOK_SECRET", "")

    path, n = build_rules_batch(root=root, out_json=out)
    print(f"[OK] built {n} chunks → {path}")

    if url:
        with open(path, "rb") as f:
            r = requests.post(url, headers={"X-Auth": secret, "Content-Type": "application/json"}, data=f.read(), timeout=1200)
        print(f"[UPSERT] status={r.status_code} len={len(r.content)}")
        print(r.text)

if __name__ == "__main__":
    main()

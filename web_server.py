# web_server.py
import os, json, asyncio, base64, threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from openai import AsyncOpenAI

# === ENV ===
PORT = int(os.getenv("PORT", "8080"))
VDB_SECRET = os.getenv("VDB_WEBHOOK_SECRET", "")
CARD_SECRET = os.getenv("CARD_WEBHOOK_SECRET", "")
ERIP_SECRET = os.getenv("ERIP_WEBHOOK_SECRET", "")
SHOP_ID  = os.getenv("BEPAID_SHOP_ID", "")
SHOP_KEY = os.getenv("BEPAID_SECRET_KEY", "")

# ленивый клиент OpenAI для эмбеддингов в /vdb/upsert
_ai = None
def AI():
    global _ai
    if _ai is None:
        _ai = AsyncOpenAI(api_key=os.getenv("OPENAI_API_KEY"))
    return _ai

# наши сервисы
from rag_vdb import upsert_rules as vdb_upsert
from payments import apply_payment_payload

def _basic_ok(auth_header: str) -> bool:
    """Проверка Basic <base64(shop_id:secret)> для вебхуков bePaid."""
    if not SHOP_ID or not SHOP_KEY: 
        return False
    if not auth_header or not auth_header.lower().startswith("basic "):
        return False
    try:
        raw = base64.b64decode(auth_header.split(" ",1)[1]).decode("utf-8")
    except Exception:
        return False
    return raw == f"{SHOP_ID}:{SHOP_KEY}"

class Handler(BaseHTTPRequestHandler):
    server_version = "GotovoBot/1.0"

    def _ok(self, text="ok", code=200, ctype="text/plain; charset=utf-8"):
        body = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        ln = int(self.headers.get("Content-Length", "0") or 0)
        raw = self.rfile.read(ln) if ln>0 else b""
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # --- health ---
    def do_GET(self):
        if self.path == "/" or self.path.startswith("/health"):
            return self._ok("ok")
        return self._ok("not found", 404)

    # --- POST routing ---
    def do_POST(self):
        # 1) VDB upsert
        if self.path == "/vdb/upsert":
            if self.headers.get("X-Auth","") != VDB_SECRET:
                return self._ok("forbidden", 403)
            data = self._read_json()
            rules = data.get("rules") or data.get("items") or []
            if not rules:
                return self._ok('{"ok":false,"error":"no rules"}', 400, "application/json")
            # апсертим асинхронно
            async def run():
                await vdb_upsert(AI(), rules)
            asyncio.run(run())
            return self._ok('{"ok":true}', 200, "application/json")

        # 2) Webhook: card/subscription (bePaid)
        if self.path == "/webhook/card":
            # допускаем либо наш X-Auth, либо Basic(shop_id:secret)
            xauth = self.headers.get("X-Auth","")
            if not (xauth == CARD_SECRET or _basic_ok(self.headers.get("Authorization",""))):
                return self._ok("forbidden", 403)
            ev = self._read_json()
            # ожидаем tracking_id (tg user id) и payload (например SUB_MONTH, CREDITS_200)
            tracking = str(ev.get("tracking_id") or ev.get("order_id") or ev.get("uid") or "").strip()
            payload  = str(ev.get("payload") or ev.get("product_id") or ev.get("kind") or "").strip()
            # некоторые webhooks bePaid присылают status и отдельные поля — позволим форсировать payload через metadata
            meta = ev.get("metadata") or ev.get("additional_data") or {}
            tracking = str(meta.get("tracking_id") or tracking)
            payload  = str(meta.get("payload") or payload)

            if not tracking or not tracking.isdigit():
                return self._ok('{"ok":false,"error":"no tracking_id"}', 400, "application/json")
            uid = int(tracking)
            if not payload:
                # fallback: по сумме решать нельзя, поэтому возвращаем ok, но без действий
                return self._ok('{"ok":true,"note":"no payload"}', 200, "application/json")

            msg = apply_payment_payload(uid, payload)
            return self._ok(json.dumps({"ok":True,"message":msg}, ensure_ascii=False), 200, "application/json")

        # 3) Webhook: ERIP
        if self.path == "/webhook/erip":
            xauth = self.headers.get("X-Auth","")
            if not (xauth == ERIP_SECRET or _basic_ok(self.headers.get("Authorization",""))):
                return self._ok("forbidden", 403)
            ev = self._read_json()
            tracking = str(ev.get("tracking_id") or ev.get("order_id") or "").strip()
            payload  = str((ev.get("metadata") or {}).get("payload") or ev.get("payload") or "")
            if not tracking or not tracking.isdigit():
                return self._ok('{"ok":false,"error":"no tracking_id"}', 400, "application/json")
            uid = int(tracking)
            if payload:
                msg = apply_payment_payload(uid, payload)
                return self._ok(json.dumps({"ok":True,"message":msg}, ensure_ascii=False), 200, "application/json")
            return self._ok('{"ok":true}', 200, "application/json")

        return self._ok("not found", 404)

def start_http_server():
    srv = HTTPServer(("0.0.0.0", PORT), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, t

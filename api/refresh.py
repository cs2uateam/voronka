import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.sync import refresh_slice  # noqa: E402

DEFAULT_LIMIT = 8  # tuned to fit comfortably in 60s function timeout per slice


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n) if n else b""
            data = json.loads(body) if body else {}
            offset = int(data.get("offset", 0) or 0)
            limit = int(data.get("limit", DEFAULT_LIMIT) or DEFAULT_LIMIT)
            limit = max(1, min(limit, 25))
            result = refresh_slice(offset=offset, limit=limit)
            return self._json({"ok": True, **result}, 200)
        except RuntimeError as e:
            return self._json({"ok": False, "error": str(e), "needs_auth": "not authenticated" in str(e)}, 401)
        except Exception as e:
            return self._json({"ok": False, "error": f"{type(e).__name__}: {e}"}, 500)

    def _json(self, payload: dict, status: int) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

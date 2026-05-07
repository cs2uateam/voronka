import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.sync import add_urls  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
            body = self.rfile.read(n) if n else b""
            data = json.loads(body) if body else {}
            urls = data.get("urls") or []
            if not urls or not isinstance(urls, list):
                return self._json({"ok": False, "error": "no urls"}, 400)
            result = add_urls(urls)
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

import json
import os
import sys
from http.server import BaseHTTPRequestHandler

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.oauth_web import is_authenticated  # noqa: E402


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        try:
            authed = is_authenticated()
            payload = {"ok": True, "authenticated": authed}
            status = 200
        except Exception as e:
            payload = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            status = 500
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

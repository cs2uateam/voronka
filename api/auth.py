import os
import sys
from http.server import BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _lib.env import base_url_from_request  # noqa: E402
from _lib.oauth_web import authorization_url, exchange_code, store_refresh_token  # noqa: E402

CALLBACK_PATH = "/api/auth"


def _redirect_uri(headers) -> str:
    return base_url_from_request(headers) + CALLBACK_PATH


def _html(status: int, title: str, body_html: str, headers_extra: dict | None = None) -> tuple[int, dict, bytes]:
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>{title}</title>
<style>body{{background:#0a0a0a;color:#e8e8e8;font:14px/1.5 system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;padding:20px}}
.card{{max-width:480px;background:#161616;border:1px solid #2a2a2a;border-radius:12px;padding:32px}}
h1{{font-size:18px;margin:0 0 12px;color:#fff}}
p{{color:#aaa;margin:8px 0}}
a.btn{{display:inline-block;margin-top:16px;background:#fe2c55;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600}}</style>
</head><body><div class="card">{body_html}</div></body></html>"""
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if headers_extra:
        headers.update(headers_extra)
    return status, headers, page.encode("utf-8")


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        qs = parse_qs(parsed.query)

        try:
            redirect_uri = _redirect_uri(self.headers)
        except Exception as e:
            self._respond(*_html(500, "Auth error", f"<h1>Server config error</h1><p>{e}</p>"))
            return

        if "code" in qs:
            try:
                tokens = exchange_code(qs["code"][0], redirect_uri)
                refresh_token = tokens.get("refresh_token")
                if not refresh_token:
                    self._respond(*_html(
                        400, "Auth error",
                        "<h1>No refresh token</h1>"
                        "<p>Google didn't return a refresh token. This usually means you've already authorized "
                        "this app — go to <a href='https://myaccount.google.com/permissions' style='color:#fe2c55'>"
                        "Google permissions</a>, remove voronka-sync, and try again.</p>"
                        "<a href='/' class='btn'>Back to voronka</a>"
                    ))
                    return
                store_refresh_token(refresh_token)
                self._respond(*_html(
                    200, "Connected",
                    "<h1>✓ Подключено</h1>"
                    "<p>Можна повертатись у voronka, sync-панель уже готова.</p>"
                    "<a href='/' class='btn'>Open voronka</a>",
                    headers_extra={"Refresh": "0; url=/"},
                ))
            except Exception as e:
                self._respond(*_html(500, "Auth error", f"<h1>Token exchange failed</h1><p>{e}</p>"))
            return

        if "error" in qs:
            err = qs.get("error", ["unknown"])[0]
            self._respond(*_html(400, "Auth denied", f"<h1>Авторизация отклонена</h1><p>{err}</p>"
                                                    "<a href='/' class='btn'>Назад</a>"))
            return

        url = authorization_url(redirect_uri)
        self.send_response(302)
        self.send_header("Location", url)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _respond(self, status: int, headers: dict, body: bytes) -> None:
        self.send_response(status)
        for k, v in headers.items():
            self.send_header(k, v)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

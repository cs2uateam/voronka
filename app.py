import os
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from lib.jsonbin import data_bin
from lib.oauth_web import (
    authorization_url,
    exchange_code,
    is_authenticated,
    store_refresh_token,
)
from lib.sync import add_urls, refresh_slice
from lib import tiktok_oauth, tiktok_sync, instagram_oauth, instagram_sync

ROOT = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)


def _base_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.host
    return f"{proto}://{host}"


def _redirect_uri() -> str:
    return _base_url() + "/api/auth"


def _tiktok_redirect_uri() -> str:
    return _base_url() + "/api/tiktok/auth"


def _instagram_redirect_uri() -> str:
    return _base_url() + "/api/instagram/auth"


def _html(body_html: str, status: int = 200, extra_headers: dict | None = None):
    page = f"""<!doctype html><html><head><meta charset="utf-8"><title>voronka auth</title>
<style>body{{background:#0a0a0a;color:#e8e8e8;font:14px/1.5 system-ui,sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;text-align:center;padding:20px}}
.card{{max-width:480px;background:#161616;border:1px solid #2a2a2a;border-radius:12px;padding:32px}}
h1{{font-size:18px;margin:0 0 12px;color:#fff}} p{{color:#aaa;margin:8px 0}}
a.btn{{display:inline-block;margin-top:16px;background:#fe2c55;color:#fff;padding:10px 20px;border-radius:8px;text-decoration:none;font-weight:600}}</style>
</head><body><div class="card">{body_html}</div></body></html>"""
    headers = {"Content-Type": "text/html; charset=utf-8"}
    if extra_headers:
        headers.update(extra_headers)
    return page, status, headers


# ── static ───────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory(ROOT, "index.html")


@app.get("/<path:filename>")
def static_file(filename: str):
    if filename.startswith("api/") or ".." in filename:
        abort(404)
    fpath = (ROOT / filename).resolve()
    try:
        fpath.relative_to(ROOT)
    except ValueError:
        abort(404)
    if not fpath.is_file():
        abort(404)
    return send_from_directory(ROOT, filename)


# ── api ──────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    try:
        return jsonify(ok=True, authenticated=is_authenticated())
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/auth")
def api_auth():
    qs = request.args
    if "error" in qs:
        return _html(
            f"<h1>Авторизация отклонена</h1><p>{qs.get('error', 'unknown')}</p>"
            "<a href='/' class='btn'>Назад</a>",
            400,
        )
    if "code" in qs:
        try:
            tokens = exchange_code(qs["code"], _redirect_uri())
            refresh_token = tokens.get("refresh_token")
            if not refresh_token:
                return _html(
                    "<h1>No refresh token</h1>"
                    "<p>Зайди на <a href='https://myaccount.google.com/permissions' style='color:#fe2c55'>"
                    "Google permissions</a>, видали voronka, спробуй ще раз.</p>"
                    "<a href='/' class='btn'>Назад</a>",
                    400,
                )
            store_refresh_token(refresh_token)
            return _html(
                "<h1>✓ Подключено</h1><p>Sync-панель уже готова.</p>"
                "<a href='/?auth=success' class='btn'>Open voronka</a>",
                200,
                extra_headers={"Refresh": "0; url=/?auth=success"},
            )
        except Exception as e:
            return _html(f"<h1>Token exchange failed</h1><p>{e}</p>", 500)

    return redirect(authorization_url(_redirect_uri()), code=302)


@app.post("/api/add")
def api_add():
    try:
        data = request.get_json(silent=True) or {}
        urls = data.get("urls") or []
        if not urls or not isinstance(urls, list):
            return jsonify(ok=False, error="no urls"), 400
        return jsonify(ok=True, **add_urls(urls))
    except RuntimeError as e:
        msg = str(e)
        return jsonify(ok=False, error=msg, needs_auth="not authenticated" in msg), 401
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/data")
def api_data_read():
    try:
        return jsonify(data_bin().read())
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.put("/api/data")
def api_data_write():
    try:
        record = request.get_json(silent=True)
        if record is None or not isinstance(record, dict):
            return jsonify(ok=False, error="body must be a JSON object"), 400
        data_bin().write(record)
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/tiktok/status")
def api_tiktok_status():
    try:
        return jsonify(ok=True, authenticated=tiktok_oauth.is_authenticated())
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/tiktok/auth")
def api_tiktok_auth():
    qs = request.args
    if "error" in qs:
        return _html(
            f"<h1>Авторизация TikTok отклонена</h1><p>{qs.get('error', 'unknown')}</p>"
            "<a href='/' class='btn'>Назад</a>",
            400,
        )
    if "code" in qs:
        try:
            tokens = tiktok_oauth.exchange_code(qs["code"], _tiktok_redirect_uri())
            # TikTok returns {data: {access_token, refresh_token, ...}, error: ...} OR flat depending on API version
            data = tokens.get("data") if isinstance(tokens.get("data"), dict) else tokens
            refresh_token = data.get("refresh_token")
            access_token = data.get("access_token")
            if not refresh_token or not access_token:
                return _html(
                    f"<h1>Не получили токены от TikTok</h1><pre style='text-align:left;color:#888'>{tokens}</pre>"
                    "<a href='/' class='btn'>Назад</a>",
                    400,
                )
            import time as _time
            expires_at = int(_time.time()) + int(data.get("expires_in") or 86400) - 60
            tiktok_oauth.store_tokens(
                refresh_token=refresh_token,
                access_token=access_token,
                expires_at=expires_at,
                open_id=data.get("open_id"),
            )
            return _html(
                "<h1>✓ TikTok подключён</h1><p>Sync-панель уже готова.</p>"
                "<a href='/?auth=tiktok-success' class='btn'>Open voronka</a>",
                200,
                extra_headers={"Refresh": "0; url=/?auth=tiktok-success"},
            )
        except Exception as e:
            return _html(f"<h1>TikTok token exchange failed</h1><p>{e}</p>", 500)
    return redirect(tiktok_oauth.authorization_url(_tiktok_redirect_uri()), code=302)


@app.post("/api/tiktok/refresh")
def api_tiktok_refresh():
    try:
        data = request.get_json(silent=True) or {}
        offset = int(data.get("offset", 0) or 0)
        limit = int(data.get("limit", 8) or 8)
        return jsonify(ok=True, **tiktok_sync.refresh_slice(offset=offset, limit=limit))
    except RuntimeError as e:
        msg = str(e)
        return jsonify(ok=False, error=msg, needs_auth="not authenticated" in msg), 401
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/instagram/status")
def api_instagram_status():
    try:
        return jsonify(ok=True, authenticated=instagram_oauth.is_authenticated())
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/instagram/auth")
def api_instagram_auth():
    qs = request.args
    if "error" in qs:
        return _html(
            f"<h1>Авторизация Instagram отклонена</h1><p>{qs.get('error_description', qs.get('error', 'unknown'))}</p>"
            "<a href='/' class='btn'>Назад</a>",
            400,
        )
    if "code" in qs:
        try:
            redirect_uri = _instagram_redirect_uri()
            short_token = instagram_oauth.exchange_code(qs["code"], redirect_uri)
            long_token = instagram_oauth.exchange_long_lived(short_token)
            info = instagram_oauth.find_ig_business_account(long_token)
            instagram_oauth.store_auth(
                page_access_token=info["page_access_token"],
                ig_user_id=info["ig_user_id"],
                page_id=info["page_id"],
                ig_username=info.get("ig_username", ""),
                page_name=info.get("page_name", ""),
            )
            return _html(
                f"<h1>✓ Instagram подключён</h1>"
                f"<p>IG @{info.get('ig_username','')} · Page {info.get('page_name','')}</p>"
                "<a href='/?auth=ig-success' class='btn'>Open voronka</a>",
                200,
                extra_headers={"Refresh": "0; url=/?auth=ig-success"},
            )
        except Exception as e:
            return _html(f"<h1>Instagram auth failed</h1><p>{e}</p>", 500)
    return redirect(instagram_oauth.authorization_url(_instagram_redirect_uri()), code=302)


@app.post("/api/instagram/refresh")
def api_instagram_refresh():
    try:
        data = request.get_json(silent=True) or {}
        offset = int(data.get("offset", 0) or 0)
        limit = int(data.get("limit", 8) or 8)
        return jsonify(ok=True, **instagram_sync.refresh_slice(offset=offset, limit=limit))
    except RuntimeError as e:
        msg = str(e)
        return jsonify(ok=False, error=msg, needs_auth="not authenticated" in msg), 401
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.post("/api/refresh")
def api_refresh():
    try:
        data = request.get_json(silent=True) or {}
        offset = int(data.get("offset", 0) or 0)
        limit = int(data.get("limit", 8) or 8)
        limit = max(1, min(limit, 25))
        return jsonify(ok=True, **refresh_slice(offset=offset, limit=limit))
    except RuntimeError as e:
        msg = str(e)
        return jsonify(ok=False, error=msg, needs_auth="not authenticated" in msg), 401
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

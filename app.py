import os
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, request, send_from_directory

from lib.oauth_web import (
    authorization_url,
    exchange_code,
    is_authenticated,
    store_refresh_token,
)
from lib.store import read_full_record, write_full_record
from lib.sync import add_urls, refresh_slice
from lib import (
    tiktok_oauth, tiktok_sync, tiktok_csv,
    instagram_oauth, instagram_sync,
    telegram_oauth, telegram_sync,
)

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
        return jsonify(read_full_record())
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.put("/api/data")
def api_data_write():
    try:
        record = request.get_json(silent=True)
        if record is None or not isinstance(record, dict):
            return jsonify(ok=False, error="body must be a JSON object"), 400
        write_full_record(record)
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


@app.post("/api/tiktok/import-csv")
def api_tiktok_csv_import():
    """Accepts the Studio Content Data CSV *or* the full 'Download data' ZIP.

    When a ZIP is uploaded, the parser tries every .csv inside and picks the
    one with the most rows containing a recognizable video id — that's the
    per-video Content Data file. Overview / Followers / LIVE CSVs are
    skipped automatically.

    Returns summary {matched, added, rows_parsed, source, warnings,
    columns_detected}. Entries touched here get last_studio_import = now()
    — subsequent /refresh calls will preserve their metric values."""
    f = request.files.get("file")
    if f is None:
        return jsonify(ok=False, error="No file uploaded (expected form field 'file')"), 400
    try:
        raw = f.read()
        try:
            rows, col_map, warnings, source = tiktok_csv.parse_upload(raw)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        if not rows:
            return jsonify(
                ok=False,
                error="No usable rows parsed. " + (warnings[0] if warnings else "Verify this is the per-video Content Data export."),
                columns_detected=col_map,
                warnings=warnings,
                source=source,
            ), 400

        summary = tiktok_csv.apply_to_db(rows)
        return jsonify(
            ok=True,
            **summary,
            source=source,
            warnings=warnings,
            columns_detected=col_map,
        )
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


@app.post("/api/tiktok/refresh")
def api_tiktok_refresh():
    try:
        data = request.get_json(silent=True) or {}
        offset = int(data.get("offset", 0) or 0)
        limit = int(data.get("limit", 8) or 8)
        return jsonify(ok=True, **tiktok_sync.refresh_slice(offset=offset, limit=limit))
    except RuntimeError as e:
        msg = str(e)
        if "not authenticated" in msg:
            return jsonify(ok=False, error=msg, needs_auth=True), 401
        return jsonify(ok=False, error=msg), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 200


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
        if "not authenticated" in msg:
            return jsonify(ok=False, error=msg, needs_auth=True), 401
        return jsonify(ok=False, error=msg), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 200


@app.get("/api/telegram/status")
def api_telegram_status():
    try:
        return jsonify(ok=True, authenticated=telegram_oauth.is_authenticated())
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 500


def _tg_auth_form(step: str, **fields) -> tuple[str, int, dict]:
    """Inline phone/code form for Telegram auth — there's no OAuth redirect to ride on."""
    hidden_inputs = "".join(
        f'<input type="hidden" name="{k}" value="{v}">' for k, v in fields.items()
    )
    if step == "phone":
        body = """
        <h1>🔑 Подключить Telegram</h1>
        <p style='text-align:left'>Шаг 1/2: введи номер телефона аккаунта Telegram, у которого есть права админа в канале (со статистикой). Код придёт в твой Telegram (в чате от Telegram, не SMS).</p>
        <form method="POST" style="display:flex;flex-direction:column;gap:12px;align-items:center;margin-top:16px">
          <input type="hidden" name="step" value="send_code">
          <input type="tel" name="phone" placeholder="+380501234567" required
            style="background:#0a0a0a;color:#fff;border:1px solid #2a2a2a;border-radius:8px;padding:10px 14px;font:14px monospace;width:100%;max-width:280px">
          <button class="btn" style="background:#fe2c55;color:#fff;border:0;padding:10px 24px;border-radius:8px;cursor:pointer;font-weight:600">Получить код</button>
        </form>"""
    elif step == "code":
        body = f"""
        <h1>🔑 Подключить Telegram</h1>
        <p style='text-align:left'>Шаг 2/2: открой Telegram → чат от <b>Telegram</b> (официальный, не SMS) → скопируй код и введи здесь.</p>
        <form method="POST" style="display:flex;flex-direction:column;gap:12px;align-items:center;margin-top:16px">
          {hidden_inputs}
          <input type="hidden" name="step" value="verify_code">
          <input type="text" name="code" placeholder="12345" required pattern="\\d+" autocomplete="one-time-code"
            style="background:#0a0a0a;color:#fff;border:1px solid #2a2a2a;border-radius:8px;padding:10px 14px;font:18px monospace;letter-spacing:6px;width:140px;text-align:center">
          <input type="password" name="password" placeholder="2FA password (если включён)"
            style="background:#0a0a0a;color:#aaa;border:1px solid #2a2a2a;border-radius:8px;padding:8px 12px;font:13px sans-serif;width:100%;max-width:280px">
          <button class="btn" style="background:#fe2c55;color:#fff;border:0;padding:10px 24px;border-radius:8px;cursor:pointer;font-weight:600">Подтвердить</button>
        </form>"""
    else:
        body = "<h1>Unknown step</h1>"
    return _html(body, 200)


@app.route("/api/telegram/auth", methods=["GET", "POST"])
def api_telegram_auth():
    if request.method == "GET":
        return _tg_auth_form("phone")
    step = request.form.get("step", "")
    try:
        if step == "send_code":
            phone = (request.form.get("phone") or "").strip()
            if not phone:
                return _tg_auth_form("phone")
            result = telegram_oauth.send_code(phone)
            return _tg_auth_form(
                "code",
                phone=phone,
                phone_code_hash=result["phone_code_hash"],
                pending_session=result["session_pending"],
            )
        if step == "verify_code":
            phone = request.form.get("phone", "")
            code = (request.form.get("code") or "").strip()
            phone_code_hash = request.form.get("phone_code_hash", "")
            pending_session = request.form.get("pending_session", "")
            password = request.form.get("password") or None
            telegram_oauth.verify_code(phone, code, phone_code_hash, pending_session, password)
            return _html(
                "<h1>✓ Telegram подключён</h1><p>Можно возвращаться в voronka и нажимать Sync.</p>"
                "<a href='/?auth=tg-success' class='btn'>Open voronka</a>",
                200,
                extra_headers={"Refresh": "0; url=/?auth=tg-success"},
            )
    except Exception as e:
        return _html(f"<h1>Telegram auth failed</h1><p>{type(e).__name__}: {e}</p>"
                     "<a href='/api/telegram/auth' class='btn'>Попробовать ещё раз</a>", 400)
    return _tg_auth_form("phone")


@app.post("/api/telegram/refresh")
def api_telegram_refresh():
    try:
        data = request.get_json(silent=True) or {}
        offset = int(data.get("offset", 0) or 0)
        limit = int(data.get("limit", 8) or 8)
        return jsonify(ok=True, **telegram_sync.refresh_slice(offset=offset, limit=limit))
    except RuntimeError as e:
        msg = str(e)
        if "not authenticated" in msg:
            return jsonify(ok=False, error=msg, needs_auth=True), 401
        return jsonify(ok=False, error=msg), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 200


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
        if "not authenticated" in msg:
            return jsonify(ok=False, error=msg, needs_auth=True), 401
        return jsonify(ok=False, error=msg), 200
    except Exception as e:
        return jsonify(ok=False, error=f"{type(e).__name__}: {e}"), 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)), debug=True)

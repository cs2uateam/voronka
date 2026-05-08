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

ROOT = Path(__file__).resolve().parent

app = Flask(__name__, static_folder=None)


def _base_url() -> str:
    proto = request.headers.get("X-Forwarded-Proto") or request.scheme or "https"
    host = request.headers.get("X-Forwarded-Host") or request.headers.get("Host") or request.host
    return f"{proto}://{host}"


def _redirect_uri() -> str:
    return _base_url() + "/api/auth"


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


@app.get("/api/debug_stw")
def api_debug_stw():
    """Diagnostic: probe several Analytics API metric combinations for one video
    to find which (if any) matches YT Studio's 'Stayed to watch %' number."""
    from datetime import datetime, timezone
    video_id = request.args.get("video_id", "").strip()
    if not video_id:
        return jsonify(error="missing ?video_id="), 400
    try:
        from lib.oauth_web import get_credentials
        from googleapiclient.discovery import build
        creds = get_credentials()
        analytics = build("youtubeAnalytics", "v2", credentials=creds, cache_discovery=False)
        end_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        attempts = []

        def try_query(name: str, **kwargs):
            try:
                resp = analytics.reports().query(
                    ids="channel==MINE",
                    startDate="2005-02-14",
                    endDate=end_date,
                    filters=f"video=={video_id}",
                    **kwargs,
                ).execute()
                attempts.append({"name": name, "ok": True, "response": resp})
            except Exception as e:
                attempts.append({"name": name, "ok": False, "error": f"{type(e).__name__}: {e}"})

        try_query("averageViewPercentage", metrics="averageViewPercentage,views")
        try_query("averageViewDuration", metrics="averageViewDuration,views")
        try_query("audienceWatchRatio_no_dim", metrics="audienceWatchRatio")
        try_query("audienceWatchRatio_w_dim",
                  metrics="audienceWatchRatio",
                  dimensions="elapsedVideoTimeRatio")
        try_query("relativeRetentionPerformance", metrics="relativeRetentionPerformance")
        try_query("redViews_views", metrics="views,redViews,estimatedMinutesWatched,averageViewDuration")
        return jsonify(video_id=video_id, attempts=attempts)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


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

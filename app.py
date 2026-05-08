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


@app.get("/api/debug_channel")
def api_debug_channel():
    """Diagnostic: list channels accessible via OAuth, and lookup ownership of a specific video."""
    video_id = request.args.get("video_id", "").strip()
    try:
        from lib.oauth_web import get_credentials
        from googleapiclient.discovery import build
        creds = get_credentials()
        yt = build("youtube", "v3", credentials=creds, cache_discovery=False)
        out = {}
        # All channels the OAuth user owns
        ch = yt.channels().list(part="id,snippet,contentDetails", mine=True).execute()
        out["my_channels"] = [
            {
                "id": c.get("id"),
                "title": (c.get("snippet") or {}).get("title"),
                "uploads_playlist": (c.get("contentDetails") or {}).get("relatedPlaylists", {}).get("uploads"),
            }
            for c in (ch.get("items") or [])
        ]
        if video_id:
            v = yt.videos().list(part="snippet,status", id=video_id).execute()
            items = v.get("items") or []
            if items:
                sn = items[0].get("snippet") or {}
                st = items[0].get("status") or {}
                out["video_lookup"] = {
                    "id": video_id,
                    "channelId": sn.get("channelId"),
                    "channelTitle": sn.get("channelTitle"),
                    "publishedAt": sn.get("publishedAt"),
                    "privacyStatus": st.get("privacyStatus"),
                }
            else:
                out["video_lookup"] = {"id": video_id, "found": False}
        return jsonify(out)
    except Exception as e:
        return jsonify(error=f"{type(e).__name__}: {e}"), 500


@app.get("/api/debug_analytics")
def api_debug_analytics():
    """Diagnostic: returns raw YouTube Analytics API response for a single video.
    Helps explain why some entries get retention/shares/follows = 0."""
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
        # Attempt 1: same call we use in production
        try:
            r1 = analytics.reports().query(
                ids="channel==MINE",
                startDate="2005-02-14",
                endDate=end_date,
                metrics="averageViewPercentage,shares,subscribersGained,views,likes,comments",
                filters=f"video=={video_id}",
            ).execute()
            attempts.append({"name": "default", "ok": True, "response": r1})
        except Exception as e:
            attempts.append({"name": "default", "ok": False, "error": f"{type(e).__name__}: {e}"})
        # Attempt 2: with creatorContentType=SHORTS dimension
        try:
            r2 = analytics.reports().query(
                ids="channel==MINE",
                startDate="2005-02-14",
                endDate=end_date,
                metrics="averageViewPercentage,shares,subscribersGained,views",
                filters=f"video=={video_id};creatorContentType==SHORTS",
            ).execute()
            attempts.append({"name": "shorts_filter", "ok": True, "response": r2})
        except Exception as e:
            attempts.append({"name": "shorts_filter", "ok": False, "error": f"{type(e).__name__}: {e}"})
        # Attempt 3: narrower date range (last 90 days)
        from datetime import timedelta
        start_recent = (datetime.now(timezone.utc) - timedelta(days=90)).strftime("%Y-%m-%d")
        try:
            r3 = analytics.reports().query(
                ids="channel==MINE",
                startDate=start_recent,
                endDate=end_date,
                metrics="averageViewPercentage,shares,subscribersGained",
                filters=f"video=={video_id}",
            ).execute()
            attempts.append({"name": "last_90d", "ok": True, "response": r3})
        except Exception as e:
            attempts.append({"name": "last_90d", "ok": False, "error": f"{type(e).__name__}: {e}"})
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

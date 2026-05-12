"""Instagram sync — Supabase-backed."""

import re
import time
from datetime import datetime, timezone

from .instagram_api import InstagramClient
from .instagram_oauth import get_credentials
from .store import read_deleted_ids, read_entries, upsert_entries

HISTORY_CAP = 5
METRIC_FIELDS = ("plays", "reach", "likes", "comments", "shares", "saves", "follows", "profile_visits")
IG_SHORTCODE_RE = re.compile(r"/(?:reel|p)/([A-Za-z0-9_-]+)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(entry: dict) -> dict:
    snap = {"at": _now_iso()}
    for f in METRIC_FIELDS:
        snap[f] = entry.get(f, 0)
    return snap


def extract_shortcode(url: str) -> str | None:
    if not url:
        return None
    m = IG_SHORTCODE_RE.search(url)
    return m.group(1) if m else None


def _build_entry(media: dict, insights: dict, existing: dict | None = None) -> dict:
    timestamp = media.get("timestamp", "")
    pub_date = timestamp[:10] if timestamp else ""

    caption = (media.get("caption") or "").strip()
    title = caption.splitlines()[0] if caption else ""

    media_type = media.get("media_type") or "IMAGE"
    media_product = media.get("media_product_type") or ""
    if media_product == "REELS" or (media_type == "VIDEO"):
        type_v = "reels"
    elif media_type == "CAROUSEL_ALBUM":
        type_v = "карусель"
    else:
        type_v = "фото"

    plays = int(insights.get("views") or insights.get("plays") or 0)
    reach = int(insights.get("reach") or 0)
    likes = int(media.get("like_count") or insights.get("likes") or 0)
    comments = int(media.get("comments_count") or insights.get("comments") or 0)
    shares = int(insights.get("shares") or 0)
    saves = int(insights.get("saved") or 0)

    base: dict = {
        "id": (existing or {}).get("id") or int(time.time() * 1000),
        "title": title,
        "url": media.get("permalink") or "",
        "vid_group": (existing or {}).get("vid_group", ""),
        "date": (existing or {}).get("date") or pub_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "type": (existing or {}).get("type") or type_v,
        "hook": (existing or {}).get("hook", ""),
        "plays": plays,
        "reach": reach,
        "likes": likes,
        "comments": comments,
        "shares": shares,
        "saves": saves,
        "avg_watch": (existing or {}).get("avg_watch", 0),
        "profile_visits": (existing or {}).get("profile_visits", 0),
        "follows": (existing or {}).get("follows", 0),
    }
    history = list((existing or {}).get("history") or [])
    if existing:
        history.append(_snapshot(existing))
    if len(history) > HISTORY_CAP:
        history = history[-HISTORY_CAP:]
    if history:
        base["history"] = history
    return base


def refresh_slice(offset: int, limit: int) -> dict:
    if offset > 0:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": 0, "next_offset": offset, "done": True, "log": "",
        }

    entries = read_entries("instagram")
    deleted_ids = set(read_deleted_ids("instagram"))

    log_lines: list[str] = []
    discovered = 0
    refreshed = 0

    try:
        token, ig_user_id = get_credentials()
        client = InstagramClient(token, ig_user_id)
        media_list = client.fetch_all_media(max_total=500)
    except Exception as e:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": len(entries), "next_offset": 0, "done": True,
            "log": f"⚠ Instagram API error: {type(e).__name__}: {e}",
        }

    existing_by_mid: dict[str, int] = {}
    for i, e in enumerate(entries):
        mid = e.get("ig_media_id")
        if mid:
            existing_by_mid[str(mid)] = i

    media_by_id = {str(m.get("id")): m for m in media_list if m.get("id")}
    changed: list[dict] = []

    for mid, idx in list(existing_by_mid.items()):
        if mid not in media_by_id:
            continue
        media = media_by_id[mid]
        insights = client.fetch_insights(mid, media.get("media_type", "VIDEO"))
        new_entry = _build_entry(media, insights, existing=entries[idx])
        new_entry["ig_media_id"] = mid
        entries[idx] = new_entry
        changed.append(new_entry)
        refreshed += 1
        log_lines.append(f"↻ {new_entry.get('title','')[:60]} — {new_entry.get('plays', 0)} plays")

    for mid in reversed(list(media_by_id.keys())):
        if mid in existing_by_mid or mid in deleted_ids:
            continue
        media = media_by_id[mid]
        insights = client.fetch_insights(mid, media.get("media_type", "VIDEO"))
        new_entry = _build_entry(media, insights)
        new_entry["ig_media_id"] = mid
        entries.insert(0, new_entry)
        changed.append(new_entry)
        discovered += 1
        log_lines.append(f"+ added: {new_entry.get('title','')[:60]} — {new_entry.get('plays', 0)} plays")

    upsert_entries("instagram", changed)
    total = len(entries)
    return {
        "processed": refreshed,
        "missing": 0,
        "discovered": discovered,
        "total": total,
        "next_offset": total,
        "done": True,
        "log": "\n".join(log_lines),
    }

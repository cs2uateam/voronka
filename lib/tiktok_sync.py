"""TikTok sync — Supabase-backed."""

import re
import time
from datetime import datetime, timezone

from .store import read_deleted_ids, read_entries, upsert_entries
from .tiktok_api import TikTokClient
from .tiktok_oauth import get_access_token

HISTORY_CAP = 5
METRIC_FIELDS = ("views", "likes", "comments", "shares")
TIKTOK_VIDEO_ID_RE = re.compile(r"/video/(\d+)")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(entry: dict) -> dict:
    snap = {"at": _now_iso()}
    for f in METRIC_FIELDS:
        snap[f] = entry.get(f, 0)
    return snap


def extract_video_id(url: str) -> str | None:
    if not url:
        return None
    m = TIKTOK_VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _build_entry(video: dict, existing: dict | None = None) -> dict:
    create_time = int(video.get("create_time") or 0)
    pub_date = (
        datetime.fromtimestamp(create_time, tz=timezone.utc).strftime("%Y-%m-%d")
        if create_time else ""
    )
    title = (video.get("title") or "").strip()
    if not title:
        desc = (video.get("video_description") or "").strip()
        title = desc.splitlines()[0] if desc else ""

    # TikTok video IDs are numeric Snowflake-style identifiers — stable + unique
    # across the channel, so they make a fine primary key when we're inserting
    # a never-before-seen video. Existing entries keep their original id.
    new_id = int(video.get("id") or 0) or int(time.time() * 1000)
    # Entries previously refreshed from a TikTok Studio CSV are marked with
    # last_studio_import. Sync still updates *cosmetic* fields (title/cover/url)
    # so the page stays fresh, but it leaves their metric numbers alone — the
    # CSV truth has saves/completion_rate/profile_visits that the public Display
    # API doesn't even expose, plus its view counts use Studio's filtered
    # definition (>=3s plays, bot filtering) which we want to keep authoritative.
    csv_locked = bool((existing or {}).get("last_studio_import"))

    base: dict = {
        "id": (existing or {}).get("id") or new_id,
        "title": title,
        "url": video.get("share_url") or f"https://www.tiktok.com/video/{video.get('id')}",
        "cover_image_url": video.get("cover_image_url") or (existing or {}).get("cover_image_url", ""),
        "vid_group": (existing or {}).get("vid_group", ""),
        "date": (existing or {}).get("date") or pub_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "type": (existing or {}).get("type") or "fomo",
        "hook": (existing or {}).get("hook", ""),
    }

    if csv_locked:
        # Carry over every metric / studio-only field; never overwrite from API.
        for k in ("views", "likes", "comments", "shares",
                  "saves", "profile_visits", "follows",
                  "completion_rate", "avg_watch_time", "reach",
                  "last_studio_import"):
            if k in existing:
                base[k] = existing[k]
    else:
        base["views"]    = int(video.get("view_count") or 0)
        base["likes"]    = int(video.get("like_count") or 0)
        base["comments"] = int(video.get("comment_count") or 0)
        base["shares"]   = int(video.get("share_count") or 0)
        # Preserve any Studio-only fields still hanging around from prior imports
        for k in ("saves", "profile_visits", "follows",
                  "completion_rate", "avg_watch_time", "reach",
                  "last_studio_import"):
            if k in (existing or {}):
                base[k] = existing[k]
    history = list((existing or {}).get("history") or [])
    if existing:
        history.append(_snapshot(existing))
    if len(history) > HISTORY_CAP:
        history = history[-HISTORY_CAP:]
    if history:
        base["history"] = history
    return base


def refresh_slice(offset: int, limit: int) -> dict:
    """One-shot refresh: fetch all user videos, merge, upsert."""
    if offset > 0:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": 0, "next_offset": offset, "done": True, "log": "",
        }

    entries = read_entries("tiktok")
    deleted_ids = set(read_deleted_ids("tiktok"))

    log_lines: list[str] = []
    discovered = 0
    refreshed = 0

    try:
        client = TikTokClient(get_access_token())
        videos = client.fetch_all_user_videos(max_total=500)
    except Exception as e:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": len(entries), "next_offset": 0, "done": True,
            "log": f"⚠ TikTok API error: {type(e).__name__}: {e}",
        }

    videos_by_id = {str(v.get("id")): v for v in videos if v.get("id") is not None}
    existing_by_id = {extract_video_id(e.get("url", "")): i for i, e in enumerate(entries)}
    existing_by_id.pop(None, None)

    changed: list[dict] = []
    for vid, idx in list(existing_by_id.items()):
        if vid in videos_by_id:
            entry = _build_entry(videos_by_id[vid], existing=entries[idx])
            entries[idx] = entry
            changed.append(entry)
            refreshed += 1
            log_lines.append(f"↻ {(entry.get('title','') or '')[:60]} — {entry.get('views', 0)} views")

    for vid in reversed(list(videos_by_id.keys())):
        if vid in existing_by_id or vid in deleted_ids:
            continue
        new_entry = _build_entry(videos_by_id[vid])
        entries.insert(0, new_entry)
        changed.append(new_entry)
        discovered += 1
        log_lines.append(f"+ added: {new_entry.get('title','')[:60]} — {new_entry.get('views', 0)} views")

    upsert_entries("tiktok", changed)
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

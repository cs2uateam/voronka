"""TikTok refresh logic.

Unlike YouTube where every refresh hits two APIs per video (Data + Analytics),
TikTok's video.list returns all metrics for a batch of videos in one call.
So /api/tiktok/refresh isn't really sliced — it does one paginated walk of
the user's library and writes once.

We still expose the same offset/limit shape as YouTube so the frontend can
reuse its sync loop, but we always return done=true after the single pass.
"""

import re
import time
from datetime import datetime, timezone

from .jsonbin import data_bin
from .tiktok_api import TikTokClient
from .tiktok_oauth import get_access_token

HISTORY_CAP = 15
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
    """Pull the numeric video ID out of a TikTok URL (works for share URLs and standard ones)."""
    if not url:
        return None
    m = TIKTOK_VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def _build_entry(video: dict, existing: dict | None = None) -> dict:
    """Map a TikTok API video object onto a voronka entry, preserving user-set fields
    (vid_group, hook, type, manual metrics like saves/completion_rate)."""
    create_time = int(video.get("create_time") or 0)
    pub_date = (
        datetime.fromtimestamp(create_time, tz=timezone.utc).strftime("%Y-%m-%d")
        if create_time else ""
    )
    title = (video.get("title") or "").strip()
    if not title:
        # TikTok titles are often empty — fall back to the description's first line.
        desc = (video.get("video_description") or "").strip()
        title = desc.splitlines()[0] if desc else ""

    base: dict = {
        "id": (existing or {}).get("id") or int(time.time() * 1000),
        "title": title,
        "url": video.get("share_url") or f"https://www.tiktok.com/video/{video.get('id')}",
        "vid_group": (existing or {}).get("vid_group", ""),
        "date": (existing or {}).get("date") or pub_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "type": (existing or {}).get("type") or "fomo",
        "hook": (existing or {}).get("hook", ""),
        # API-filled
        "views": int(video.get("view_count") or 0),
        "likes": int(video.get("like_count") or 0),
        "comments": int(video.get("comment_count") or 0),
        "shares": int(video.get("share_count") or 0),
        # Manual fields preserved — TikTok API doesn't expose these.
        "saves": (existing or {}).get("saves", 0),
        "completion_rate": (existing or {}).get("completion_rate", 0),
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
    """Fetch the user's TikTok library, merge into the bin, write once.

    offset/limit are accepted for parity with YouTube's slicing API but we
    do all work on the offset==0 call and report done=true.
    """
    if offset > 0:
        # Already done in the first slice; nothing to do.
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": 0, "next_offset": offset, "done": True, "log": "",
        }

    bin_ = data_bin()
    record = bin_.read()
    record.setdefault("tiktok", {}).setdefault("entries", [])
    record["tiktok"].setdefault("deleted_ids", [])
    entries = record["tiktok"]["entries"]
    deleted_ids = set(record["tiktok"]["deleted_ids"] or [])

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

    # 1. Update existing entries from the same fetch
    for vid, idx in list(existing_by_id.items()):
        if vid in videos_by_id:
            entries[idx] = _build_entry(videos_by_id[vid], existing=entries[idx])
            refreshed += 1
            log_lines.append(f"↻ {(entries[idx].get('title','') or '')[:60]} — {entries[idx].get('views', 0)} views")

    # 2. Discover new videos (not in entries, not tombstoned)
    for vid in reversed(list(videos_by_id.keys())):  # reversed so newest ends up at index 0
        if vid in existing_by_id or vid in deleted_ids:
            continue
        new_entry = _build_entry(videos_by_id[vid])
        entries.insert(0, new_entry)
        discovered += 1
        log_lines.append(f"+ added: {new_entry.get('title','')[:60]} — {new_entry.get('views', 0)} views")

    bin_.write(record)
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

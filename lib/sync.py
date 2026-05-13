"""YouTube sync — now backed by Supabase via lib.store instead of JSONBin.

Same external shape as before: refresh_slice(offset, limit) returns the same
dict the frontend's loop expects. Internally we read entries from Postgres,
do all the discovery/refresh work in Python, then upsert changes back.

No more MAX_ENTRIES cap — Supabase 500MB tier holds ~1M entries comfortably.
HISTORY_CAP stays at 5 because more history doesn't tell us much beyond trend
direction; trimming aggressively keeps each entry small and refresh snappy.
"""

import hashlib
import time
from datetime import datetime, timezone

from .oauth_web import get_credentials
from .store import (
    read_deleted_ids,
    read_entries,
    upsert_entries,
)
from .url_parser import extract_video_id
from .youtube_api import YouTubeClient

SHORTS_DURATION_LIMIT_SEC = 60
METRIC_FIELDS = ("views", "retention", "likes", "comments", "shares", "follows")
HISTORY_CAP = 5


def _stable_id(s: str) -> int:
    """Deterministic 60-bit id from a string (YouTube video_id) — stable
    across runs so re-discovering the same video reuses the same row."""
    return int(hashlib.sha1((s or "").encode("utf-8")).hexdigest()[:15], 16)


def _parse_iso8601_duration_seconds(s: str) -> int:
    if not s or not s.startswith("PT"):
        return 0
    h = m = sec = 0
    cur = ""
    for ch in s[2:]:
        if ch.isdigit():
            cur += ch
        else:
            v = int(cur or "0")
            cur = ""
            if ch == "H":
                h = v
            elif ch == "M":
                m = v
            elif ch == "S":
                sec = v
    return h * 3600 + m * 60 + sec


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(entry: dict) -> dict:
    snap = {"at": _now_iso()}
    for f in METRIC_FIELDS:
        snap[f] = entry.get(f, 0)
    return snap


def _build_entry(public: dict, analytics: dict | None, url: str, existing: dict | None = None) -> dict:
    duration_sec = _parse_iso8601_duration_seconds(public.get("duration", ""))
    yt_type = "shorts" if duration_sec and duration_sec <= SHORTS_DURATION_LIMIT_SEC else "long"
    pub_date = (public.get("publishedAt", "") or "")[:10]

    if analytics is None:
        prev = existing or {}
        analytics = {
            "retention": prev.get("retention", 0.0),
            "shares": prev.get("shares", 0),
            "follows": prev.get("follows", 0),
        }

    vid = extract_video_id(url) or url
    base: dict = {
        "id": (existing or {}).get("id") or _stable_id(vid),
        "title": public.get("title", ""),
        "url": url,
        "vid_group": (existing or {}).get("vid_group", ""),
        "date": (existing or {}).get("date") or pub_date or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "type": (existing or {}).get("type") or yt_type,
        "hook": (existing or {}).get("hook", ""),
        "views": public.get("views", 0),
        "likes": public.get("likes", 0),
        "comments": public.get("comments", 0),
        "retention": analytics.get("retention", 0.0),
        "shares": analytics.get("shares", 0),
        "follows": analytics.get("follows", 0),
    }
    history = list((existing or {}).get("history") or [])
    if existing:
        history.append(_snapshot(existing))
    if len(history) > HISTORY_CAP:
        history = history[-HISTORY_CAP:]
    if history:
        base["history"] = history
    return base


def _bare_entry(video_id: str) -> dict:
    """A placeholder entry for a discovered video; metrics get filled on first refresh."""
    return {
        "id": _stable_id(video_id),
        "title": "",
        "url": f"https://youtube.com/shorts/{video_id}",
        "vid_group": "",
        "date": "",
        "type": "shorts",
        "hook": "",
        "views": 0, "likes": 0, "comments": 0,
        "retention": 0.0, "shares": 0, "follows": 0,
    }


def add_urls(urls: list[str]) -> dict:
    yt = YouTubeClient(get_credentials())

    entries = read_entries("youtube")
    deleted_ids = set(read_deleted_ids("youtube"))
    by_vid = {extract_video_id(e.get("url", "")): i for i, e in enumerate(entries) if extract_video_id(e.get("url", ""))}

    log_lines: list[str] = []
    added = updated = skipped = 0
    changed: list[dict] = []

    for raw in urls:
        raw = (raw or "").strip()
        if not raw:
            continue
        vid = extract_video_id(raw)
        if not vid:
            log_lines.append(f"✗ unrecognized URL: {raw}")
            skipped += 1
            continue
        public = yt.fetch_public(vid)
        if not public:
            log_lines.append(f"✗ video not found: {vid}")
            skipped += 1
            continue
        analytics = yt.fetch_analytics(vid)
        # un-tombstone if it was previously deleted
        if vid in deleted_ids:
            # PostgREST DELETE by (platform, external_id) — handled via store directly
            from .store import _request  # local import to keep public API tidy
            _request("DELETE", f"deleted_ids?platform=eq.youtube&external_id=eq.{vid}",
                     prefer="return=minimal")
            deleted_ids.discard(vid)
        if vid in by_vid:
            idx = by_vid[vid]
            entry = _build_entry(public, analytics, raw, existing=entries[idx])
            entries[idx] = entry
            changed.append(entry)
            log_lines.append(f"↻ updated: {public['title'][:60]} — {public['views']} views")
            updated += 1
        else:
            entry = _build_entry(public, analytics, raw)
            entries.insert(0, entry)
            by_vid[vid] = 0
            changed.append(entry)
            log_lines.append(f"+ added:   {public['title'][:60]} — {public['views']} views")
            added += 1

    upsert_entries("youtube", changed)
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "log": "\n".join(log_lines),
    }


def refresh_slice(offset: int, limit: int) -> dict:
    yt = YouTubeClient(get_credentials())

    entries = read_entries("youtube")
    deleted_ids = set(read_deleted_ids("youtube"))

    discovered = 0
    discover_error = ""

    # First slice: discover any channel videos that aren't already tracked.
    if offset == 0:
        try:
            existing_ids = {extract_video_id(e.get("url", "")) for e in entries}
            existing_ids.discard(None)
            channel_ids = yt.fetch_channel_uploads()
            new_ids = [v for v in channel_ids if v not in existing_ids and v not in deleted_ids]
            for vid in reversed(new_ids):
                entries.insert(0, _bare_entry(vid))
            discovered = len(new_ids)
        except Exception as e:
            discover_error = f"{type(e).__name__}: {e}"

    total = len(entries)

    if offset >= total:
        return {
            "processed": 0,
            "missing": 0,
            "discovered": discovered,
            "total": total,
            "next_offset": offset,
            "done": True,
            "log": ("⚠ discovery failed: " + discover_error) if discover_error else "",
        }

    end = min(offset + limit, total)
    log_lines: list[str] = []
    if discovered:
        log_lines.append(f"+ discovered {discovered} new video(s) from channel")
    if discover_error:
        log_lines.append(f"⚠ discovery failed: {discover_error}")

    processed = missing = 0
    changed: list[dict] = []
    for i in range(offset, end):
        entry = entries[i]
        url = entry.get("url", "")
        vid = extract_video_id(url)
        if not vid:
            log_lines.append(f"✗ skip (bad URL): {entry.get('title','')[:60]}")
            missing += 1
            continue
        public = yt.fetch_public(vid)
        if not public:
            log_lines.append(f"✗ video gone: {entry.get('title','')[:60]}")
            missing += 1
            continue
        analytics = yt.fetch_analytics(vid)
        new_entry = _build_entry(public, analytics, url, existing=entry)
        entries[i] = new_entry
        changed.append(new_entry)
        log_lines.append(f"↻ {public['title'][:60]} — {public['views']} views")
        processed += 1

    # Persist updates + any freshly-discovered bare entries from this slice.
    # Discovered entries occupy positions 0..discovered-1; processed ones occupy
    # offset..end-1. When discovered <= end the two ranges overlap, so the previous
    # `entries[:discovered] + changed` shape contained duplicate ids and PostgREST
    # rejected the batch with "ON CONFLICT DO UPDATE cannot affect row a second time".
    # Slicing entries[:max(discovered, end)] covers both ranges without duplicates.
    if offset == 0 and discovered:
        upsert_entries("youtube", entries[:max(discovered, end)])
    else:
        upsert_entries("youtube", changed)

    done = end >= total
    return {
        "processed": processed,
        "missing": missing,
        "discovered": discovered,
        "total": total,
        "next_offset": end,
        "done": done,
        "log": "\n".join(log_lines),
    }

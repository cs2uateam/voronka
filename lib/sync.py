import time
from datetime import datetime, timezone

from .jsonbin import data_bin
from .oauth_web import get_credentials
from .url_parser import extract_video_id
from .youtube_api import YouTubeClient

SHORTS_DURATION_LIMIT_SEC = 60
METRIC_FIELDS = ("views", "retention", "likes", "comments", "shares", "follows")


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


def _build_entry(public: dict, analytics: dict, url: str, existing: dict | None = None) -> dict:
    duration_sec = _parse_iso8601_duration_seconds(public.get("duration", ""))
    yt_type = "shorts" if duration_sec and duration_sec <= SHORTS_DURATION_LIMIT_SEC else "long"
    pub_date = (public.get("publishedAt", "") or "")[:10]

    base: dict = {
        "id": (existing or {}).get("id") or int(time.time() * 1000),
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
    if history:
        base["history"] = history
    return base


def add_urls(urls: list[str]) -> dict:
    bin_ = data_bin()
    yt = YouTubeClient(get_credentials())

    record = bin_.read()
    record.setdefault("youtube", {}).setdefault("entries", [])
    entries = record["youtube"]["entries"]
    by_vid = {extract_video_id(e.get("url", "")): i for i, e in enumerate(entries) if extract_video_id(e.get("url", ""))}

    log_lines: list[str] = []
    added = updated = skipped = 0

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
        if vid in by_vid:
            idx = by_vid[vid]
            entries[idx] = _build_entry(public, analytics, raw, existing=entries[idx])
            log_lines.append(f"↻ updated: {public['title'][:60]} — {public['views']} views")
            updated += 1
        else:
            entries.insert(0, _build_entry(public, analytics, raw))
            by_vid[vid] = 0
            log_lines.append(f"+ added:   {public['title'][:60]} — {public['views']} views")
            added += 1

    bin_.write(record)
    return {
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "log": "\n".join(log_lines),
    }


def refresh_slice(offset: int, limit: int) -> dict:
    bin_ = data_bin()
    record = bin_.read()
    entries = (record.get("youtube") or {}).get("entries") or []
    total = len(entries)

    if offset >= total:
        return {"processed": 0, "total": total, "done": True, "log": ""}

    yt = YouTubeClient(get_credentials())

    end = min(offset + limit, total)
    log_lines: list[str] = []
    processed = missing = 0

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
        log_lines.append(f"↻ {public['title'][:60]} — {public['views']} views")
        processed += 1

    bin_.write(record)

    done = end >= total
    return {
        "processed": processed,
        "missing": missing,
        "total": total,
        "next_offset": end,
        "done": done,
        "log": "\n".join(log_lines),
    }

import time
from datetime import datetime, timezone

from .jsonbin import data_bin
from .oauth_web import get_credentials
from .url_parser import extract_video_id
from .youtube_api import YouTubeClient

SHORTS_DURATION_LIMIT_SEC = 60
METRIC_FIELDS = ("views", "retention", "likes", "comments", "shares", "follows")
# JSONBin free tier rejects PUTs >100KB. With ~30 entries × full history, each
# entry's history must stay bounded. 5 snapshots per video keeps the whole bin
# comfortably under the cap even with 80+ YouTube videos + 50 Telegram posts.
HISTORY_CAP = 5
# Cap discovered+manual YT entries to bound bin size as the channel grows.
MAX_ENTRIES = 80


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

    # When Analytics API hasn't aggregated this video yet, fetch_analytics
    # returns None. Preserve whatever was previously stored instead of
    # overwriting with zeros — matters most for videos in their first 24-72h.
    if analytics is None:
        prev = existing or {}
        analytics = {
            "retention": prev.get("retention", 0.0),
            "shares": prev.get("shares", 0),
            "follows": prev.get("follows", 0),
        }

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
    if len(history) > HISTORY_CAP:
        history = history[-HISTORY_CAP:]
    if history:
        base["history"] = history
    return base


def _bare_entry(video_id: str) -> dict:
    """A placeholder entry for a discovered video; metrics get filled on refresh."""
    return {
        "id": int(time.time() * 1000) + (hash(video_id) & 0xFFF),
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
    bin_ = data_bin()
    yt = YouTubeClient(get_credentials())

    record = bin_.read()
    record.setdefault("youtube", {}).setdefault("entries", [])
    record["youtube"].setdefault("deleted_ids", [])
    entries = record["youtube"]["entries"]
    deleted_ids = record["youtube"]["deleted_ids"]
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
        # un-tombstone if it was previously deleted
        if vid in deleted_ids:
            record["youtube"]["deleted_ids"] = [d for d in deleted_ids if d != vid]
            deleted_ids = record["youtube"]["deleted_ids"]
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
    record.setdefault("youtube", {}).setdefault("entries", [])
    record["youtube"].setdefault("deleted_ids", [])

    yt = YouTubeClient(get_credentials())

    discovered = 0
    discover_error = ""

    # First slice: discover any channel videos that aren't already tracked.
    if offset == 0:
        try:
            entries = record["youtube"]["entries"]
            existing_ids = {extract_video_id(e.get("url", "")) for e in entries}
            existing_ids.discard(None)
            deleted_ids = set(record["youtube"]["deleted_ids"] or [])
            channel_ids = yt.fetch_channel_uploads()
            new_ids = [v for v in channel_ids if v not in existing_ids and v not in deleted_ids]
            # newest first in channel response → prepend in reverse so newest ends up at index 0
            for vid in reversed(new_ids):
                entries.insert(0, _bare_entry(vid))
            discovered = len(new_ids)
        except Exception as e:
            discover_error = f"{type(e).__name__}: {e}"

    entries = record["youtube"]["entries"]
    total = len(entries)

    if offset >= total:
        if discovered or discover_error:
            bin_.write(record)
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

    # Cap YouTube entries so the bin stays under JSONBin's 100KB free-tier ceiling.
    if len(entries) > MAX_ENTRIES:
        record["youtube"]["entries"] = entries[:MAX_ENTRIES]
        log_lines.append(f"… capped to {MAX_ENTRIES} newest entries (bin-size guard)")
        total = MAX_ENTRIES

    bin_.write(record)

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

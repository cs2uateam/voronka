"""Telegram refresh — fetch latest channel posts, merge with bin, write once.

Schema-wise, Telegram posts live in record.telegram.entries[] mirroring the
other platforms. Per-entry shape:

  {
    "id": <ms timestamp>,
    "msg_id": <Telegram message id; used for matching across refreshes>,
    "title": "first line of post text or [video]",
    "date": "YYYY-MM-DD",
    "type": "text"|"photo"|"video"|"album"|"audio"|"link"|"other",
    "url": "https://t.me/.../<msg_id>",
    "views": int,
    "forwards": int,
    "reactions": int,           # sum
    "reactions_breakdown": {"🔥": 12, ...},
    "comments": int,
    "history": [{at, views, forwards, reactions, comments}, ...]
  }

Capped to 100 posts and 10 history snapshots per entry to keep total bin size
comfortably under JSONBin's 100KB free-tier cap.
"""

import time
from datetime import datetime, timezone

from .env import required
from .jsonbin import data_bin
from .telegram_api import fetch_channel_posts

HISTORY_CAP = 5
MAX_ENTRIES = 50  # Newest N posts only — protects JSONBin's 100KB write cap.
METRIC_FIELDS = ("views", "forwards", "reactions", "comments")


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _snapshot(entry: dict) -> dict:
    snap = {"at": _now_iso()}
    for f in METRIC_FIELDS:
        snap[f] = entry.get(f, 0)
    return snap


def _build_entry(post: dict, existing: dict | None = None) -> dict:
    base: dict = {
        "id": (existing or {}).get("id") or int(time.time() * 1000),
        "msg_id": post["msg_id"],
        "title": post.get("title", ""),
        "url": post.get("url", ""),
        "vid_group": (existing or {}).get("vid_group", ""),
        "date": post.get("date") or datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "type": post.get("type", "text"),
        "hook": (existing or {}).get("hook", ""),
        "views": int(post.get("views") or 0),
        "forwards": int(post.get("forwards") or 0),
        "reactions": int(post.get("reactions") or 0),
        "reactions_breakdown": post.get("reactions_breakdown") or {},
        "comments": int(post.get("comments") or 0),
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
    """Same offset/limit shape as the other platforms, but Telegram syncs
    everything on offset==0 since one MTProto pass returns all posts at once."""
    if offset > 0:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": 0, "next_offset": offset, "done": True, "log": "",
        }

    bin_ = data_bin()
    record = bin_.read()
    record.setdefault("telegram", {}).setdefault("entries", [])
    record["telegram"].setdefault("deleted_ids", [])
    entries = record["telegram"]["entries"]
    deleted_ids = set(str(x) for x in (record["telegram"]["deleted_ids"] or []))

    log_lines: list[str] = []
    discovered = 0
    refreshed = 0

    try:
        channel = required("TELEGRAM_CHANNEL")
        posts = fetch_channel_posts(channel, limit=MAX_ENTRIES)
    except Exception as e:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": len(entries), "next_offset": 0, "done": True,
            "log": f"⚠ Telegram error: {type(e).__name__}: {e}",
        }

    posts_by_mid = {str(p["msg_id"]): p for p in posts}
    existing_by_mid: dict[str, int] = {}
    for i, e in enumerate(entries):
        mid = e.get("msg_id")
        if mid is not None:
            existing_by_mid[str(mid)] = i

    # 1. Update existing entries
    for mid, idx in list(existing_by_mid.items()):
        if mid not in posts_by_mid:
            continue
        entries[idx] = _build_entry(posts_by_mid[mid], existing=entries[idx])
        refreshed += 1
        log_lines.append(f"↻ {entries[idx].get('title','')[:60]} — {entries[idx].get('views', 0)} views")

    # 2. Discover new posts
    for mid in reversed(list(posts_by_mid.keys())):  # reversed so newest ends up at index 0
        if mid in existing_by_mid or mid in deleted_ids:
            continue
        new_entry = _build_entry(posts_by_mid[mid])
        entries.insert(0, new_entry)
        discovered += 1
        log_lines.append(f"+ added: {new_entry.get('title','')[:60]} — {new_entry.get('views', 0)} views")

    # Cap total entries to MAX_ENTRIES (drop oldest beyond the limit)
    if len(entries) > MAX_ENTRIES:
        record["telegram"]["entries"] = entries[:MAX_ENTRIES]
        log_lines.append(f"… capped to {MAX_ENTRIES} newest posts (bin-size guard)")

    bin_.write(record)
    total = len(record["telegram"]["entries"])
    return {
        "processed": refreshed,
        "missing": 0,
        "discovered": discovered,
        "total": total,
        "next_offset": total,
        "done": True,
        "log": "\n".join(log_lines),
    }

"""Telegram sync — Supabase-backed."""

import time
from datetime import datetime, timezone

from .env import required
from .store import read_deleted_ids, read_entries, upsert_entries
from .telegram_api import fetch_channel_posts

HISTORY_CAP = 5
# How many newest posts to refresh per Sync. Older posts stay in DB; their
# views drift slowly past ~7 days anyway so we just don't re-fetch them.
SYNC_WINDOW = 200
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
    if offset > 0:
        return {
            "processed": 0, "missing": 0, "discovered": 0,
            "total": 0, "next_offset": offset, "done": True, "log": "",
        }

    entries = read_entries("telegram")
    deleted_ids = set(read_deleted_ids("telegram"))

    log_lines: list[str] = []
    discovered = 0
    refreshed = 0

    try:
        channel = required("TELEGRAM_CHANNEL")
        posts = fetch_channel_posts(channel, limit=SYNC_WINDOW)
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

    changed: list[dict] = []

    for mid, idx in list(existing_by_mid.items()):
        if mid not in posts_by_mid:
            continue
        new_entry = _build_entry(posts_by_mid[mid], existing=entries[idx])
        entries[idx] = new_entry
        changed.append(new_entry)
        refreshed += 1
        log_lines.append(f"↻ {new_entry.get('title','')[:60]} — {new_entry.get('views', 0)} views")

    for mid in reversed(list(posts_by_mid.keys())):
        if mid in existing_by_mid or mid in deleted_ids:
            continue
        new_entry = _build_entry(posts_by_mid[mid])
        entries.insert(0, new_entry)
        changed.append(new_entry)
        discovered += 1
        log_lines.append(f"+ added: {new_entry.get('title','')[:60]} — {new_entry.get('views', 0)} views")

    upsert_entries("telegram", changed)
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

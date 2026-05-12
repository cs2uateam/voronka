"""Telethon wrapper for reading a single channel's posts + stats.

We pull the last N messages of a given channel (default 100), and per message:
- views (Telegram exposes per-post view count publicly)
- forwards (shares)
- reactions (sum across all emojis + per-emoji breakdown)
- replies/comments (when a discussion group is linked)
- date, message text, media type, message link

This is enough metrics to track post performance without needing the
aggregated "Channel Statistics" API that Telegram gates behind subscriber
thresholds.
"""

import asyncio
from datetime import datetime, timezone

from telethon.tl.types import (
    Message,
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
    ReactionCustomEmoji,
    ReactionEmoji,
)

from .telegram_oauth import get_client


def _media_type(msg: Message) -> str:
    """Classify a Telegram message for voronka's `type` field."""
    if not msg.media:
        return "text"
    if isinstance(msg.media, MessageMediaPhoto):
        return "photo"
    if isinstance(msg.media, MessageMediaDocument):
        doc = msg.media.document
        if doc and doc.mime_type:
            if doc.mime_type.startswith("video/"):
                return "video"
            if doc.mime_type.startswith("audio/"):
                return "audio"
        return "file"
    if isinstance(msg.media, MessageMediaWebPage):
        return "link"
    return "other"


def _reactions(msg: Message) -> tuple[int, dict[str, int]]:
    """Returns (sum_of_reactions, {emoji: count, ...})."""
    if not msg.reactions or not msg.reactions.results:
        return 0, {}
    total = 0
    breakdown: dict[str, int] = {}
    for r in msg.reactions.results:
        count = r.count or 0
        total += count
        emoji = ""
        if isinstance(r.reaction, ReactionEmoji):
            emoji = r.reaction.emoticon
        elif isinstance(r.reaction, ReactionCustomEmoji):
            emoji = f"custom:{r.reaction.document_id}"
        if emoji:
            breakdown[emoji] = count
    return total, breakdown


def _post_link(channel_username: str | None, channel_id: int, msg_id: int) -> str:
    if channel_username:
        return f"https://t.me/{channel_username}/{msg_id}"
    # Private channel — bare channel ID link
    return f"https://t.me/c/{channel_id}/{msg_id}"


async def _fetch_async(channel: str, limit: int = 100) -> list[dict]:
    client = get_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("not authenticated — Telegram session invalid, re-auth required")
        entity = await client.get_entity(channel)
        channel_username = getattr(entity, "username", None)
        channel_id = getattr(entity, "id", 0)

        posts: list[dict] = []
        async for msg in client.iter_messages(entity, limit=limit):
            if not isinstance(msg, Message):
                continue
            views = int(msg.views or 0)
            forwards = int(msg.forwards or 0)
            reactions_total, reaction_breakdown = _reactions(msg)
            comments = 0
            if msg.replies and msg.replies.replies:
                comments = int(msg.replies.replies)

            text = (msg.message or "").strip()
            title = text.splitlines()[0][:200] if text else f"[{_media_type(msg)}]"

            posts.append({
                "msg_id": msg.id,
                "title": title,
                "date": msg.date.astimezone(timezone.utc).strftime("%Y-%m-%d") if msg.date else "",
                "timestamp": msg.date.astimezone(timezone.utc).isoformat() if msg.date else "",
                "type": _media_type(msg),
                "views": views,
                "forwards": forwards,
                "reactions": reactions_total,
                "reactions_breakdown": reaction_breakdown,
                "comments": comments,
                "url": _post_link(channel_username, channel_id, msg.id),
            })
        return posts
    finally:
        await client.disconnect()


def fetch_channel_posts(channel: str, limit: int = 100) -> list[dict]:
    """Sync wrapper — returns latest `limit` posts of `channel` with metrics."""
    return asyncio.run(_fetch_async(channel, limit=limit))


async def _resolve_async(channel: str) -> dict:
    client = get_client()
    await client.connect()
    try:
        if not await client.is_user_authorized():
            raise RuntimeError("not authenticated")
        entity = await client.get_entity(channel)
        return {
            "id": getattr(entity, "id", None),
            "username": getattr(entity, "username", None),
            "title": getattr(entity, "title", None),
            "participants_count": getattr(entity, "participants_count", None),
        }
    finally:
        await client.disconnect()


def resolve_channel(channel: str) -> dict:
    """Sync wrapper — verify the configured channel exists and is reachable."""
    return asyncio.run(_resolve_async(channel))

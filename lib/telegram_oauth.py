"""Telegram MTProto auth — phone + code flow, session persisted to JSONBin.

Unlike YouTube/TikTok/Instagram which use OAuth web flows, Telegram requires
authorising a real user account via:
  1. POST phone → Telegram sends a numeric code TO the user's Telegram app
  2. POST {phone, code, phone_code_hash} → returns a session string
  3. (Optional) If 2FA enabled, after code → password step

Telethon serialises the entire authorised session to a string via StringSession.
We stash that in a dedicated JSONBin so future requests can re-instantiate the
client without re-asking for the code.

The Flask layer wraps the async Telethon calls with asyncio.run() per request —
simple and stateless, matches Render's serverless-ish runtime model.
"""

import asyncio

from telethon import TelegramClient
from telethon.errors import (
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)
from telethon.sessions import StringSession

from .env import required
from .jsonbin import JsonBin


def _api() -> tuple[int, str]:
    return int(required("TELEGRAM_API_ID")), required("TELEGRAM_API_HASH")


def _auth_bin() -> JsonBin:
    return JsonBin(required("JSONBIN_TELEGRAM_AUTH_BIN_ID"), required("JSONBIN_MASTER_KEY"))


def _save_session(session_str: str) -> None:
    _auth_bin().write({"session": session_str})


def _load_session() -> str | None:
    try:
        rec = _auth_bin().read() or {}
        return rec.get("session") or None
    except Exception:
        return None


def is_authenticated() -> bool:
    """Returns True if a non-empty session string is stored.

    Note: this doesn't verify the session is still valid on Telegram's side —
    a connect() call would, but that's expensive on every status probe."""
    return bool(_load_session())


async def _send_code_async(phone: str) -> dict:
    api_id, api_hash = _api()
    client = TelegramClient(StringSession(), api_id, api_hash)
    await client.connect()
    try:
        result = await client.send_code_request(phone)
        # We deliberately don't disconnect — Telegram requires the same connection
        # for the subsequent sign_in. Instead we close after returning the session
        # (which won't be authorised yet but holds the connection ID).
        session_str = client.session.save()
        return {
            "phone_code_hash": result.phone_code_hash,
            "session_pending": session_str,
        }
    finally:
        await client.disconnect()


async def _sign_in_async(phone: str, code: str, phone_code_hash: str,
                         pending_session: str, password: str | None) -> str:
    api_id, api_hash = _api()
    client = TelegramClient(StringSession(pending_session), api_id, api_hash)
    await client.connect()
    try:
        try:
            await client.sign_in(phone=phone, code=code, phone_code_hash=phone_code_hash)
        except SessionPasswordNeededError:
            if not password:
                raise RuntimeError("2FA_PASSWORD_REQUIRED")
            await client.sign_in(password=password)
        if not await client.is_user_authorized():
            raise RuntimeError("authorisation failed — Telegram rejected the code")
        return client.session.save()
    finally:
        await client.disconnect()


def send_code(phone: str) -> dict:
    """Step 1 — Telegram delivers a numeric code to the user's Telegram app.
    Returns {phone_code_hash, session_pending} to pass into verify_code()."""
    return asyncio.run(_send_code_async(phone))


def verify_code(phone: str, code: str, phone_code_hash: str,
                pending_session: str, password: str | None = None) -> None:
    """Step 2 — sign in with the code (and optional 2FA password). Persists session."""
    session_str = asyncio.run(_sign_in_async(phone, code, phone_code_hash, pending_session, password))
    _save_session(session_str)


def get_client() -> TelegramClient:
    """Create a Telethon client from the stored session. Caller must await client.connect()."""
    session_str = _load_session()
    if not session_str:
        raise RuntimeError("not authenticated — run /api/telegram/auth first")
    api_id, api_hash = _api()
    return TelegramClient(StringSession(session_str), api_id, api_hash)

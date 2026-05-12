"""TikTok Login Kit / Open API OAuth flow.

Differences from Google's Flow that matter:
- Authorization endpoint sits on tiktok.com (not the API host)
- Token exchange + refresh use form-encoded bodies, not JSON
- Refresh tokens last ~365 days (vs Google's 7-day testing tokens)
- Each refresh ROTATES the refresh_token, so we always store the latest
"""

import time
from urllib.parse import urlencode

import requests

from .env import required
from .store import read_auth, write_auth

AUTH_URL = "https://www.tiktok.com/v2/auth/authorize/"
TOKEN_URL = "https://open.tiktokapis.com/v2/oauth/token/"
SCOPES = ["user.info.basic", "video.list"]


def _client() -> tuple[str, str]:
    return required("TIKTOK_CLIENT_KEY"), required("TIKTOK_CLIENT_SECRET")


def authorization_url(redirect_uri: str, state: str = "") -> str:
    ck, _ = _client()
    params = {
        "client_key": ck,
        "scope": ",".join(SCOPES),
        "response_type": "code",
        "redirect_uri": redirect_uri,
    }
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    ck, cs = _client()
    r = requests.post(
        TOKEN_URL,
        data={
            "client_key": ck,
            "client_secret": cs,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    r.raise_for_status()
    return r.json()


def store_tokens(refresh_token: str, access_token: str | None = None,
                 expires_at: int | None = None, open_id: str | None = None) -> None:
    record = read_auth("tiktok") or {}
    record["refresh_token"] = refresh_token
    if access_token is not None:
        record["access_token"] = access_token
    if expires_at is not None:
        record["expires_at"] = expires_at
    if open_id is not None:
        record["open_id"] = open_id
    write_auth("tiktok", record)


def load_refresh_token() -> str | None:
    try:
        return (read_auth("tiktok") or {}).get("refresh_token") or None
    except Exception:
        return None


def is_authenticated() -> bool:
    return bool(load_refresh_token())


def get_access_token() -> str:
    """Returns a fresh access_token, refreshing it transparently if expired."""
    record = read_auth("tiktok") or {}
    refresh_token = record.get("refresh_token")
    if not refresh_token:
        raise RuntimeError("not authenticated — run /api/tiktok/auth first")

    access_token = record.get("access_token")
    expires_at = int(record.get("expires_at") or 0)
    if access_token and expires_at > int(time.time()) + 60:
        return access_token

    ck, cs = _client()
    r = requests.post(
        TOKEN_URL,
        data={
            "client_key": ck,
            "client_secret": cs,
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        },
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        timeout=20,
    )
    if not r.ok:
        raise RuntimeError(f"token refresh failed: {r.status_code} {r.text[:200]}")
    tokens = r.json()
    new_refresh = tokens.get("refresh_token") or refresh_token
    new_access = tokens.get("access_token")
    if not new_access:
        raise RuntimeError(f"token refresh missing access_token: {tokens}")
    expires_in = int(tokens.get("expires_in") or 86400)
    new_expires_at = int(time.time()) + expires_in - 60

    record["refresh_token"] = new_refresh
    record["access_token"] = new_access
    record["expires_at"] = new_expires_at
    write_auth("tiktok", record)
    return new_access

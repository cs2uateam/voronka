"""YouTube OAuth (Google) — Supabase-backed token storage."""

from urllib.parse import urlencode

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials

from .env import required
from .store import read_auth, write_auth

SCOPES = [
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
]
AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"


def _client() -> tuple[str, str]:
    return required("GOOGLE_CLIENT_ID"), required("GOOGLE_CLIENT_SECRET")


def authorization_url(redirect_uri: str, state: str = "") -> str:
    cid, _ = _client()
    params = {
        "client_id": cid,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> dict:
    cid, secret = _client()
    resp = requests.post(
        TOKEN_URL,
        data={
            "code": code,
            "client_id": cid,
            "client_secret": secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        },
        timeout=20,
    )
    resp.raise_for_status()
    return resp.json()


def store_refresh_token(refresh_token: str) -> None:
    payload = read_auth("youtube") or {}
    payload["refresh_token"] = refresh_token
    write_auth("youtube", payload)


def load_refresh_token() -> str | None:
    try:
        return (read_auth("youtube") or {}).get("refresh_token") or None
    except Exception:
        return None


def get_credentials() -> Credentials:
    refresh_token = load_refresh_token()
    if not refresh_token:
        raise RuntimeError("not authenticated — run /api/auth first")
    cid, secret = _client()
    creds = Credentials(
        token=None,
        refresh_token=refresh_token,
        token_uri=TOKEN_URL,
        client_id=cid,
        client_secret=secret,
        scopes=SCOPES,
    )
    creds.refresh(Request())
    return creds


def is_authenticated() -> bool:
    return bool(load_refresh_token())

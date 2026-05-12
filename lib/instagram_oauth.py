"""Instagram Graph API auth: Facebook Login → long-lived page token + IG user ID.

Token flow (different from YouTube and TikTok):
  1. User clicks "Подключить Instagram" → we send them to www.facebook.com/v22.0/dialog/oauth
  2. They authorise; Facebook redirects back with ?code=
  3. We exchange code → SHORT-lived user access token (~1 hour)
  4. We exchange short → LONG-lived user token (~60 days)
  5. We hit GET /me/accounts → list the user's Facebook Pages with PAGE access tokens
  6. For each Page we ask GET /{page}?fields=instagram_business_account
  7. The page whose IG-business-account is non-null is the one we want
  8. We store: page_access_token + ig_user_id + page_id

Page tokens derived from a long-lived user token never expire as long as
the user keeps using Facebook, so we don't need a refresh loop like for TikTok.
"""

from urllib.parse import urlencode

import requests

from .env import required
from .store import read_auth, write_auth

GRAPH_VERSION = "v22.0"
FB_AUTH_URL = f"https://www.facebook.com/{GRAPH_VERSION}/dialog/oauth"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

# Minimum scopes to read media + insights on the user's IG Business / Creator account.
SCOPES = ",".join([
    "pages_show_list",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_manage_insights",
])


def _app() -> tuple[str, str]:
    return required("META_APP_ID"), required("META_APP_SECRET")


def authorization_url(redirect_uri: str, state: str = "") -> str:
    app_id, _ = _app()
    params = {
        "client_id": app_id,
        "redirect_uri": redirect_uri,
        "scope": SCOPES,
        "response_type": "code",
    }
    if state:
        params["state"] = state
    return f"{FB_AUTH_URL}?{urlencode(params)}"


def exchange_code(code: str, redirect_uri: str) -> str:
    """Returns a SHORT-lived user access token."""
    app_id, app_secret = _app()
    r = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "client_id": app_id,
            "client_secret": app_secret,
            "redirect_uri": redirect_uri,
            "code": code,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def exchange_long_lived(short_token: str) -> str:
    """Exchange a short-lived user token for a long-lived one (~60 days, refreshable)."""
    app_id, app_secret = _app()
    r = requests.get(
        f"{GRAPH_BASE}/oauth/access_token",
        params={
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
        timeout=20,
    )
    r.raise_for_status()
    return r.json()["access_token"]


def find_ig_business_account(user_token: str) -> dict:
    """Returns {page_id, page_access_token, ig_user_id, ig_username, page_name}
    for the first Page connected to an IG Business/Creator account.
    Raises RuntimeError if none of the user's pages have a connected IG."""
    r = requests.get(
        f"{GRAPH_BASE}/me/accounts",
        params={"access_token": user_token, "fields": "id,name,access_token"},
        timeout=20,
    )
    r.raise_for_status()
    pages = r.json().get("data") or []
    if not pages:
        raise RuntimeError("User has no Facebook Pages — IG Business account must be linked to a Page.")

    for page in pages:
        page_id = page["id"]
        page_token = page["access_token"]
        rr = requests.get(
            f"{GRAPH_BASE}/{page_id}",
            params={
                "access_token": page_token,
                "fields": "instagram_business_account{id,username}",
            },
            timeout=20,
        )
        if not rr.ok:
            continue
        body = rr.json()
        ig = body.get("instagram_business_account")
        if ig and ig.get("id"):
            return {
                "page_id": page_id,
                "page_access_token": page_token,
                "ig_user_id": ig["id"],
                "ig_username": ig.get("username") or "",
                "page_name": page.get("name") or "",
            }
    raise RuntimeError(
        "None of your Facebook Pages has a connected Instagram Business/Creator account. "
        "Switch your IG to Business or Creator, then link it to a Page."
    )


def store_auth(page_access_token: str, ig_user_id: str, page_id: str,
               ig_username: str = "", page_name: str = "") -> None:
    write_auth("instagram", {
        "page_access_token": page_access_token,
        "ig_user_id": ig_user_id,
        "page_id": page_id,
        "ig_username": ig_username,
        "page_name": page_name,
    })


def load_auth() -> dict | None:
    try:
        rec = read_auth("instagram") or {}
        if rec.get("page_access_token") and rec.get("ig_user_id"):
            return rec
    except Exception:
        pass
    return None


def is_authenticated() -> bool:
    return load_auth() is not None


def get_credentials() -> tuple[str, str]:
    """Returns (page_access_token, ig_user_id). Raises if not authenticated."""
    auth = load_auth()
    if not auth:
        raise RuntimeError("not authenticated — run /api/instagram/auth first")
    return auth["page_access_token"], auth["ig_user_id"]

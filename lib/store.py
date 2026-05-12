"""Supabase REST wrapper — replaces the per-platform JSONBin shuffle with
ordinary Postgres tables exposed via PostgREST.

Schema:
  entries        (platform text, id bigint, data jsonb, updated_at)   PK (platform, id)
  deleted_ids    (platform text, external_id text)                    PK (platform, external_id)
  auth_tokens    (platform text PK, data jsonb, updated_at)

Why this exists:
- JSONBin's free 100KB-per-record cap was throttling growth across 4 platforms.
- Supabase free tier is 500MB → effectively unlimited for our content volume.
- PostgREST auto-generates a REST API on top of the tables, so this module is
  thin HTTP: read_entries(plat) → GET, upsert_entries → POST with merge-duplicates,
  delete → DELETE with filter, etc.

Service-role key bypasses RLS, so writes succeed even though tables have RLS on.
"""

import requests

from .env import required

PLATFORMS = ("youtube", "tiktok", "instagram", "telegram")


def _base() -> str:
    return required("SUPABASE_URL").rstrip("/") + "/rest/v1"


def _headers(prefer: str | None = None) -> dict:
    key = required("SUPABASE_KEY")
    h = {
        "apikey": key,
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


def _request(method: str, path: str, *, body=None, prefer: str | None = None,
             timeout: int = 30) -> requests.Response:
    url = f"{_base()}/{path}"
    r = requests.request(
        method, url,
        headers=_headers(prefer),
        json=body,
        timeout=timeout,
    )
    if not r.ok:
        raise RuntimeError(f"Supabase {method} {path} -> {r.status_code}: {r.text[:300]}")
    return r


# ── entries ────────────────────────────────────────────────────────────

def read_entries(platform: str) -> list[dict]:
    """Returns entries for a platform as a list of dicts, with `id` flattened
    out of the row alongside the JSONB data. Ordered by id descending
    (matches voronka's newest-first display)."""
    r = _request("GET", f"entries?platform=eq.{platform}&select=id,data&order=id.desc")
    rows = r.json() or []
    return [{**(row["data"] or {}), "id": int(row["id"])} for row in rows]


def upsert_entries(platform: str, entries: list[dict]) -> None:
    """Upsert by (platform, id). Each entry must carry an integer `id`."""
    if not entries:
        return
    payload = [
        {"platform": platform, "id": int(e["id"]), "data": {k: v for k, v in e.items() if k != "id"}}
        for e in entries
    ]
    _request("POST", "entries", body=payload, prefer="resolution=merge-duplicates,return=minimal")


def replace_entries(platform: str, entries: list[dict]) -> None:
    """Make Supabase's set of entries for `platform` match `entries` exactly.

    Diff strategy: upsert everything new/changed, then delete IDs that
    existed before but aren't in the new set. Single-writer safe, no
    transaction needed for our single-user use case."""
    new_ids = {int(e["id"]) for e in entries if "id" in e}
    upsert_entries(platform, entries)

    # Find IDs that should be removed
    existing = read_entries(platform)
    existing_ids = {int(e["id"]) for e in existing}
    to_delete = existing_ids - new_ids
    if to_delete:
        # PostgREST `in` filter: id=in.(1,2,3) — URL-safe for ints
        ids_str = ",".join(str(i) for i in to_delete)
        _request("DELETE", f"entries?platform=eq.{platform}&id=in.({ids_str})",
                 prefer="return=minimal")


def delete_entry(platform: str, entry_id: int) -> None:
    _request("DELETE", f"entries?platform=eq.{platform}&id=eq.{int(entry_id)}",
             prefer="return=minimal")


# ── deleted_ids (tombstones for channel-discovery) ─────────────────────

def read_deleted_ids(platform: str) -> list[str]:
    r = _request("GET", f"deleted_ids?platform=eq.{platform}&select=external_id")
    return [row["external_id"] for row in (r.json() or [])]


def add_deleted_id(platform: str, external_id: str) -> None:
    _request(
        "POST", "deleted_ids",
        body=[{"platform": platform, "external_id": str(external_id)}],
        prefer="resolution=ignore-duplicates,return=minimal",
    )


def replace_deleted_ids(platform: str, ids: list[str]) -> None:
    """Match Supabase's tombstone set exactly. Used by /api/data PUT."""
    new_set = {str(i) for i in (ids or [])}
    # Upsert new ones
    if new_set:
        payload = [{"platform": platform, "external_id": x} for x in new_set]
        _request("POST", "deleted_ids", body=payload,
                 prefer="resolution=ignore-duplicates,return=minimal")
    # Delete missing ones
    existing = set(read_deleted_ids(platform))
    to_delete = existing - new_set
    for x in to_delete:
        # external_id can contain anything, so encode via URL params via PostgREST
        _request("DELETE", f"deleted_ids?platform=eq.{platform}&external_id=eq.{x}",
                 prefer="return=minimal")


# ── auth_tokens (per-platform OAuth state) ─────────────────────────────

def read_auth(platform: str) -> dict:
    """Returns the stored auth payload for a platform (empty dict if none)."""
    r = _request("GET", f"auth_tokens?platform=eq.{platform}&select=data")
    rows = r.json() or []
    if not rows:
        return {}
    return rows[0].get("data") or {}


def write_auth(platform: str, data: dict) -> None:
    """Upsert the platform's auth payload."""
    payload = [{"platform": platform, "data": data}]
    _request("POST", "auth_tokens", body=payload,
             prefer="resolution=merge-duplicates,return=minimal")


# ── full-record bridge for legacy /api/data ────────────────────────────

def read_full_record() -> dict:
    """Returns the same shape voronka's frontend expects:
       { youtube: {entries:[...], deleted_ids:[...]}, tiktok: {...}, ... }"""
    record: dict = {}
    for plat in PLATFORMS:
        record[plat] = {
            "entries": read_entries(plat),
            "deleted_ids": read_deleted_ids(plat),
        }
    return record


def write_full_record(record: dict) -> None:
    """Inverse of read_full_record — used by /api/data PUT when the frontend
    saves edits/deletes through saveData()."""
    for plat in PLATFORMS:
        plat_data = (record or {}).get(plat) or {}
        replace_entries(plat, plat_data.get("entries") or [])
        replace_deleted_ids(plat, plat_data.get("deleted_ids") or [])

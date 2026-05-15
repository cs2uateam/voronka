"""TikTok Studio "Download data" CSV importer.

Studio's CSV column names drift between regions and UI updates, so this
parser doesn't hardcode a schema. Instead it fingerprints each header
by keyword matching (case-insensitive) and maps it to our internal
metric fields. Rows are matched to existing entries by the TikTok
video id extracted from the row's URL.

After a successful import the affected entries get a `last_studio_import`
ISO timestamp — `tiktok_sync._build_entry` reads this flag to know it
should keep the Studio metrics on subsequent API syncs rather than
overwriting them with the rougher Display API counts.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import datetime, timezone

from .store import read_entries, upsert_entries

VIDEO_ID_RE = re.compile(r"/video/(\d+)")

# Column-name → internal field, by keyword. Each tuple is
# (internal_field, keyword_set, parser). The first header whose lowered
# label contains any keyword wins for that column.
COLUMN_HINTS: list[tuple[str, tuple[str, ...]]] = [
    ("url",              ("url", "link", "post link", "post url")),
    ("video_id",         ("video id", "post id")),
    ("date",             ("post date", "publish", "posted on", "date")),
    ("title",            ("caption", "description", "title", "post caption")),
    ("views",            ("video views", "view")),
    ("likes",            ("like",)),
    ("comments",         ("comment",)),
    ("shares",           ("share",)),
    ("saves",            ("bookmark", "save")),
    ("profile_visits",   ("profile visit",)),
    ("follows",          ("new follower", "follower gain", "followers gained")),
    ("completion_rate",  ("full video watched", "completion", "watched rate")),
    ("avg_watch_time",   ("average watch", "avg watch")),
    ("reach",            ("reach", "unique viewers")),
]

METRIC_FIELDS = (
    "views", "likes", "comments", "shares",
    "saves", "profile_visits", "follows",
    "completion_rate", "avg_watch_time", "reach",
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _detect_columns(headers: list[str]) -> dict[str, int]:
    """Maps internal field name → column index in this CSV."""
    mapping: dict[str, int] = {}
    lowered = [h.strip().lower() for h in headers]
    for field, hints in COLUMN_HINTS:
        if field in mapping:
            continue
        for i, h in enumerate(lowered):
            if i in mapping.values():
                continue
            if any(k in h for k in hints):
                mapping[field] = i
                break
    return mapping


def _to_number(raw: str) -> float | None:
    """Parses '1,234' / '12.5%' / '' into a float, returns None on garbage."""
    if raw is None:
        return None
    s = str(raw).strip().replace(",", "").replace("%", "")
    if not s or s.lower() in ("--", "n/a", "—", "-"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _extract_video_id(row: dict) -> str | None:
    direct = (row.get("video_id") or "").strip()
    if direct.isdigit():
        return direct
    url = (row.get("url") or "").strip()
    if not url:
        return None
    m = VIDEO_ID_RE.search(url)
    return m.group(1) if m else None


def parse_csv(content: str) -> tuple[list[dict], dict[str, int], list[str]]:
    """Parses Studio CSV content. Returns (rows, column_map, warnings).

    Each row in `rows` is a dict keyed by internal field names with
    parsed values. Rows that lack a recognizable video_id are skipped
    and noted in `warnings`.
    """
    # utf-8-sig strips Excel BOM that Studio sometimes prepends
    reader = csv.reader(io.StringIO(content))
    rows_raw = list(reader)
    if not rows_raw:
        return [], {}, ["empty file"]

    # Some Studio exports prepend metadata rows like "Account: cs2..."
    # before the actual table. Find the first row whose cells contain at
    # least one keyword from our hints — that's the real header.
    header_idx = 0
    for i, row in enumerate(rows_raw[:6]):
        joined = " | ".join(row).lower()
        if any(k in joined for hints in (h for _, h in COLUMN_HINTS) for k in hints):
            header_idx = i
            break

    headers = rows_raw[header_idx]
    col_map = _detect_columns(headers)
    warnings: list[str] = []

    if "url" not in col_map and "video_id" not in col_map:
        return [], col_map, ["No URL or Video ID column found — is this per-video Content Data, not Overview?"]

    if "views" not in col_map and "likes" not in col_map:
        warnings.append("No views or likes column matched — metric extraction may be incomplete.")

    out: list[dict] = []
    for raw_row in rows_raw[header_idx + 1:]:
        if not any(c.strip() for c in raw_row):
            continue
        rec: dict = {}
        for field, idx in col_map.items():
            if idx >= len(raw_row):
                continue
            cell = raw_row[idx]
            if field in METRIC_FIELDS:
                v = _to_number(cell)
                if v is not None:
                    rec[field] = v
            else:
                rec[field] = (cell or "").strip()

        vid = _extract_video_id(rec)
        if not vid:
            continue
        rec["_video_id"] = vid
        out.append(rec)

    return out, col_map, warnings


def _decode_bytes(raw: bytes) -> str:
    """Tries common encodings TikTok Studio uses — UTF-8 (with/without BOM)
    is most common, but Windows clients sometimes save CSV as cp1251."""
    for enc in ("utf-8-sig", "utf-8", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    raise ValueError("Cannot decode file — save as UTF-8 or upload the ZIP as-is.")


def parse_upload(raw: bytes) -> tuple[list[dict], dict[str, int], list[str], str]:
    """Single entry point for an uploaded file. Handles both raw CSVs and the
    Studio 'Download data' ZIP. Returns (rows, col_map, warnings, source_name).

    When given a ZIP, tries every .csv inside and picks the one that yields
    the most rows with a recognizable video_id — Studio archives typically
    bundle Overview / Content / Followers / LIVE CSVs but only Content has
    per-video data with URLs.
    """
    if raw[:2] == b"PK":
        return _parse_from_zip(raw)
    content = _decode_bytes(raw)
    rows, col_map, warnings = parse_csv(content)
    return rows, col_map, warnings, "uploaded.csv"


def _parse_from_zip(raw: bytes) -> tuple[list[dict], dict[str, int], list[str], str]:
    candidates: list[tuple[str, list[dict], dict[str, int], list[str]]] = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
    except zipfile.BadZipFile:
        return [], {}, ["File looks like ZIP but failed to open — re-download from Studio."], "<bad zip>"

    names = [n for n in zf.namelist() if n.lower().endswith(".csv") and not n.startswith("__MACOSX")]
    if not names:
        return [], {}, ["ZIP contains no .csv files."], "<no csv>"

    for name in names:
        try:
            with zf.open(name) as f:
                content = _decode_bytes(f.read())
        except Exception:
            continue
        try:
            rows, col_map, warnings = parse_csv(content)
        except Exception:
            continue
        candidates.append((name, rows, col_map, warnings))

    if not candidates:
        return [], {}, [f"None of the {len(names)} CSVs inside the ZIP could be parsed."], "<no parse>"

    # Pick the CSV with the most rows containing a recognizable video_id —
    # that's the per-video Content Data file. Overview/Followers CSVs typically
    # have 0 such rows because they lack URL columns.
    candidates.sort(key=lambda c: len(c[1]), reverse=True)
    name, rows, col_map, warnings = candidates[0]
    others = [c[0] for c in candidates[1:]]
    enriched = list(warnings)
    if others:
        enriched.append(f"Picked '{name}' from the ZIP (also tried: {', '.join(others)}).")
    if not rows:
        return [], col_map, [
            f"ZIP contained {len(candidates)} CSVs but none had per-video rows. "
            f"Tried: {', '.join(c[0] for c in candidates)}."
        ], name
    return rows, col_map, enriched, name


def apply_to_db(parsed_rows: list[dict]) -> dict:
    """Merges parsed rows into Supabase. Returns summary stats.

    Strategy: for each parsed row, find the matching existing entry by
    video_id (extracted from URL). Update its metric fields and stamp
    `last_studio_import`. Rows whose video isn't in the DB are inserted
    as new entries — that way old Studio data backfills the archive.
    """
    existing = read_entries("tiktok")
    by_vid: dict[str, int] = {}
    for i, e in enumerate(existing):
        url = e.get("url") or ""
        m = VIDEO_ID_RE.search(url)
        if m:
            by_vid[m.group(1)] = i

    now = _now_iso()
    matched = added = 0
    changed: list[dict] = []

    for rec in parsed_rows:
        vid = rec["_video_id"]
        metrics = {k: rec[k] for k in METRIC_FIELDS if k in rec}
        # Cast known integer-ish fields back to int for cleaner UI display.
        for k in ("views", "likes", "comments", "shares", "saves",
                  "profile_visits", "follows", "reach"):
            if k in metrics:
                metrics[k] = int(metrics[k])

        if vid in by_vid:
            entry = dict(existing[by_vid[vid]])
            entry.update(metrics)
            entry["last_studio_import"] = now
            existing[by_vid[vid]] = entry
            changed.append(entry)
            matched += 1
        else:
            # Bootstrap a minimal entry from CSV — we lack title/cover for
            # now, but Sync will fill those in next time.
            entry = {
                "id": int(vid),
                "title": rec.get("title", "") or "",
                "url": rec.get("url") or f"https://www.tiktok.com/video/{vid}",
                "cover_image_url": "",
                "vid_group": "",
                "date": (rec.get("date") or now[:10]),
                "type": "fomo",
                "hook": "",
                "last_studio_import": now,
                **metrics,
            }
            existing.insert(0, entry)
            by_vid[vid] = 0
            changed.append(entry)
            added += 1

    if changed:
        upsert_entries("tiktok", changed)

    return {
        "matched": matched,
        "added": added,
        "rows_parsed": len(parsed_rows),
    }

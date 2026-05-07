import re
from urllib.parse import urlparse, parse_qs

_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


def extract_video_id(url_or_id: str) -> str | None:
    s = (url_or_id or "").strip()
    if not s:
        return None
    if _ID_RE.match(s):
        return s

    try:
        u = urlparse(s if "://" in s else f"https://{s}")
    except ValueError:
        return None

    host = (u.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host.startswith("m."):
        host = host[2:]
    path = u.path

    if host == "youtu.be":
        candidate = path.lstrip("/").split("/", 1)[0]
        return candidate if _ID_RE.match(candidate) else None

    if host in ("youtube.com", "youtube-nocookie.com"):
        if path == "/watch":
            v = parse_qs(u.query).get("v", [None])[0]
            return v if v and _ID_RE.match(v) else None
        for prefix in ("/shorts/", "/embed/", "/v/", "/live/"):
            if path.startswith(prefix):
                candidate = path[len(prefix):].split("/", 1)[0]
                return candidate if _ID_RE.match(candidate) else None

    return None

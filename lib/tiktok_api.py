"""Thin TikTok Display API client.

Endpoints used:
- POST /v2/video/list/   — paginated list of OAuth user's own videos
- POST /v2/video/query/  — fetch specific videos by IDs (used to update metrics
                            when we already know the IDs from prior discovery)

Both expect ?fields= as a query param (CSV) and an empty-or-cursor JSON body.
"""

import requests

VIDEO_LIST_URL = "https://open.tiktokapis.com/v2/video/list/"
VIDEO_QUERY_URL = "https://open.tiktokapis.com/v2/video/query/"

# Fields TikTok will populate on each video object.
VIDEO_FIELDS = (
    "id,title,video_description,create_time,duration,"
    "cover_image_url,share_url,embed_link,"
    "like_count,comment_count,share_count,view_count"
)


class TikTokError(RuntimeError):
    pass


class TikTokClient:
    def __init__(self, access_token: str):
        self.access_token = access_token

    def _post(self, url: str, body: dict | None = None) -> dict:
        r = requests.post(
            url,
            params={"fields": VIDEO_FIELDS},
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            json=body or {},
            timeout=20,
        )
        if not r.ok:
            raise TikTokError(f"{url} -> {r.status_code} {r.reason}: {r.text[:300]}")
        return r.json()

    def list_videos(self, max_count: int = 20, cursor: int | None = None) -> dict:
        body: dict = {"max_count": max_count}
        if cursor is not None:
            body["cursor"] = cursor
        return self._post(VIDEO_LIST_URL, body)

    def query_videos(self, video_ids: list[str]) -> dict:
        return self._post(VIDEO_QUERY_URL, {"filters": {"video_ids": list(video_ids)}})

    def fetch_all_user_videos(self, max_total: int = 500) -> list[dict]:
        """Paginate through every video the OAuth user has uploaded (newest first)."""
        videos: list[dict] = []
        cursor: int | None = None
        while True:
            resp = self.list_videos(max_count=20, cursor=cursor)
            data = (resp or {}).get("data") or {}
            batch = data.get("videos") or []
            videos.extend(batch)
            if len(videos) >= max_total or not data.get("has_more"):
                break
            cursor = data.get("cursor")
            if cursor is None:
                break
        return videos[:max_total]

"""Instagram Graph API client — media list + per-media insights.

The Graph API splits IG content reads in two layers:
- /{ig_user_id}/media        → flat list of all media (paginated)
- /{media_id}/insights       → per-post metrics (different metric sets per media_type)

For Reels (media_type == "VIDEO") the useful metrics in v22 are:
  views, reach, likes, comments, shares, saved, total_interactions

For IMAGE / CAROUSEL_ALBUM, the metric set is slightly different but voronka's
schema is Reels-first, so we surface the same fields and accept zeros for
non-applicable metrics.
"""

import requests

GRAPH_VERSION = "v22.0"
GRAPH_BASE = f"https://graph.facebook.com/{GRAPH_VERSION}"

MEDIA_FIELDS = "id,caption,media_type,media_product_type,permalink,timestamp,like_count,comments_count"
REEL_INSIGHT_METRICS = "views,reach,likes,comments,shares,saved,total_interactions"
IMAGE_INSIGHT_METRICS = "reach,likes,comments,saved,total_interactions"


class InstagramError(RuntimeError):
    pass


class InstagramClient:
    def __init__(self, page_access_token: str, ig_user_id: str):
        self.token = page_access_token
        self.ig_user_id = ig_user_id

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = f"{GRAPH_BASE}/{path}"
        p = {"access_token": self.token, **(params or {})}
        r = requests.get(url, params=p, timeout=20)
        if not r.ok:
            raise InstagramError(f"GET {path} -> {r.status_code}: {r.text[:300]}")
        return r.json()

    def list_media(self, after: str | None = None, limit: int = 25) -> dict:
        params: dict = {"fields": MEDIA_FIELDS, "limit": limit}
        if after:
            params["after"] = after
        return self._get(f"{self.ig_user_id}/media", params)

    def fetch_all_media(self, max_total: int = 500) -> list[dict]:
        items: list[dict] = []
        after: str | None = None
        while True:
            resp = self.list_media(after=after, limit=25)
            data = resp.get("data") or []
            items.extend(data)
            paging = resp.get("paging") or {}
            after = (paging.get("cursors") or {}).get("after")
            if not after or not paging.get("next") or len(items) >= max_total:
                break
        return items[:max_total]

    def fetch_insights(self, media_id: str, media_type: str = "VIDEO") -> dict:
        """Returns a flat dict of metric_name -> value. Empty dict on error."""
        if media_type == "VIDEO":
            metrics = REEL_INSIGHT_METRICS
        else:
            metrics = IMAGE_INSIGHT_METRICS
        try:
            resp = self._get(f"{media_id}/insights", {"metric": metrics})
        except InstagramError:
            return {}
        out: dict = {}
        for row in resp.get("data") or []:
            name = row.get("name")
            values = row.get("values") or []
            if not name or not values:
                continue
            out[name] = values[0].get("value") or 0
        return out

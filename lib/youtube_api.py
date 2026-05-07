from datetime import datetime, timezone
from googleapiclient.discovery import build

ANALYTICS_METRICS = "averageViewPercentage,shares,subscribersGained"
DATA_START = "2005-02-14"


class YouTubeClient:
    def __init__(self, credentials):
        self.data = build("youtube", "v3", credentials=credentials, cache_discovery=False)
        self.analytics = build("youtubeAnalytics", "v2", credentials=credentials, cache_discovery=False)

    def fetch_public(self, video_id: str) -> dict | None:
        resp = self.data.videos().list(
            part="snippet,statistics,contentDetails",
            id=video_id,
        ).execute()
        items = resp.get("items") or []
        if not items:
            return None
        it = items[0]
        sn, st, cd = it.get("snippet", {}), it.get("statistics", {}), it.get("contentDetails", {})
        return {
            "title": sn.get("title", ""),
            "publishedAt": sn.get("publishedAt", ""),
            "duration": cd.get("duration", ""),
            "views": int(st.get("viewCount", 0)),
            "likes": int(st.get("likeCount", 0)),
            "comments": int(st.get("commentCount", 0)),
        }

    def fetch_analytics(self, video_id: str) -> dict:
        end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            resp = self.analytics.reports().query(
                ids="channel==MINE",
                startDate=DATA_START,
                endDate=end,
                metrics=ANALYTICS_METRICS,
                filters=f"video=={video_id}",
            ).execute()
        except Exception:
            return {"retention": 0.0, "shares": 0, "follows": 0}
        rows = resp.get("rows") or []
        if not rows:
            return {"retention": 0.0, "shares": 0, "follows": 0}
        retention, shares, subs = rows[0]
        return {
            "retention": round(float(retention), 1),
            "shares": int(shares),
            "follows": int(subs),
        }

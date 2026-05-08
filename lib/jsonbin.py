import requests

from .env import required

BASE = "https://api.jsonbin.io/v3/b"
# JSONBin appears to 403 some cloud-host PUT requests without a UA. Cheap mitigation.
UA = "voronka-sync/1.0 (+https://voronka-cs2uateam.onrender.com)"


def _explain(resp: "requests.Response", action: str, url: str) -> str:
    body = ""
    try:
        body = resp.text[:300]
    except Exception:
        pass
    return f"{action} {url} -> {resp.status_code} {resp.reason}: {body}"


class JsonBin:
    def __init__(self, bin_id: str, master_key: str):
        self.bin_id = bin_id
        self.headers = {
            "X-Master-Key": master_key,
            "Content-Type": "application/json",
            "User-Agent": UA,
        }

    def read(self) -> dict:
        url = f"{BASE}/{self.bin_id}/latest"
        r = requests.get(url, headers=self.headers, timeout=20)
        if not r.ok:
            raise RuntimeError(_explain(r, "GET", url))
        return r.json()["record"]

    def write(self, record: dict) -> None:
        url = f"{BASE}/{self.bin_id}"
        # X-Bin-Versioning: false → the bin is overwritten in place instead of
        # creating a new version. Free tier caps at 100 versions per bin and
        # then 403's every PUT, which is exactly what we hit. We don't use
        # JSONBin versioning anyway — entry history lives in record.youtube.history[].
        headers = {**self.headers, "X-Bin-Versioning": "false"}
        r = requests.put(url, headers=headers, json=record, timeout=20)
        if not r.ok:
            raise RuntimeError(_explain(r, "PUT", url))


def data_bin() -> JsonBin:
    return JsonBin(required("JSONBIN_BIN_ID"), required("JSONBIN_MASTER_KEY"))


def auth_bin() -> JsonBin:
    return JsonBin(required("JSONBIN_AUTH_BIN_ID"), required("JSONBIN_MASTER_KEY"))

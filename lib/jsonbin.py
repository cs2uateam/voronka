import requests

from .env import required

BASE = "https://api.jsonbin.io/v3/b"


class JsonBin:
    def __init__(self, bin_id: str, master_key: str):
        self.bin_id = bin_id
        self.headers = {
            "X-Master-Key": master_key,
            "Content-Type": "application/json",
        }

    def read(self) -> dict:
        r = requests.get(f"{BASE}/{self.bin_id}/latest", headers=self.headers, timeout=20)
        r.raise_for_status()
        return r.json()["record"]

    def write(self, record: dict) -> None:
        r = requests.put(f"{BASE}/{self.bin_id}", headers=self.headers, json=record, timeout=20)
        r.raise_for_status()


def data_bin() -> JsonBin:
    return JsonBin(required("JSONBIN_BIN_ID"), required("JSONBIN_MASTER_KEY"))


def auth_bin() -> JsonBin:
    return JsonBin(required("JSONBIN_AUTH_BIN_ID"), required("JSONBIN_MASTER_KEY"))

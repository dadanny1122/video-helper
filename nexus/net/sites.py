"""
支援下載的網站清單（V3 D2）。唯一來源是同資料夾的 sites.json：
  ・電腦版的 BRIDGE_HOSTS／BRIDGE_MEDIA_HOSTS 由這裡產生
  ・Nexus 節點下載（video.download）只准這些網站
  ・安卓 vh_mobile.py 直接讀同一份 JSON
打包：build.yml 用 --add-data "nexus/net/sites.json;nexus/net" 放進 EXE。
"""
import json
import os
from functools import lru_cache

SITES_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sites.json")


@lru_cache(maxsize=1)
def load() -> dict:
    with open(SITES_PATH, encoding="utf-8") as f:
        data = json.load(f)
    if data.get("version") != 1 or not isinstance(data.get("sites"), list) or not data["sites"]:
        raise ValueError("sites.json 格式不對")
    for s in data["sites"]:
        if not (s.get("name") and s.get("page") and s.get("media")):
            raise ValueError(f"sites.json 少欄位：{s}")
    return data


def _uniq(items):
    out = []
    for x in items:
        x = x.lower()
        if x not in out:
            out.append(x)
    return tuple(out)


def page_hosts() -> tuple:
    """影片頁網域（照 sites.json 順序、不重複）"""
    return _uniq(h for s in load()["sites"] for h in s["page"])


def media_hosts() -> tuple:
    """影片伺服器網域（照 sites.json 順序、不重複）"""
    return _uniq(h for s in load()["sites"] for h in s["media"])


def host_matches(host: str, domains) -> bool:
    """host 是不是 domains 裡某個網域本身或它的子網域（evilpornhub.com 不算 pornhub.com）"""
    h = (host or "").lower().rstrip(".")
    return bool(h) and any(h == d or h.endswith("." + d) for d in domains)


def site_name(host: str) -> str | None:
    """網域 → 網站名稱（影片頁網域優先），不在清單回 None"""
    for s in load()["sites"]:
        if host_matches(host, s["page"]):
            return s["name"]
    for s in load()["sites"]:
        if host_matches(host, s["media"]):
            return s["name"]
    return None

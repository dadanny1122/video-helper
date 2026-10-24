"""
ThePornDude「影片網站排行」→ Discord 排行榜頻道。
只貼「名次、網站名稱、網域」和原頁面連結（不轉貼它的介紹文字），每週檢查一次；
名次有變化時，直接修改原本那則訊息，並另外發一則「排名變動」。

用法：python rank_feed.py 狀態檔.json [--probe]
"""
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

PAGE = "https://theporndude.com/zh/top-porn-tube-sites"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TOP = 20


def fetch():
    req = urllib.request.Request(PAGE, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(5_000_000).decode("utf-8", "ignore")


def probe():
    html = fetch()
    print(f"::notice title=長度::{len(html)}")
    for key in ("PornHub", "XVideos", "xHamster"):
        i = html.find(key)
        print(f"::notice title={key}::" + (re.sub(r"\s+", " ", html[max(0, i - 300):i + 300]) if i >= 0 else "找不到"))


if __name__ == "__main__" and "--probe" in sys.argv:
    probe()

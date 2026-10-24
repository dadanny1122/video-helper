"""
新聞機器人：用 Google 新聞搜尋這幾個網站的名字，把「新出現的」報導貼到 Discord 新聞頻道。
只貼標題、來源、時間和連結（不貼全文）。看過的會記住，不會重複貼。

用法：
  python news_feed.py 記錄檔.json          抓新聞並貼到 Discord（Webhook：環境變數 DISCORD_NEWS_WEBHOOK）
  python news_feed.py 記錄檔.json --dry    只印出來，不貼
"""
import email.utils
import json
import os
import re
import sys
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

# 要追的網站：名稱、搜尋字、標題裡一定要出現的字（過濾掉不相關的）
TOPICS = [
    ("Pornhub", "Pornhub", r"pornhub|aylo|mindgeek"),
    ("XVideos", "XVideos", r"xvideos"),
    ("MissAV", "MissAV", r"missav"),
    ("Jable", "Jable.tv OR \"Jable\"", r"jable"),
    ("Hanime1", "hanime1 OR \"Hanime1\"", r"hanime"),
]
# 中文（台灣）和英文都搜
EDITIONS = [("zh-TW", "TW", "TW:zh-Hant"), ("en-US", "US", "US:en")]
MAX_AGE_DAYS = 7            # 太舊的不貼
MAX_PER_RUN = 8             # 一次最多貼幾則（避免洗版）
FIRST_RUN_POST = 3          # 第一次執行：每個網站只貼最新的幾則，其他的記成看過
SEEN_MAX = 3000
UA = "Mozilla/5.0 (compatible; VideoHelper-NewsBot/1.0)"


def fetch(topic_q, hl, gl, ceid):
    q = urllib.parse.quote(topic_q + " when:7d")
    url = f"https://news.google.com/rss/search?q={q}&hl={hl}&gl={gl}&ceid={ceid}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        root = ET.fromstring(r.read())
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        link = (it.findtext("link") or "").strip()
        src = (it.findtext("source") or "").strip()
        pub = it.findtext("pubDate") or ""
        try:
            when = email.utils.parsedate_to_datetime(pub)
        except Exception:
            when = None
        if src and title.endswith(" - " + src):          # Google 會把來源接在標題後面
            title = title[: -len(src) - 3]
        out.append({"title": title, "link": link, "source": src, "when": when,
                    "id": (it.findtext("guid") or link)[:300]})
    return out


def norm_title(t):
    return re.sub(r"\W+", "", t.lower())[:80]


def collect():
    now = datetime.now(timezone.utc)
    items, errors = [], []
    for name, q, must in TOPICS:
        for hl, gl, ceid in EDITIONS:
            try:
                for n in fetch(q, hl, gl, ceid):
                    if not re.search(must, n["title"], re.I):
                        continue
                    if n["when"] and now - n["when"] > timedelta(days=MAX_AGE_DAYS):
                        continue
                    n["topic"] = name
                    items.append(n)
            except Exception as e:
                errors.append(f"{name}/{hl}: {e}")
            time.sleep(1)
    # 同一則新聞在中英文、不同網站搜尋都可能出現 → 用標題去重
    seen, uniq = set(), []
    for n in sorted(items, key=lambda x: x["when"] or now, reverse=True):
        k = norm_title(n["title"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(n)
    return uniq, errors


def post(hook, n):
    tw = n["when"].astimezone(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M") if n["when"] else ""
    embed = {"title": n["title"][:250], "url": n["link"], "color": 0x1565C0,
             "description": f"🏷 {n['topic']}　📰 {n['source'] or '新聞'}" + (f"　🕒 {tw}" if tw else "")}
    data = json.dumps({"username": "Video Helper 新聞", "embeds": [embed]}).encode("utf-8")
    for _ in range(5):
        req = urllib.request.Request(hook, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "VideoHelper-CI"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return True
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 2)), 30) + 0.5)
                continue
            print(f"::warning title=Discord 新聞貼文失敗::{e}")
            return False
    return False


def main(path, dry=False):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {"seen": []}
    first_run = not state["seen"]
    seen = set(state["seen"])
    items, errors = collect()
    for e in errors:
        print(f"::warning title=抓新聞失敗::{e}")
    new = [n for n in items if norm_title(n["title"]) not in seen]
    if first_run:                                        # 第一次：每個網站只挑最新幾則
        picked, per = [], {}
        for n in new:
            if per.get(n["topic"], 0) < FIRST_RUN_POST:
                picked.append(n)
                per[n["topic"]] = per.get(n["topic"], 0) + 1
        to_post = picked
    else:
        to_post = new[:MAX_PER_RUN]
    hook = os.environ.get("DISCORD_NEWS_WEBHOOK", "").strip()
    posted = 0
    for n in reversed(to_post):                          # 舊的先貼，頻道裡由舊到新
        print(f"[{n['topic']}] {n['title']} ({n['source']})")
        if dry or not hook:
            continue
        if post(hook, n):
            posted += 1
        time.sleep(1.2)
    # 這次看到的都記成看過（包含沒貼的），之後只貼真的新的
    for n in items:
        seen.add(norm_title(n["title"]))
    state["seen"] = list(seen)[-SEEN_MAX:]
    if not dry and hook:                                 # 還沒設定頻道時不記，設定好後第一次才會正常貼
        json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    by = {}
    for n in items:
        by[n["topic"]] = by.get(n["topic"], 0) + 1
    print(f"::notice title=新聞::找到 {len(items)} 則（{by}），新的 {len(new)} 則，貼出 {posted} 則"
          + ("" if hook else "（沒有設定 DISCORD_NEWS_WEBHOOK，只預覽）"))


if __name__ == "__main__":
    main(sys.argv[1], dry="--dry" in sys.argv)

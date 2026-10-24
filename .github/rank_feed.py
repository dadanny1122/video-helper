"""
ThePornDude「影片網站排行」→ Discord 排行榜頻道。
只貼「名次、網站名稱」和連結（不轉貼它的介紹文字），並標出 Video Helper 支援到什麼程度。
每週檢查一次：排行榜那則訊息直接修改（頻道裡永遠只有一份最新的），名次有變動時另外發一則通知。

用法：python rank_feed.py 狀態檔.json [--probe]
"""
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

PAGE = "https://theporndude.com/zh/top-porn-tube-sites"
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"
TOP = 20

# Video Helper 對每個網站的支援程度（名稱用排行榜上的寫法，比對時不分大小寫）
FULL = {"pornhub", "xvideos", "xnxx", "redtube", "youporn", "xhamster"}                      # 擋廣告＋下載
ADBLOCK = {"eporner", "hqporner", "beeg", "yourporn", "spankbang", "xmoviesforyou", "porntrex",
           "youjizz", "pornone", "3movs", "tube8", "porndig", "cumlouder", "txxx", "porndoe",
           "missav", "jable", "hanime1"}   # 只擋廣告


def fetch():
    req = urllib.request.Request(PAGE, headers={"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.5"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(5_000_000).decode("utf-8", "ignore")


def parse(html):
    """從網頁裡的 schema.org ItemList 讀出排行：[(名次, 名稱, 連結)]"""
    for m in re.finditer(r"<script[^>]+application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            data = json.loads(m.group(1))
        except ValueError:
            continue
        for d in (data if isinstance(data, list) else [data]):
            if isinstance(d, dict) and d.get("@type") == "ItemList" and d.get("itemListElement"):
                out = []
                for it in d["itemListElement"]:
                    if isinstance(it, dict) and it.get("name"):
                        out.append((int(it.get("position") or len(out) + 1), str(it["name"]).strip(), it.get("url") or PAGE))
                return sorted(out)
    return []


def mark(name):
    k = re.sub(r"\s*\(.*\)$", "", name).strip().lower()
    return "✅" if k in FULL else "🛡" if k in ADBLOCK else "　"


def embed_for(rows, now_tw):
    tag = {"✅": "　**⬇ 可下載**", "🛡": "　只擋廣告", "　": "　（沒支援）"}
    lines = [f"`{p:>2}.` {mark(n)} [{n}]({u}){tag[mark(n)]}" for p, n, u in rows[:TOP]]
    can = [n for _, n, _ in rows[:TOP] if mark(n) == "✅"]
    return {
        "title": "🏆 影片網站排行 Top 20（ThePornDude）",
        "url": PAGE,
        "color": 0xF9A825,
        "description": "\n".join(lines) + f"\n\n**⬇ 可以下載的（{len(can)} 個）：**{'、'.join(can)}\n"
                       "其他網站只擋廣告、預估時間，不提供下載。\n"
                       f"🕒 最後檢查：{now_tw}（每週一更新）",
    }


def call(method, url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    for _ in range(5):
        req = urllib.request.Request(url, data=data, method=method,
                                     headers={"Content-Type": "application/json", "User-Agent": "VideoHelper-CI"})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                b = r.read()
                return json.loads(b) if b else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 2)), 30) + 0.5)
                continue
            raise
    raise RuntimeError("Discord 一直說太快")


def changes(old, new):
    """跟上次比：新進榜、掉出榜、名次變動"""
    o = {n: p for p, n, _ in old[:TOP]}
    n_ = {n: p for p, n, _ in new[:TOP]}
    msg = []
    for name, p in n_.items():
        if name not in o:
            msg.append(f"🆕 **{name}** 進榜（第 {p} 名）{mark(name)}")
        elif o[name] != p:
            msg.append(f"{'⬆' if p < o[name] else '⬇'} {name}：{o[name]} → {p}")
    for name, p in o.items():
        if name not in n_:
            msg.append(f"👋 {name} 掉出前 {TOP}（原本第 {p} 名）")
    return msg


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    rows = parse(fetch())
    if len(rows) < 10:
        print(f"::warning title=排行榜::讀不到排行（只讀到 {len(rows)} 筆），網頁可能改版了")
        return
    unsupported = [n for _, n, _ in rows[:TOP] if mark(n) == "　"]
    print(f"::notice title=排行榜::前 {TOP} 名：" + "、".join(n for _, n, _ in rows[:TOP]))
    print(f"::notice title=還沒支援::" + ("、".join(unsupported) or "（全部都支援了）"))
    hook = os.environ.get("DISCORD_RANK_WEBHOOK", "").strip()
    if not hook:
        print("沒有設定 DISCORD_RANK_WEBHOOK，只預覽")
        return
    base = hook.split("?")[0]
    now_tw = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    e = embed_for(rows, now_tw)
    mid = state.get("message_id")
    done = False
    if mid:
        try:
            call("PATCH", f"{base}/messages/{mid}", {"embeds": [e]})
            done = True
        except urllib.error.HTTPError as err:
            print(f"改不到舊的排行榜訊息（{err.code}），重新貼")
    if not done:
        r = call("POST", f"{base}?wait=true", {"username": "Video Helper 排行榜", "embeds": [e]})
        state["message_id"] = r["id"]
    old = [tuple(x) for x in state.get("rows", [])]
    diff = changes(old, rows) if old else []
    if diff:
        call("POST", base, {"username": "Video Helper 排行榜",
                            "embeds": [{"title": "📊 本週排名變動", "url": PAGE, "color": 0xF9A825,
                                        "description": "\n".join(diff)[:4000]}]})
    state["rows"] = [list(x) for x in rows[:TOP]]
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"::notice title=排行榜::已更新；變動 {len(diff)} 筆")


if __name__ == "__main__":
    main(sys.argv[1])

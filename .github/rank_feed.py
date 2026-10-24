"""
ThePornDude「影片網站排行」→ Discord 排行榜頻道。
只貼「名次、網站名稱」和連結（不轉貼它的介紹文字），並標出 Video Helper 支援到什麼程度。
每天晚上 8 點檢查：排行榜那則訊息直接修改（頻道裡永遠只有一份最新的），另外每天發一則「跟昨天比」的名次變動。

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
# 第二個來源：Tranco（學術單位維護的全球網站排名，每天合併多家流量資料；可免費下載，論文常用）
TRANCO_ZIP = "https://tranco-list.eu/top-1m.csv.zip"
TRANCO_API = "https://tranco-list.eu/api/lists/date/latest"
# 排行榜上的名稱 → 網域（找不到的用「名稱.com」猜）
DOMAINS = {"pornhub": "pornhub.com", "xvideos": "xvideos.com", "xhamster": "xhamster.com", "xnxx": "xnxx.com",
           "hqporner": "hqporner.com", "beeg": "beeg.com", "yourporn": "sxyprn.com", "spankbang": "spankbang.com",
           "xmoviesforyou": "xmoviesforyou.com", "porntrex": "porntrex.com", "youjizz": "youjizz.com",
           "redtube": "redtube.com", "youporn": "youporn.com", "pornone": "pornone.com", "3movs": "3movs.com",
           "tube8": "tube8.com", "porndig": "porndig.com", "cumlouder": "cumlouder.com", "txxx": "txxx.com",
           "porndoe": "porndoe.com", "eporner": "eporner.com", "missav": "missav.ws", "jable": "jable.tv",
           "hanime1": "hanime1.me"}
# 交叉比對用：Similarweb 依「實際流量」排的成人網站排行（只附連結，不抓內容）
CROSS = "https://www.similarweb.com/top-websites/adult/"

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


def key(name):
    return re.sub(r"\s*\(.*\)$", "", name).strip().lower()


def domain(name):
    k = key(name)
    return DOMAINS.get(k) or re.sub(r"[^a-z0-9.-]", "", k) + ".com"


def tranco(names):
    """回傳 ({名稱: 全球名次}, 這份清單的連結)；讀不到就回傳 (None, 連結)"""
    import io
    import zipfile
    link = "https://tranco-list.eu/"
    try:
        with urllib.request.urlopen(urllib.request.Request(TRANCO_API, headers={"User-Agent": UA}), timeout=20) as r:
            lid = json.loads(r.read()).get("list_id")
            if lid:
                link = f"https://tranco-list.eu/list/{lid}"
    except Exception as e:
        print(f"Tranco 清單編號讀不到：{e}")
    try:
        with urllib.request.urlopen(urllib.request.Request(TRANCO_ZIP, headers={"User-Agent": UA}), timeout=60) as r:
            z = zipfile.ZipFile(io.BytesIO(r.read(60_000_000)))
        want = {domain(n): n for n in names}
        out = {}
        for line in z.read(z.namelist()[0]).decode("utf-8", "ignore").splitlines():
            rank, _, d = line.strip().partition(",")
            if d in want:
                out[want[d]] = int(rank)
        return out, link
    except Exception as e:
        print(f"::warning title=Tranco::流量排名讀不到：{e}")
        return None, link


def mark(name):
    k = key(name)
    return "✅" if k in FULL else "🛡" if k in ADBLOCK else "　"


def embed_for(rows, now_tw, tr=None, tr_link="https://tranco-list.eu/"):
    tag = {"✅": "　**⬇ 可下載**", "🛡": "　只擋廣告", "　": "　（沒支援）"}
    top = rows[:TOP]
    # 流量名次：這 20 個網站照 Tranco 全球名次重新排，得到「20 個裡第幾」
    order = {}
    if tr:
        for i, n in enumerate(sorted((n for _, n, _ in top if n in tr), key=lambda n: tr[n]), 1):
            order[n] = i

    def flow(n):
        if tr is None:
            return ""
        if n not in order:
            return "｜流量：不在前 100 萬"
        return f"｜流量第 {order[n]}（全球 #{tr[n]:,}）"
    lines = [f"`{p:>2}.` {mark(n)} [{n}]({u}){tag[mark(n)]}{flow(n)}" for p, n, u in top]
    can = [n for _, n, _ in top if mark(n) == "✅"]
    gap = ""
    if order:
        p_of = {n: p for p, n, _ in top}
        big = sorted(order, key=lambda n: -abs(p_of[n] - order[n]))[:3]
        gap = "\n**🔍 兩邊差最多：**" + "、".join(f"{n}（評分第 {p_of[n]}／流量第 {order[n]}）" for n in big
                                                   if p_of[n] != order[n]) + "\n"
    return {
        "title": "🏆 影片網站排行 Top 20（評分 vs 流量）",
        "url": PAGE,
        "color": 0xF9A825,
        "description": "左邊名次＝ThePornDude 評分；「流量第幾」＝同這 20 個網站照 Tranco 全球流量排名重排\n\n"
                       + "\n".join(lines) + "\n" + gap + f"\n**⬇ 可以下載的（{len(can)} 個）：**{'、'.join(can)}\n"
                       "其他網站只擋廣告、預估時間，不提供下載。\n"
                       f"🕒 最後檢查：{now_tw}（每天晚上 8 點更新）\n\n"
                       f"📎 **資料來源：**[ThePornDude 影片網站排行]({PAGE})\n"
                       "　⚠ 單一網站的排行（站長自己評分，可能有廣告合作），僅供參考\n"
                       f"📎 **流量來源：**[Tranco 全球網站排名]({tr_link})（學術單位維護，合併多家流量資料）"
                       + ("" if tr is not None else "　⚠ 今天讀不到") + "\n"
                       f"📎 也可以對照：[Similarweb 成人網站流量排行]({CROSS})",
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
    tr, tr_link = tranco([n for _, n, _ in rows[:TOP]])
    print("::notice title=Tranco::" + ("讀不到" if tr is None else
          "、".join(f"{n}={tr.get(n, '無')}" for _, n, _ in rows[:TOP])))
    hook = os.environ.get("DISCORD_RANK_WEBHOOK", "").strip()
    if os.environ.get("PREVIEW") == "true":
        print("::notice title=預覽::" + json.dumps(embed_for(rows, "預覽", tr, tr_link), ensure_ascii=False)[:3500])
        return
    if not hook:
        print("沒有設定 DISCORD_RANK_WEBHOOK，只預覽")
        return
    base = hook.split("?")[0]
    now_tw = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    e = embed_for(rows, now_tw, tr, tr_link)
    mid = state.get("message_id")
    done = False
    if mid and os.environ.get("REPOST") == "true":    # 手動要求「重新貼一份新的」：刪掉舊的那則
        try:
            call("DELETE", f"{base}/messages/{mid}")
        except urllib.error.HTTPError as err:
            print(f"刪不到舊的排行榜訊息（{err.code}），直接貼新的")
        mid = None
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
    # 每天都發一則：有變動列出來，沒變動也說一聲（第一次跑沒有昨天的資料可比，就不發）
    if old:
        body = "\n".join(diff)[:4000] if diff else "今天前 20 名都沒有變動 👌"
        call("POST", base, {"username": "Video Helper 排行榜",
                            "embeds": [{"title": f"📊 今日排名變動（{now_tw[:10]}，跟昨天比）", "url": PAGE,
                                        "color": 0xF9A825, "description": body + f"\n\n📎 來源：[ThePornDude]({PAGE})（單一網站，僅供參考）"}]})
    state["rows"] = [list(x) for x in rows[:TOP]]
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"::notice title=排行榜::已更新；變動 {len(diff)} 筆")


if __name__ == "__main__":
    main(sys.argv[1])

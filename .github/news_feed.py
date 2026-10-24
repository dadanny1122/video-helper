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

# 「會影響使用者」的新聞會出現的字：封鎖、法規、訴訟、罰款、年齡驗證、資料外洩…
IMPACT = (r"(ban|bans|banned|block|blocked|law|laws|bill|court|lawsuit|sue|sued|fine|fined|regulat|"
          r"age.?verif|age check|shut|outage|hack|breach|leak of|data|policy|investigat|"
          r"封鎖|禁|法|訴|罰|驗證|關閉|外洩|駭|調查|政策)")

# 要追的網站：名稱、搜尋字、標題裡一定要出現的字
#   名字很特別的（Pornhub、XVideos…）：內文提到就算，不限標題（中文新聞標題常常不寫網站名）
#   名字是常見字的（Jable）：標題一定要有，不然會混進很多不相關的新聞
TOPICS = [
    ("Pornhub", "\"Pornhub\"", None),
    ("XVideos", "\"XVideos\"", None),
    ("MissAV", "\"MissAV\"", None),
    ("Jable", "\"Jable\"", r"jable"),
    ("Hanime1", "\"hanime1\" OR \"hanime\"", None),
    ("XNXX", "\"XNXX\"", r"xnxx.*" + IMPACT + "|" + IMPACT + ".*xnxx"),   # 垃圾網頁很多：標題要有名字＋影響類的字
    ("xHamster", "\"xHamster\"", None),
    ("OnlyFans", "\"OnlyFans\"", r"onlyfans.*" + IMPACT + "|" + IMPACT + ".*onlyfans"),  # 新聞太多：只要跟法規、封鎖有關的
    ("Aylo", "\"Aylo\"", r"aylo|pornhub"),              # Pornhub 的母公司
    # 年齡驗證法規：最常讓成人網站在某些地區被封鎖的原因
    ("年齡驗證法規", "\"age verification\" porn OR 年齡驗證 成人網站",
     r"age.?verif|年齡驗證|年龄验证|age check"),
]
# 中文（台灣）和英文都搜
EDITIONS = [("zh-TW", "TW", "TW:zh-Hant"), ("en-US", "US", "US:en")]
MAX_AGE_DAYS = 7            # 太舊的不貼
MAX_PER_RUN = 10            # 一次最多貼幾則（避免洗版）
MAX_PER_TOPIC = 2           # 每個網站一次最多貼幾則（OnlyFans 這種新聞很多的才不會洗版）
FIRST_RUN_POST = 3          # 第一次執行：每個網站只貼最新的幾則，其他的記成看過
SEEN_MAX = 3000
UA = "Mozilla/5.0 (compatible; VideoHelper-NewsBot/1.0)"

# ---- 擋掉垃圾、釣魚「新聞」----
# 1. 被駭的政府、學校網站：正常不會出現成人網站的新聞（例如 town.xxx.lg.jp 被塞了廣告頁）
BAD_SOURCE = re.compile(r"(\.|^)(gov|edu|mil|lg\.jp|go\.jp|ac\.[a-z]{2}|gov\.[a-z]{2}|edu\.[a-z]{2}|"
                        r"go\.[a-z]{2}|or\.jp|ed\.jp)$", re.I)
# 2. 網址裡藏了程式碼或跳轉（data:、base64、krpano 漏洞、javascript:）
BAD_LINK = re.compile(r"data:|base64|krpano|javascript:|%3cscript|<script|xml=|\.xml\b|[?&](url|redirect|goto)=http",
                      re.I)
# 3. 廣告業配、SEO 垃圾文常見的標題（「XX 推薦」「實測排名」「免費替代」）
BAD_TITLE = re.compile(r"\b(tested|ranked|ranking|best|top ?\d+|alternatives?|free|apps?|generators?|"
                       r"chat|sites like|reviews?|coupon|discount|promo|viral|bokep|terbaru|nonton|download|"
                       r"full video|leaked?|link video|watch online|indo|desi|mms)\b|推薦|排行|替代|免費看|優惠|外流|完整版", re.I)


# 4. 只收「認識的新聞媒體」：Google 新聞裡很多是假裝成新聞的垃圾、釣魚網頁，不認識的一律不貼
TRUSTED = {
    # 國際
    "reuters.com", "apnews.com", "bbc.com", "bbc.co.uk", "theguardian.com", "nytimes.com", "washingtonpost.com",
    "cnn.com", "nbcnews.com", "cbsnews.com", "abcnews.go.com", "npr.org", "bloomberg.com", "ft.com", "wsj.com",
    "economist.com", "axios.com", "politico.com", "politico.eu", "thehill.com", "usatoday.com", "latimes.com",
    "independent.co.uk", "telegraph.co.uk", "sky.com", "news.sky.com", "cnbc.com", "forbes.com", "businessinsider.com",
    "time.com", "newsweek.com", "theatlantic.com", "vox.com", "nymag.com", "rollingstone.com", "variety.com",
    "lemonde.fr", "france24.com", "rfi.fr", "dw.com", "euronews.com", "spiegel.de", "lefigaro.fr",
    "japantimes.co.jp", "nikkei.com", "asia.nikkei.com", "scmp.com", "straitstimes.com", "channelnewsasia.com",
    "abc.net.au", "smh.com.au", "theage.com.au", "cbc.ca", "theglobeandmail.com", "nzherald.co.nz", "aljazeera.com",
    # 科技
    "theverge.com", "wired.com", "arstechnica.com", "techcrunch.com", "engadget.com", "404media.co", "vice.com",
    "theregister.com", "cnet.com", "zdnet.com", "gizmodo.com", "mashable.com", "techradar.com", "tomsguide.com",
    "pcmag.com", "bleepingcomputer.com", "therecord.media", "techspot.com", "9to5mac.com", "androidauthority.com",
    "restofworld.org", "eff.org", "theinformation.com", "fastcompany.com", "digitaltrends.com", "lifehacker.com",
    # 成人產業媒體（產業、法規新聞）
    "xbiz.com", "avn.com",
    # 台灣、港澳、中文
    "ltn.com.tw", "udn.com", "chinatimes.com", "ettoday.net", "setn.com", "tvbs.com.tw", "cna.com.tw", "storm.mg",
    "nownews.com", "ctwant.com", "mirrormedia.mg", "businesstoday.com.tw", "bnext.com.tw", "technews.tw",
    "inside.com.tw", "ithome.com.tw", "tw.news.yahoo.com", "news.yahoo.co.jp", "cw.com.tw", "newtalk.tw",
    "ftvnews.com.tw", "ttv.com.tw", "ctee.com.tw", "news.pts.org.tw", "pts.org.tw", "rti.org.tw", "taiwannews.com.tw",
    "taipeitimes.com", "focustaiwan.tw", "hk01.com", "mingpao.com", "rthk.hk", "news.rthk.hk", "hket.com",
    "on.cc", "zaobao.com.sg", "zaobao.com", "8world.com", "thenewslens.com", "twreporter.org", "gvm.com.tw",
    "managertoday.com.tw", "kocpc.com.tw", "4gamers.com.tw", "gamebase.com.tw", "yahoo.com",
    # 日本、韓國
    "nhk.or.jp", "asahi.com", "mainichi.jp", "yomiuri.co.jp", "itmedia.co.jp", "gigazine.net", "koreaherald.com",
    "koreatimes.co.kr", "yna.co.kr",
}


def trusted(host):
    host = host.lower().removeprefix("www.")
    return any(host == d or host.endswith("." + d) for d in TRUSTED)


def host_of(u):
    try:
        return (urllib.parse.urlparse(u).hostname or "").lower()
    except Exception:
        return ""


def is_junk(n):
    """回傳擋掉的原因；正常新聞回傳空字串"""
    src_host = host_of(n.get("source_url", ""))
    if not src_host or not trusted(src_host):
        return f"不是認識的新聞媒體（{src_host or '沒有來源'}）"
    for u in (n["link"], n.get("source_url", "")):
        h = host_of(u)
        if h and h != "news.google.com" and BAD_SOURCE.search(h):
            return f"來源是政府／學校網站（{h}），可能被駭"
        if BAD_LINK.search(urllib.parse.unquote(u)):
            return "網址裡藏了程式碼或跳轉"
    if BAD_TITLE.search(n["title"]):
        return "像是業配或 SEO 垃圾文"
    return ""


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
        src_el = it.find("source")
        src = (src_el.text or "").strip() if src_el is not None else ""
        src_url = src_el.get("url", "") if src_el is not None else ""
        pub = it.findtext("pubDate") or ""
        try:
            when = email.utils.parsedate_to_datetime(pub)
        except Exception:
            when = None
        if src and title.endswith(" - " + src):          # Google 會把來源接在標題後面
            title = title[: -len(src) - 3]
        out.append({"title": title, "link": link, "source": src, "source_url": src_url, "when": when,
                    "id": (it.findtext("guid") or link)[:300]})
    return out


# ---- 確認真正的發布日期 ----
# Google 新聞給的時間有時是「被更新的時間」（舊文章改一下就變新的），所以貼之前打開原文看媒體自己寫的日期

def resolve_google_link(link):
    """把 news.google.com/rss/articles/… 換成原文網址；失敗回傳 None"""
    m = re.search(r"/articles/([^?/]+)", link)
    if not m or "news.google.com" not in link:
        return link
    aid = m.group(1)
    try:
        html = urllib.request.urlopen(urllib.request.Request(
            f"https://news.google.com/rss/articles/{aid}", headers={"User-Agent": UA}), timeout=20).read().decode("utf-8", "ignore")
        sg = re.search(r'data-n-a-sg="([^"]+)"', html)
        ts = re.search(r'data-n-a-ts="([^"]+)"', html)
        if not (sg and ts):
            return None
        inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                                            None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                            aid, int(ts.group(1)), sg.group(1)])
        body = urllib.parse.urlencode({"f.req": json.dumps([[["Fbv4je", inner, None, "generic"]]])}).encode()
        r = urllib.request.urlopen(urllib.request.Request(
            "https://news.google.com/_/DotsSplashUi/data/batchexecute", data=body, method="POST",
            headers={"User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"}),
            timeout=20).read().decode("utf-8", "ignore")
        u = re.search(r'\\"(https?://[^"\\]+)\\"', r)
        return u.group(1) if u else None
    except Exception as e:
        print(f"換原文網址失敗：{e}")
        return None


DATE_PATTERNS = [
    r'<meta[^>]+(?:property|name|itemprop)=["\'](?:article:published_time|og:published_time|datePublished|'
    r'pubdate|publishdate|date|dc\.date|sailthru\.date|parsely-pub-date)["\'][^>]+content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+(?:property|name|itemprop)=["\'](?:article:published_time|datePublished)',
    r'"datePublished"\s*:\s*"([^"]+)"',
    r'<time[^>]+datetime=["\']([^"\']+)',
]


def parse_date(t):
    t = t.strip()
    for fmt in (None,):
        try:
            d = datetime.fromisoformat(t.replace("Z", "+00:00"))
            return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    try:
        return email.utils.parsedate_to_datetime(t)
    except Exception:
        pass
    m = re.match(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", t)
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    return None


def real_published(url):
    """打開原文，找媒體自己寫的發布時間；找不到回傳 None"""
    try:
        html = urllib.request.urlopen(urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36"}),
            timeout=20).read(1_500_000).decode("utf-8", "ignore")
    except Exception as e:
        print(f"打不開原文：{url[:80]}（{e}）")
        return None
    for pat in DATE_PATTERNS:
        for m in re.finditer(pat, html, re.I):
            d = parse_date(m.group(1))
            if d and d.year > 2000:
                return d
    m = re.search(r"/(20\d\d)[/-](\d\d)[/-](\d\d)/", url)                 # 網址裡有日期（例如 /2026/05/12/）
    if m:
        return datetime(int(m[1]), int(m[2]), int(m[3]), tzinfo=timezone.utc)
    return None


def verify(n, now):
    """貼之前確認：換成原文網址、看真正的發布日期。回傳 False 表示太舊不要貼"""
    real = resolve_google_link(n["link"])
    if real and real.startswith("http"):
        if not trusted(host_of(real)) or BAD_LINK.search(urllib.parse.unquote(real)):
            print(f"原文網址可疑，不貼：{real[:100]}")
            return False
        n["link"] = real
        d = real_published(real)
        if d:
            print(f"原文日期 {d.date()}：{n['title'][:50]}")
            if now - d > timedelta(days=MAX_AGE_DAYS):
                print(f"太舊了，不貼：{n['title'][:60]}（{d.date()}）")
                return False
            n["when"] = d
    return True


def norm_title(t):
    return re.sub(r"\W+", "", t.lower())[:80]


def collect():
    now = datetime.now(timezone.utc)
    items, errors = [], []
    for name, q, must in TOPICS:
        for hl, gl, ceid in EDITIONS:
            try:
                got = fetch(q, hl, gl, ceid)
                print(f"抓到 {name}/{hl}：{len(got)} 則" +
                      (f"，例：{got[0]['title'][:60]}（{got[0]['when']}）" if got else ""))
                for n in got:
                    if must and not re.search(must, n["title"], re.I):
                        continue
                    if n["when"] and now - n["when"] > timedelta(days=MAX_AGE_DAYS):
                        continue
                    junk = is_junk(n)
                    if junk:
                        print(f"擋掉：{n['title'][:60]}｜{n['source_url'] or n['link'][:80]}｜{junk}")
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


def main(path, dry=False, mark_only=False):
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
        to_post, per = [], {}
        for n in new:
            if per.get(n["topic"], 0) < MAX_PER_TOPIC and len(to_post) < MAX_PER_RUN:
                to_post.append(n)
                per[n["topic"]] = per.get(n["topic"], 0) + 1
    hook = os.environ.get("DISCORD_NEWS_WEBHOOK", "").strip()
    now = datetime.now(timezone.utc)
    to_post = [n for n in to_post if verify(n, now)]
    posted = 0
    for n in reversed(to_post):                          # 舊的先貼，頻道裡由舊到新
        print(f"[{n['topic']}] {n['title']} ({n['source']})")
        if dry or not hook or mark_only:
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


def test_verify():
    """測試用：拿目前找到的前幾則，看換原文網址、讀日期有沒有成功（不貼、不記）"""
    items, _ = collect()
    now = datetime.now(timezone.utc)
    ok = 0
    for n in items[:6]:
        g = n["when"]
        keep = verify(n, now)
        real = not n["link"].startswith("https://news.google.com")
        ok += real
        print(f"::notice title=日期檢查::{'✓原文' if real else '✗還是 Google 連結'}｜Google 說 {g.date() if g else '?'}"
              f"｜原文 {n['when'].date() if n['when'] else '?'}｜{'貼' if keep else '不貼'}｜{n['link'][:70]}")
    print(f"::notice title=日期檢查結果::{ok}/{min(6, len(items))} 則換到原文網址")


if __name__ == "__main__":
    if "--test-verify" in sys.argv:
        test_verify()
    else:
        main(sys.argv[1], dry="--dry" in sys.argv, mark_only="--mark-only" in sys.argv)

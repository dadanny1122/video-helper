"""
外幣匯率 → Discord 匯率頻道。
資料：臺灣銀行牌告匯率（官方公開 CSV）；另附 TradingView 走勢圖連結（只附連結，不抓它的資料）。
做法：頻道裡「匯率表」那則訊息直接修改（永遠只有一份最新的）；跟前一個營業日比，漲跌超過門檻才另發一則提醒。

用法：python fx_feed.py 狀態檔.json
環境變數：DISCORD_FX_WEBHOOK（沒設定就只預覽）、PREVIEW=true（只印出來）
"""
import csv
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

CSV_URL = "https://rate.bot.com.tw/xrt/flcsv/0/day"
PAGE = "https://rate.bot.com.tw/xrt?Lang=zh-TW"
UA = "Mozilla/5.0 (compatible; VideoHelper-FxBot/1.0)"
# 要追的貨幣：代碼 → (中文、國旗)
WATCH = {"USD": ("美金", "🇺🇸"), "EUR": ("歐元", "🇪🇺"), "JPY": ("日圓", "🇯🇵"), "CNY": ("人民幣", "🇨🇳"),
         "GBP": ("英鎊", "🇬🇧"), "HKD": ("港幣", "🇭🇰"), "CHF": ("瑞士法郎", "🇨🇭"), "AUD": ("澳幣", "🇦🇺"),
         "KRW": ("韓元", "🇰🇷")}
ALERT_PCT = 0.5          # 跟前一個營業日比，漲跌超過 0.5% 就另發提醒
TW = timezone(timedelta(hours=8))
DAY_PAGE = "https://rate.bot.com.tw/xrt/quote/day/{}"   # 台銀官方：這種貨幣今天每一次掛牌的時間和價格


def quote_time():
    """台銀網頁上的「牌價最新掛牌時間」；讀不到回傳 None"""
    import re
    try:
        req = urllib.request.Request(PAGE, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            html = r.read(2_000_000).decode("utf-8", "ignore")
        m = re.search(r"掛牌時間[^0-9]{0,80}(\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2})", html)
        return m.group(1) if m else None
    except Exception as e:
        print(f"讀不到掛牌時間：{e}")
        return None


def fetch():
    req = urllib.request.Request(CSV_URL, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
    text = raw.decode("utf-8-sig", "ignore")
    rates = {}
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 14 or row[0].strip() not in WATCH:
            continue
        def num(x):
            try:
                v = float(x)
                return v if v > 0 else None
            except ValueError:
                return None
        # 欄位：幣別, 本行買入, 現金, 即期, 遠期…(7 欄), 本行賣出, 現金, 即期, …
        rates[row[0].strip()] = {"cash_buy": num(row[2]), "spot_buy": num(row[3]),
                                 "cash_sell": num(row[12]), "spot_sell": num(row[13])}
    return rates


HIST_URL = "https://rate.bot.com.tw/xrt/flcsv/0/L3M/{}"   # 台銀：過去 3 個月每天的牌價
DAYS = 30                 # 「便宜還是貴」看最近幾天
CALC_TWD = 10000          # 換匯試算：拿多少台幣去換


def history(code):
    """過去 3 個月每天的 [日期, 即期中間價, 現金賣出]（舊 → 新）"""
    req = urllib.request.Request(HIST_URL.format(code), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        text = r.read().decode("utf-8-sig", "ignore")
    out = []
    for row in csv.reader(io.StringIO(text)):
        if len(row) < 15 or not row[0].isdigit():
            continue
        try:
            sb, ss = float(row[4] or 0), float(row[14] or 0)
            cb, cs = float(row[3] or 0), float(row[13] or 0)
        except ValueError:
            continue
        m = (sb + ss) / 2 if sb and ss else (cb + cs) / 2 if cb and cs else None
        if m:
            out.append([row[0], m, cs or None])
    return sorted(out)


def recent(hist, today):
    """最近 DAYS 天（日曆天）的資料"""
    cut = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=DAYS)).strftime("%Y%m%d")
    return [h for h in hist if h[0] >= cut]


def position(m, hist):
    """今天的價格在最近 30 天裡算便宜還是貴（對「拿台幣去換外幣」的人來說，越低越划算）"""
    vals = [h[1] for h in hist] + [m]
    rank = sorted(vals).index(m) + 1           # 第幾便宜
    n = len(vals)
    pct = (rank - 1) / max(n - 1, 1)
    tag = "🟢 偏便宜" if pct <= 0.2 else "🔴 偏貴" if pct >= 0.8 else "🟡 普通"
    return f"{tag}（{DAYS} 天內第 {rank} 便宜／共 {n} 天）", pct


def money(v, code):
    return f"{v:,.0f}" if code in ("JPY", "KRW") else f"{v:,.2f}"


def mid(r):
    """用「即期」買賣的中間價當作代表（沒有即期就用現金）"""
    a, b = r.get("spot_buy") or r.get("cash_buy"), r.get("spot_sell") or r.get("cash_sell")
    return (a + b) / 2 if a and b else None


def fmt(v):
    return "—" if v is None else (f"{v:.4f}" if v < 1 else f"{v:.3f}" if v < 10 else f"{v:.2f}")


def embed_for(rates, prev, now_tw, qtime=None, hists=None):
    hists = hists or {}
    lines = []
    for code, (zh, flag) in WATCH.items():
        r = rates.get(code)
        if not r:
            continue
        m, p = mid(r), prev.get(code)
        chg = ""
        if m and p:
            pct = (m - p) / p * 100
            arrow = "🔺" if pct > 0.05 else "🔻" if pct < -0.05 else "▫"
            chg = f"　{arrow} {pct:+.2f}%"
        tv = f"https://www.tradingview.com/symbols/{code}TWD/"
        extra = ""
        h = hists.get(code) or []
        if m and h:
            extra += "\n　" + position(m, h)[0]
        if r.get("cash_sell"):
            got = CALC_TWD / r["cash_sell"]
            avg = [x[2] for x in h if x[2]]
            diff = ""
            if avg:
                d = got - CALC_TWD / (sum(avg) / len(avg))
                diff = f"（比 {DAYS} 天平均{'多' if d >= 0 else '少'} {money(abs(d), code)}）"
            extra += f"\n　💵 {CALC_TWD // 10000} 萬台幣換現鈔 → **{money(got, code)} {zh}**{diff}"
        lines.append(f"{flag} **{zh} {code}**{chg}　[各時段牌價]({DAY_PAGE.format(code)})｜[走勢]({tv})\n"
                     f"　現金 買 {fmt(r['cash_buy'])}／賣 {fmt(r['cash_sell'])}　"
                     f"即期 買 {fmt(r['spot_buy'])}／賣 {fmt(r['spot_sell'])}" + extra)
    return {
        "title": "💱 外幣匯率（1 外幣 = 多少台幣）",
        "url": PAGE,
        "color": 0x2E7D32,
        "description": f"📎 **資料來源：[臺灣銀行牌告匯率]({PAGE})**（官方公開資料）\n"
                       f"🏦 台銀掛牌時間：**{qtime or '讀不到，請點來源確認'}**\n"
                       "⏰ 想知道某個時段的實際價格 → 點各貨幣的「今日各時段牌價」（台銀官方，每次掛牌都有記錄）\n\n"
                       + "\n".join(lines) + "\n\n"
                       "「買」＝銀行跟你買（你賣外幣拿到的台幣）；「賣」＝銀行賣你（你買外幣要付的台幣）\n"
                       "🔺🔻＝跟前一個營業日比（用即期中間價）\n"
                       f"🟢🟡🔴＝現在的價格在最近 {DAYS} 天裡算便宜還是貴（給要換外幣的人參考，不是買賣建議）\n"
                       f"💵＝{CALC_TWD:,} 台幣用現鈔賣出價換得到多少\n"
                       "📈 走勢圖：TradingView（國際市場價，跟銀行牌價會有一點差距）\n"
                       f"🕒 最後更新：{now_tw}（平日 9:00～16:30 每 30 分鐘）",
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


def chart(rates, hists, path):
    """畫最近 30 天的走勢（各貨幣都換成「漲跌 %」才能放在同一張圖比較）；失敗回傳 False"""
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates
    except Exception as e:
        print(f"沒有 matplotlib，不畫圖：{e}")
        return False
    fig, ax = plt.subplots(figsize=(9, 4.6), dpi=110)
    fig.patch.set_facecolor("#2b2d31")
    ax.set_facecolor("#2b2d31")
    colors = {"USD": "#4fc3f7", "EUR": "#ffb74d", "JPY": "#e57373", "CNY": "#81c784", "GBP": "#ba68c8"}
    for code, col in colors.items():
        h = hists.get(code) or []
        if len(h) < 3:
            continue
        xs = [datetime.strptime(d, "%Y%m%d") for d, _, _ in h]
        ys = [v for _, v, _ in h]
        m = mid(rates.get(code, {})) if rates.get(code) else None
        if m:
            xs.append(datetime.now(TW).replace(tzinfo=None))
            ys.append(m)
        base = ys[0]
        ax.plot(xs, [(y / base - 1) * 100 for y in ys], color=col, linewidth=2, label=code)
    ax.axhline(0, color="#80848e", linewidth=0.8, linestyle="--")
    ax.set_title(f"Last {DAYS} days vs TWD (% change, Bank of Taiwan)", color="#f2f3f5", fontsize=12)
    ax.set_ylabel("%", color="#b5bac1")
    ax.tick_params(colors="#b5bac1")
    ax.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
    for sp in ax.spines.values():
        sp.set_color("#4e5058")
    ax.grid(color="#3f4147", linewidth=0.6)
    ax.legend(facecolor="#2b2d31", edgecolor="#4e5058", labelcolor="#f2f3f5", ncol=5, loc="upper left")
    fig.tight_layout()
    fig.savefig(path, facecolor=fig.get_facecolor())
    plt.close(fig)
    return True


def post_summary(base, rates, prev, hists, today):
    """每日收盤總結：今天漲最多、跌最多、30 天最便宜／最貴的，附走勢圖"""
    moves = []
    for code, r in rates.items():
        m, p = mid(r), prev.get(code)
        if m and p:
            moves.append(((m - p) / p * 100, code))
    moves.sort()
    lines = []
    if moves:
        up, dn = moves[-1], moves[0]
        lines.append(f"🔺 漲最多：{WATCH[up[1]][1]} {WATCH[up[1]][0]} {up[0]:+.2f}%")
        lines.append(f"🔻 跌最多：{WATCH[dn[1]][1]} {WATCH[dn[1]][0]} {dn[0]:+.2f}%")
    pos = []
    for code, r in rates.items():
        m, h = mid(r), hists.get(code) or []
        if m and h:
            pos.append((position(m, h)[1], code))
    cheap = [c for p, c in sorted(pos) if p <= 0.2]
    dear = [c for p, c in sorted(pos, reverse=True) if p >= 0.8]
    if cheap:
        lines.append(f"🟢 {DAYS} 天內偏便宜：" + "、".join(f"{WATCH[c][1]}{WATCH[c][0]}" for c in cheap))
    if dear:
        lines.append(f"🔴 {DAYS} 天內偏貴：" + "、".join(f"{WATCH[c][1]}{WATCH[c][0]}" for c in dear))
    lines.append(f"\n📎 資料來源：[臺灣銀行牌告匯率]({PAGE})｜只是整理數字，不是買賣建議")
    embed = {"title": f"📊 今日匯率總結（{today}）", "url": PAGE, "color": 0x1565C0, "description": "\n".join(lines)}
    img = "/tmp/fx.png"
    if chart(rates, hists, img):
        embed["image"] = {"url": "attachment://fx.png"}
        boundary = "----vhfx" + str(int(time.time()))
        payload = json.dumps({"username": "Video Helper 匯率", "embeds": [embed]}).encode("utf-8")
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
                f"Content-Type: application/json\r\n\r\n").encode() + payload + \
               (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"fx.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + open(img, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(base, data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                              "User-Agent": "VideoHelper-CI"})
        urllib.request.urlopen(req, timeout=30).read()
    else:
        call("POST", base, {"username": "Video Helper 匯率", "embeds": [embed]})
    print("::notice title=匯率總結::已發出")


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    rates = fetch()
    if len(rates) < 3:
        print(f"::warning title=匯率::讀不到臺灣銀行的匯率（只讀到 {len(rates)} 種）")
        return
    now = datetime.now(TW)
    today, now_tw = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M")
    # 「前一個營業日」的收盤：每天第一次跑時，把昨天最後一次的價格存成比較基準
    if state.get("day") != today:
        state["prev"] = state.get("last", {}) or state.get("prev", {})
        state["day"] = today
        state["alerted"] = []
    prev = state.get("prev", {})
    # 歷史資料一天抓一次就好（台銀一天只多一筆）
    if state.get("hist_day") != today or not state.get("hists"):
        hists = {}
        for code in WATCH:
            try:
                hists[code] = recent(history(code), today)
            except Exception as err:
                print(f"::warning title=匯率歷史::{code} 讀不到：{err}")
        state["hists"], state["hist_day"] = hists, today
    hists = state["hists"]
    qtime = quote_time()
    e = embed_for(rates, prev, now_tw, qtime, hists)
    if len(e["description"]) > 4000:             # Discord 一則最多 4096 字：太長就拿掉走勢連結
        e["description"] = e["description"].replace("｜[走勢](https://www.tradingview.com/symbols/", "｜[").replace("TWD/)", ")")
    print(f"::notice title=匯率表長度::{len(e['description'])} 字")
    print("::notice title=匯率::" + "｜".join(f"{c}={fmt(mid(r))}" for c, r in rates.items()))
    hook = os.environ.get("DISCORD_FX_WEBHOOK", "").strip()
    if os.environ.get("PREVIEW") == "true" or not hook:
        print("::notice title=預覽::" + json.dumps(e, ensure_ascii=False)[:3000])
        return
    base = hook.split("?")[0]
    done = False
    if state.get("message_id"):
        try:
            call("PATCH", f"{base}/messages/{state['message_id']}", {"embeds": [e]})
            done = True
        except urllib.error.HTTPError as err:
            print(f"改不到舊的匯率訊息（{err.code}），重新貼")
    if not done:
        state["message_id"] = call("POST", f"{base}?wait=true", {"username": "Video Helper 匯率", "embeds": [e]})["id"]
    # 大漲大跌提醒：同一種貨幣一天只提醒一次
    alerts = []
    for code, r in rates.items():
        m, p = mid(r), prev.get(code)
        if m and p and abs((m - p) / p * 100) >= ALERT_PCT and code not in state["alerted"]:
            zh, flag = WATCH[code]
            pct = (m - p) / p * 100
            alerts.append(f"{flag} **{zh}** {'升值' if pct > 0 else '貶值'} {abs(pct):.2f}%（{fmt(p)} → {fmt(m)}）")
            state["alerted"].append(code)
    if alerts:
        call("POST", base, {"username": "Video Helper 匯率",
                            "embeds": [{"title": f"⚠ 匯率大變動（跟前一個營業日比，超過 {ALERT_PCT}%）",
                                        "url": PAGE, "color": 0xC62828, "description": "\n".join(alerts)}]})
    # 每日收盤總結：下午 4 點半以後第一次跑（或手動勾 summary）時發一則，附 30 天走勢圖
    if (now.hour * 60 + now.minute >= 16 * 60 + 30 and state.get("summary_day") != today) \
            or os.environ.get("SUMMARY") == "true":
        post_summary(base, rates, prev, hists, today)
        state["summary_day"] = today
    state["last"] = {c: mid(r) for c, r in rates.items() if mid(r)}
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"::notice title=匯率::已更新；提醒 {len(alerts)} 則")


def probe():
    for u in ("https://rate.bot.com.tw/xrt/flcsv/0/L3M/USD", "https://rate.bot.com.tw/xrt/flcsv/0/L3M/JPY"):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30) as r:
                t = r.read().decode("utf-8-sig", "ignore").splitlines()
            print(f"::notice title=歷史 {u[-3:]}::{len(t)} 行｜" + " ⏎ ".join(t[:3]) + " ⏎ … ⏎ " + t[-1])
        except Exception as e:
            print(f"::notice title=歷史 {u[-3:]}::{e}")


if __name__ == "__main__":
    if os.environ.get("PROBE") == "true":
        probe()
        sys.exit(1)
    main(sys.argv[1])

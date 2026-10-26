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
DAY_PAGE = "https://rate.bot.com.tw/xrt/quote/day/{}"
DOC = "https://dadanny1122.github.io/video-helper/fx/"     # 匯率頻道說明書   # 台銀官方：這種貨幣今天每一次掛牌的時間和價格


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


def embed_for(rates, prev, now_tw, qtime=None, hists=None, small=None):
    hists = hists or {}
    small = small or {}
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
                     f"即期 買 {fmt(r['spot_buy'])}／賣 {fmt(r['spot_sell'])}" + extra
                     )
    return {
        "title": f"💱 外幣匯率｜{now_tw[5:]}（1 外幣 = 多少台幣）",
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
                       f"📖 [每個數字怎麼看（說明書）]({DOC})\n"
                       f"🕒 最後更新：{now_tw}（平日 9:00～17:00 每小時整點；分析在 #匯率分析）",
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


# ---- 預報（forecast）：不猜「會漲還是跌」，只說「可能的範圍」和「歷史上相似的情況後來怎樣」----
FC_CODES = ["USD", "EUR", "JPY", "CNY"]        # 預報只做這幾種（總結才不會太長）
FC_DAYS = 5               # 預報未來幾個營業日
W = 10                    # 「相似走勢」看最近幾個營業日的形狀
K = 20                    # 找最像的幾段
LONG_YEARS = 3            # 從過去幾年裡找
# FRED（美國聯準會公開資料）：(代號, 是不是「1 外幣 = 多少美金」的寫法)
FRED = {"TWD": ("DEXTAUS", False), "JPY": ("DEXJPUS", False), "CNY": ("DEXCHUS", False), "EUR": ("DEXUSEU", True),
        "GBP": ("DEXUSUK", True), "HKD": ("DEXHKUS", False), "CHF": ("DEXSZUS", False), "AUD": ("DEXUSAL", True),
        "KRW": ("DEXKOUS", False), "USD": (None, False)}
FRED_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv?id={}"


def fred(sid):
    req = urllib.request.Request(FRED_URL.format(sid), headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        rows = list(csv.reader(io.StringIO(r.read().decode("utf-8", "ignore"))))
    cut = (datetime.now(TW) - timedelta(days=365 * LONG_YEARS + 30)).strftime("%Y-%m-%d")
    out = {}
    for row in rows[1:]:
        try:
            if row[0] >= cut and float(row[1]) > 0:
                out[row[0]] = float(row[1])
        except (ValueError, IndexError):
            continue
    return out


def long_series(code, twd):
    """過去 3 年每天「1 外幣 = 多少台幣」（用 FRED 的美金匯率換算）"""
    sid, inverted = FRED[code]
    if sid is None:
        return [twd[d] for d in sorted(twd)]
    x = fred(sid)
    out = []
    for d in sorted(set(x) & set(twd)):
        out.append(twd[d] * x[d] if inverted else twd[d] / x[d])
    return out


def stats(prices):
    """一串每日價格 → 每日報酬的標準差（log）"""
    import math
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a and b]
    if len(rets) < 10:
        return None
    mu = sum(rets) / len(rets)
    return (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5


def vol_range(prices, m):
    """用最近 30 天每天晃動的幅度，推算未來 5 個營業日約 68% 機會落在的範圍"""
    import math
    rets = [math.log(b / a) for a, b in zip(prices, prices[1:]) if a and b]
    if len(rets) < 10:
        return None
    mu = sum(rets) / len(rets)
    sd = (sum((r - mu) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * FC_DAYS ** 0.5
    return m * math.exp(-sd), m * math.exp(sd)


def analog(long, recent_prices):
    """找過去 3 年裡，跟最近 10 天形狀最像的 20 段，看它們之後 5 天怎麼走"""
    if len(recent_prices) < W + 1 or len(long) < 200:
        return None
    tgt = [p / recent_prices[-1] - 1 for p in recent_prices[-(W + 1):]]
    cands = []
    for e in range(W, len(long) - FC_DAYS):
        win = long[e - W:e + 1]
        path = [p / win[-1] - 1 for p in win]
        cands.append((sum((a - b) ** 2 for a, b in zip(path, tgt)), e))
    cands.sort()
    picked = []
    for dist, e in cands:                       # 挑出來的片段不要互相重疊
        if all(abs(e - p) > FC_DAYS for p in picked):
            picked.append(e)
        if len(picked) >= K:
            break
    outs = sorted(long[e + FC_DAYS] / long[e] - 1 for e in picked)
    up = sum(1 for o in outs if o > 0)
    n = len(outs)
    q = lambda f: outs[min(int(f * (n - 1) + 0.5), n - 1)] * 100
    return {"n": n, "up": up, "down": n - up, "median": q(0.5), "mean": sum(outs) / n * 100,
            "p25": q(0.25), "p75": q(0.75)}


def forecasts(rates, hists):
    try:
        twd = fred("DEXTAUS")
    except Exception as e:
        print(f"::warning title=預報::讀不到 FRED：{e}")
        twd = None
    out = {}
    for code in FC_CODES:
        m, h = mid(rates.get(code, {})) if rates.get(code) else None, hists.get(code) or []
        if not m or len(h) < 10:
            continue
        prices = [x[1] for x in h] + [m]
        f = {"m": m, "range": vol_range(prices, m)}
        sd = stats(prices)
        if sd:
            import math
            k = sd * FC_DAYS ** 0.5
            f["r95"] = (m * math.exp(-1.96 * k), m * math.exp(1.96 * k))
        if twd:
            try:
                f["analog"] = analog(long_series(code, twd), prices)
            except Exception as e:
                print(f"::warning title=預報::{code} 相似走勢失敗：{e}")
        out[code] = f
    return out


def score(state, hists, today):
    """對答案：5 個營業日前的預報，實際結果有沒有落在範圍裡、方向有沒有對"""
    for f in state.get("fc", []):
        if f.get("done"):
            continue
        after = [x for x in (hists.get(f["c"]) or []) if x[0] > f["d"].replace("-", "")]
        if len(after) < FC_DAYS:
            continue
        actual = after[FC_DAYS - 1][1]
        f["done"] = True
        f["in"] = f["lo"] <= actual <= f["hi"] if f.get("lo") else None
        if f.get("up") is not None and f["up"] != 0.5:
            f["dir"] = (f["up"] > 0.5) == (actual > f["m"])
    done = [f for f in state.get("fc", []) if f.get("done")][-120:]
    rin = [f["in"] for f in done if f.get("in") is not None]
    rdir = [f["dir"] for f in done if f.get("dir") is not None]
    if not rin:
        return "📏 準不準：累積中（5 個營業日後開始對答案，結果會公開在這裡）"
    return (f"📏 準不準（最近 {len(rin)} 次已對答案）：範圍命中 **{sum(rin)}/{len(rin)}**（理論上約 68%）"
            + (f"｜相似走勢方向 **{sum(rdir)}/{len(rdir)}**（丟硬幣約 50%）" if rdir else ""))


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


def post_summary(base, rates, prev, hists, today, fcs=None, accuracy=""):
    """每日收盤總結：今天漲最多、跌最多、30 天最便宜／最貴的，附走勢圖"""
    def pad(x, w):
        return str(x).rjust(w)
    # ── 表 1：行情（全部貨幣）──
    rows = [f"{'CCY':<4}{pad('Mid', 9)}{pad('1D%', 7)}{pad('30D Lo', 9)}{pad('30D Hi', 9)}{pad('Pctl', 6)}{pad('Vol%', 6)}"]
    moves, pos = [], []
    for code in WATCH:
        r = rates.get(code)
        m = mid(r) if r else None
        if not m:
            continue
        h = hists.get(code) or []
        p = prev.get(code)
        d1 = (m - p) / p * 100 if p else None
        if d1 is not None:
            moves.append((d1, code))
        vals = [x[1] for x in h] + [m]
        pct = position(m, h)[1] * 100 if h else None
        if pct is not None:
            pos.append((pct, code))
        sd = stats(vals)
        vol = sd * 252 ** 0.5 * 100 if sd else None
        rows.append(f"{code:<4}{pad(fmt(m), 9)}{pad('—' if d1 is None else f'{d1:+.2f}', 7)}"
                    f"{pad(fmt(min(vals)), 9)}{pad(fmt(max(vals)), 9)}"
                    f"{pad('—' if pct is None else f'{pct:.0f}', 6)}{pad('—' if vol is None else f'{vol:.1f}', 6)}")
    lines = [f"**📋 行情**（1 外幣 = 台幣；台銀即期中間價）", "```", *rows, "```",
             f"Mid＝即期買賣中間價｜1D%＝跟前一營業日比｜30D Lo/Hi＝近 {DAYS} 天最低／最高｜"
             f"Pctl＝現價在近 {DAYS} 天的百分位（0＝最低、100＝最高）｜Vol%＝近 {DAYS} 天年化波動率"]
    # ── 表 2：預報 ──
    if fcs:
        fr = [f"{'CCY':<4}{pad('68% range', 17)}{pad('95% range', 17)}"]
        ar = [f"{'CCY':<4}{pad('Up/N', 7)}{pad('Mean%', 7)}{pad('Med%', 7)}{pad('P25%', 7)}{pad('P75%', 7)}"]
        for code, f in fcs.items():
            r68, r95 = f.get("range"), f.get("r95")
            fr.append(f"{code:<4}{pad(f'{fmt(r68[0])}-{fmt(r68[1])}' if r68 else '—', 17)}"
                      f"{pad(f'{fmt(r95[0])}-{fmt(r95[1])}' if r95 else '—', 17)}")
            a = f.get("analog")
            if a:
                ar.append(f"{code:<4}{pad(f'{a['up']}/{a['n']}', 7)}{pad(f'{a['mean']:+.2f}', 7)}"
                          f"{pad(f'{a['median']:+.2f}', 7)}{pad(f'{a['p25']:+.2f}', 7)}{pad(f'{a['p75']:+.2f}', 7)}")
        lines += ["", f"**📐 {FC_DAYS} 個營業日價格區間**（依近 {DAYS} 天波動率）", "```", *fr, "```"]
        if len(ar) > 1:
            lines += [f"**🔁 歷史相似走勢**（近 {W} 日形狀 vs 過去 {LONG_YEARS} 年，取最像 {K} 段，之後 {FC_DAYS} 日報酬）",
                      "```", *ar, "```",
                      "Up/N＝之後上漲的段數／總段數｜Mean/Med＝平均／中位數報酬｜P25/P75＝四分位（一半的結果落在這之間）"]
        lines.append(accuracy)
    # ── 解讀（另外放，跟數據分開）──
    notes = []
    if len(moves) >= 2:
        moves.sort()
        notes.append(f"漲最多 {moves[-1][1]} {moves[-1][0]:+.2f}%｜跌最多 {moves[0][1]} {moves[0][0]:+.2f}%")
    hi_ = [c for p_, c in sorted(pos, reverse=True) if p_ >= 80]
    lo_ = [c for p_, c in sorted(pos) if p_ <= 20]
    if hi_:
        notes.append(f"處於近 {DAYS} 天高檔（Pctl≥80）：" + "、".join(hi_))
    if lo_:
        notes.append(f"處於近 {DAYS} 天低檔（Pctl≤20）：" + "、".join(lo_))
    if notes:
        lines += ["", "**💬 簡短解讀**", *notes]
    lines.append(f"\n📎 資料：[臺灣銀行牌告匯率]({PAGE})、[FRED](https://fred.stlouisfed.org/categories/94)｜數據整理，非買賣建議｜[📖 說明書]({DOC})")
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


def wait_for_hour():
    """排程在每小時 :50 啟動 → 等到下一個整點才發（GitHub 排程常遲到，提早啟動才準時）"""
    now = datetime.now(TW)
    target = (now + timedelta(minutes=15)).replace(minute=0, second=0, microsecond=0)
    if now < target:
        print(f"等到 {target.strftime('%H:%M')}（還有 {int((target - now).total_seconds())} 秒）")
        time.sleep((target - now).total_seconds())


def small_direction(rates, hists, state, now):
    """小方向：今天盤中的變化（每小時一筆）→ (表格列, 提醒)"""
    intra = state.setdefault("intra", {})
    hm = now.strftime("%H:%M")
    rows, alerts = [], []
    for code in WATCH:
        r = rates.get(code)
        m = mid(r) if r else None
        if not m:
            continue
        seq = intra.setdefault(code, [])
        if not seq or seq[-1][0] != hm:
            seq.append([hm, m])
        o = seq[0][1]
        day_pct = (m - o) / o * 100
        before = [x for x in seq[:-1] if x[0] <= (now - timedelta(minutes=50)).strftime("%H:%M")]
        h1 = (m - before[-1][1]) / before[-1][1] * 100 if before else None
        lo, hi = min(x[1] for x in seq), max(x[1] for x in seq)
        sd = stats([x[1] for x in hists.get(code) or []])
        band = sd * 100 if sd else None             # 平常「一天」大約晃多少 %
        fc = next((f for f in reversed(state.get("fc", [])) if f["c"] == code and f.get("lo")), None)
        where = "—" if not fc else "in" if fc["lo"] <= m <= fc["hi"] else "▲out" if m > fc["hi"] else "▼out"
        rows.append(f"{code:<4}{fmt(m):>9}{day_pct:>+7.2f}{'—' if h1 is None else f'{h1:+.2f}':>7}"
                    f"{fmt(lo):>9}{fmt(hi):>9}{'—' if band is None else f'{band:.2f}':>6}{where:>6}")
        zh, flag = WATCH[code]
        if band and abs(day_pct) >= band and f"{code}:day" not in state["alerted"]:
            alerts.append(f"{flag} **{zh}** 盤中 {day_pct:+.2f}%（{fmt(o)} → {fmt(m)}），已超過平常一天的幅度 ±{band:.2f}%")
            state["alerted"].append(f"{code}:day")
        if fc and where != "in" and f"{code}:fc" not in state["alerted"]:
            alerts.append(f"{flag} **{zh}** {fmt(m)} 跑出 {fc['d'][5:]} 預報的 5 日 68% 區間{'上緣' if m > fc['hi'] else '下緣'}"
                          f"（{fmt(fc['lo'])}～{fmt(fc['hi'])}）")
            state["alerted"].append(f"{code}:fc")
    return rows, alerts


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    scheduled = os.environ.get("EVENT") == "schedule" or os.environ.get("WAIT") == "true"
    if scheduled:
        wait_for_hour()
    rates = fetch()
    if len(rates) < 3:
        print(f"::warning title=匯率::讀不到臺灣銀行的匯率（只讀到 {len(rates)} 種）")
        return
    now = datetime.now(TW)
    today, now_tw, slot = now.strftime("%Y-%m-%d"), now.strftime("%Y-%m-%d %H:%M"), now.strftime("%Y-%m-%d %H")
    if scheduled and state.get("slot") == slot:
        print(f"::notice title=匯率::{slot} 點已經發過，跳過")
        return
    # 「前一個營業日」的收盤：每天第一次跑時，把昨天最後一次的價格存成比較基準
    if state.get("day") != today:
        state["prev"] = state.get("last", {}) or state.get("prev", {})
        state["day"] = today
        state["alerted"] = []
        state["intra"] = {}                      # 今天盤中每小時的價格（小方向用）
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
    rows, small_alerts = small_direction(rates, hists, state, now)
    # 大漲大跌：跟前一個營業日比超過 ALERT_PCT
    for code, r in rates.items():
        m, p = mid(r), prev.get(code)
        if m and p and abs((m - p) / p * 100) >= ALERT_PCT and code not in state["alerted"]:
            zh, flag = WATCH[code]
            pct = (m - p) / p * 100
            small_alerts.append(f"{flag} **{zh}** 跟前一營業日比 {'升' if pct > 0 else '貶'} {abs(pct):.2f}%（{fmt(p)} → {fmt(m)}）")
            state["alerted"].append(code)
    # ---- #匯率：價格表（每小時發一則新的）----
    qtime = quote_time()
    e = embed_for(rates, prev, now_tw, qtime, hists)
    if len(e["description"]) > 4000:             # Discord 一則最多 4096 字：太長就拿掉走勢連結
        e["description"] = e["description"].replace("｜[走勢](https://www.tradingview.com/symbols/", "｜[").replace("TWD/)", ")")
    # ---- #匯率分析：小方向表格＋提醒（每小時）----
    hdr = f"{'CCY':<4}{'Now':>9}{'Day%':>7}{'1H%':>7}{'Lo':>9}{'Hi':>9}{'1σ%':>6}{'5D':>6}"
    desc = []
    if small_alerts:
        desc += ["**⚡ 提醒**", *small_alerts, ""]
    desc += ["**⏱ 小方向（今天盤中）**", "```", hdr, *rows, "```",
             "Now＝即期中間價｜Day%＝今天第一筆到現在｜1H%＝跟上一小時比｜Lo/Hi＝盤中最低／最高｜"
             "1σ%＝平常一天晃多少｜5D＝在不在最近一次 5 日 68% 預報區間（in／▲out 超出上緣／▼out 跌破下緣）",
             f"\n數據整理，非買賣建議｜[📖 說明書]({DOC})"]
    ae = {"title": f"⏱ 小方向｜{now.strftime('%m/%d %H:%M')}", "url": DOC,
          "color": 0xF9A825 if small_alerts else 0x546E7A, "description": "\n".join(desc)[:4000]}
    print(f"::notice title=匯率::" + "｜".join(f"{c}={fmt(mid(r))}" for c, r in rates.items()))
    hook = os.environ.get("DISCORD_FX_WEBHOOK", "").strip()
    ahook = os.environ.get("DISCORD_FX_ANALYSIS_WEBHOOK", "").strip() or hook   # 分析頻道還沒設定 → 先發在匯率頻道
    if os.environ.get("PREVIEW") == "true" or not hook:
        print("::notice title=預覽 價格::" + json.dumps(e, ensure_ascii=False)[:2500])
        print("::notice title=預覽 分析::" + json.dumps(ae, ensure_ascii=False)[:2500])
        return
    base, abase = hook.split("?")[0], ahook.split("?")[0]
    call("POST", base, {"username": "Video Helper 匯率", "embeds": [e]})
    call("POST", abase, {"username": "Video Helper 匯率分析", "embeds": [ae]})
    # ---- #匯率分析：大方向（每天 17:00 那次，或手動勾 summary）----
    if (now.hour >= 17 and state.get("summary_day") != today) or os.environ.get("SUMMARY") == "true":
        accuracy = score(state, hists, today)
        fcs = forecasts(rates, hists)
        post_summary(abase, rates, prev, hists, today, fcs, accuracy)
        if state.get("fc_day") != today:          # 一天只記一次預報，之後拿來對答案
            for code, f in fcs.items():
                a = f.get("analog")
                state.setdefault("fc", []).append({
                    "d": today, "c": code, "m": f["m"],
                    "lo": f["range"][0] if f.get("range") else None, "hi": f["range"][1] if f.get("range") else None,
                    "up": a["up"] / a["n"] if a and a["n"] else None})
            state["fc"] = state["fc"][-300:]
            state["fc_day"] = today
        state["summary_day"] = today
    state["last"] = {c: mid(r) for c, r in rates.items() if mid(r)}
    if scheduled:
        state["slot"] = slot
    state.pop("message_id", None)
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"::notice title=匯率::已發出；提醒 {len(small_alerts)} 則")


def probe():
    for u in ("https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXTAUS",
              "https://fred.stlouisfed.org/graph/fredgraph.csv?id=DEXJPUS"):
        try:
            with urllib.request.urlopen(urllib.request.Request(u, headers={"User-Agent": UA}), timeout=30) as r:
                t = r.read().decode("utf-8-sig", "ignore").splitlines()
            print(f"::notice title=歷史 {u[-11:]}::{len(t)} 行｜" + " ⏎ ".join(t[:3]) + " ⏎ … ⏎ " + t[-1])
        except Exception as e:
            print(f"::notice title=歷史 {u[-11:]}::{e}")


if __name__ == "__main__":
    if os.environ.get("PROBE") == "true":
        probe()
        sys.exit(1)
    main(sys.argv[1])

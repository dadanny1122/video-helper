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


def mid(r):
    """用「即期」買賣的中間價當作代表（沒有即期就用現金）"""
    a, b = r.get("spot_buy") or r.get("cash_buy"), r.get("spot_sell") or r.get("cash_sell")
    return (a + b) / 2 if a and b else None


def fmt(v):
    return "—" if v is None else (f"{v:.4f}" if v < 1 else f"{v:.3f}" if v < 10 else f"{v:.2f}")


def embed_for(rates, prev, now_tw):
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
        lines.append(f"{flag} **{zh} {code}**{chg}　[走勢圖]({tv})\n"
                     f"　現金 買 {fmt(r['cash_buy'])}／賣 {fmt(r['cash_sell'])}　"
                     f"即期 買 {fmt(r['spot_buy'])}／賣 {fmt(r['spot_sell'])}")
    return {
        "title": "💱 外幣匯率（1 外幣 = 多少台幣）",
        "url": PAGE,
        "color": 0x2E7D32,
        "description": "\n".join(lines) + "\n\n"
                       "「買」＝銀行跟你買（你賣外幣拿到的台幣）；「賣」＝銀行賣你（你買外幣要付的台幣）\n"
                       "🔺🔻＝跟前一個營業日比（用即期中間價）\n"
                       f"📎 來源：[臺灣銀行牌告匯率]({PAGE})；走勢圖：TradingView\n"
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
    e = embed_for(rates, prev, now_tw)
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
    state["last"] = {c: mid(r) for c, r in rates.items() if mid(r)}
    json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"::notice title=匯率::已更新；提醒 {len(alerts)} 則")


if __name__ == "__main__":
    main(sys.argv[1])

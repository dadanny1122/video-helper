"""
生活資訊 → Discord：⛽ 中油油價、🧾 統一發票中獎號碼
  油價：中油官方牌價（公開資料，不用鑰匙）。生效日期變了（每週一調價）就通知，跟上週比漲跌多少
  發票：財政部 RSS（公開資料）。單數月 25 日開獎，有新的一期就通知
狀態存 .github/life_state.json（也給 Discord /對獎 指令讀）
環境變數：DISCORD_LIFE_WEBHOOK（沒設定就用 DISCORD_WEATHER_WEBHOOK）、PREVIEW=true 只印不發
用法：python life_feed.py 狀態檔.json
"""
import json
import os
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

TW = timezone(timedelta(hours=8))
UA = {"User-Agent": "VideoHelper-LifeBot/1.0"}
OIL_URL = "https://vipmbr.cpc.com.tw/cpcstn/listpricewebservice.asmx/getCPCMainProdListPrice_XML"
OIL_PAGE = "https://www.cpc.com.tw/historyprice.aspx?n=2890"
OIL_ITEMS = ("92無鉛汽油", "95無鉛汽油", "98無鉛汽油", "超級柴油")
INVOICE_URL = "https://invoice.etax.nat.gov.tw/invoice.xml"
PRIZES = "特別獎 1,000 萬｜特獎 200 萬｜頭獎 20 萬｜末 7 碼 4 萬｜末 6 碼 1 萬｜末 5 碼 4 千｜末 4 碼 1 千｜末 3 碼 200"


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "ignore")


def call(hook, payload):
    req = urllib.request.Request(hook, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", **UA})
    urllib.request.urlopen(req, timeout=30).read()


def roc_date(s):
    """1151109 → 2026-11-09"""
    s = (s or "").strip()
    return f"{int(s[:-4]) + 1911}-{s[-4:-2]}-{s[-2:]}" if len(s) >= 6 else s


# ---------- ⛽ 油價 ----------
def oil():
    root = ET.fromstring(get(OIL_URL))
    out, date = {}, ""
    for t in root.iter("Table"):
        f = {c.tag: (c.text or "").strip() for c in t}
        name = f.get("產品名稱", "")
        if name in OIL_ITEMS and "零售" in f.get("型別名稱", "") and name not in out:
            out[name] = float(f.get("參考牌價_金額") or 0)
            date = max(date, roc_date(f.get("牌價生效日期")))
    if len(out) < 3:
        raise RuntimeError(f"油價格式變了：只讀到 {out}")
    return date, out


def oil_embed(date, prices, prev):
    rows = []
    for name in OIL_ITEMS:
        if name not in prices:
            continue
        p, o = prices[name], (prev or {}).get(name)
        d = "" if o is None else ("　持平" if abs(p - o) < 0.005 else f"　{'🔺' if p > o else '🔻'} {p - o:+.1f}")
        rows.append(f"{name:<6} {p:5.1f} 元{d}")
    chg = [prices[n] - prev[n] for n in OIL_ITEMS if prev and n in prev and n in prices]
    head = ("汽油" + ("漲" if chg[0] > 0 else "跌" if chg[0] < 0 else "不調整") +
            (f" {abs(chg[0]):.1f} 元" if chg and chg[0] else "")) if chg else "目前牌價"
    return {"title": f"⛽ 中油油價｜{date} 起｜{head}", "url": OIL_PAGE,
            "color": 0xC62828 if chg and chg[0] > 0 else 0x2E7D32 if chg and chg[0] < 0 else 0x546E7A,
            "description": "```\n" + "\n".join(rows) + "\n```（每公升，中油直營站；台塑通常跟進）\n資料：台灣中油 牌價"}


# ---------- 🧾 發票 ----------
def invoices():
    root = ET.fromstring(get(INVOICE_URL))
    out = []
    for it in root.iter("item"):
        title = (it.findtext("title") or "").strip()
        desc = re.sub(r"<[^>]+>", "\n", it.findtext("description") or "")
        nums = lambda label: re.findall(r"\d{3,8}", (re.search(label + r"[：:]\s*([\d、,，\s]+)", desc) or [None, ""])[1])
        out.append({"period": title, "link": (it.findtext("link") or "").strip(),
                    "special": (nums("特別獎") or [""])[0], "grand": (nums("特獎") or [""])[0],
                    "first": nums("頭獎"), "extra": nums("增開六獎")})
    if not out or not out[0]["first"]:
        raise RuntimeError("發票號碼格式變了")
    return out


def invoice_embed(v):
    lines = [f"🏆 特別獎　**{v['special']}**", f"🥇 特獎　　**{v['grand']}**", "🎫 頭獎　　" + "、".join(f"**{x}**" for x in v["first"])]
    if v["extra"]:
        lines.append("➕ 增開六獎 " + "、".join(v["extra"]))
    return {"title": f"🧾 統一發票 {v['period']} 中獎號碼", "url": v["link"], "color": 0xF9A825,
            "description": "\n".join(lines) + f"\n\n{PRIZES}\n對獎：Discord 打 `/對獎 號碼`，或用財政部「統一發票兌獎」App 掃描"
                           "\n資料：財政部稅務入口網"}


def check_number(num, periods):
    """/對獎 用的規則（Worker 也照這個寫）：回傳 [(期別, 獎項)]"""
    out = []
    for v in periods[:2]:                          # 能領的只有最近兩期
        if num == v["special"]:
            out.append((v["period"], "特別獎 1,000 萬"))
        elif num == v["grand"]:
            out.append((v["period"], "特獎 200 萬"))
        else:
            best = 0
            for f in v["first"]:
                k = 0
                while k < 8 and num[-1 - k] == f[-1 - k]:
                    k += 1
                best = max(best, k)
            name = {8: "頭獎 20 萬", 7: "二獎 4 萬", 6: "三獎 1 萬", 5: "四獎 4 千", 4: "五獎 1 千", 3: "六獎 200 元"}
            if best >= 3:
                out.append((v["period"], name[best]))
            elif num[-3:] in v["extra"]:
                out.append((v["period"], "增開六獎 200 元"))
    return out


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    hook = (os.environ.get("DISCORD_LIFE_WEBHOOK") or os.environ.get("DISCORD_WEATHER_WEBHOOK") or "").strip()
    preview = os.environ.get("PREVIEW") == "true" or not hook
    embeds = []
    try:
        date, prices = oil()
        old = state.get("oil") or {}
        print(f"::notice title=油價::{date} {prices}（上次 {old.get('date')}）")
        if date != old.get("date"):
            embeds.append(oil_embed(date, prices, old.get("prices")))
            state["oil"] = {"date": date, "prices": prices, "prev": old.get("prices")}
    except Exception as e:
        print(f"::warning title=油價::{e}")
    try:
        inv = invoices()
        print(f"::notice title=發票::{inv[0]['period']} {inv[0]['special']} {inv[0]['grand']} {inv[0]['first']}")
        if inv[0]["period"] != (state.get("invoice") or [{}])[0].get("period"):
            embeds.append(invoice_embed(inv[0]))
        state["invoice"] = inv[:3]
    except Exception as e:
        print(f"::warning title=發票::{e}")
    if os.environ.get("TEST_NUMBER") and state.get("invoice"):
        print("::notice title=對獎測試::" + str(check_number(os.environ["TEST_NUMBER"], state["invoice"])))
    for e in embeds:
        if preview:
            print("::notice title=預覽::" + json.dumps(e, ensure_ascii=False)[:2000])
        else:
            call(hook, {"username": "Video Helper 生活", "embeds": [e]})
    state["updated"] = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
    if not preview:
        json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"::notice title=生活::發出 {len(embeds)} 則")


if __name__ == "__main__":
    main(sys.argv[1])

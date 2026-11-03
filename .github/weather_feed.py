"""
中央氣象署「一般天氣預報－今明 36 小時天氣預報」（F-C0032-001）→ Discord #每日氣象
  MODE=daily：全台 22 縣市今明天氣總覽（每天早上 7:00）
  MODE=check：任何縣市接下來的時段降雨機率 ≥ 70% → ☔ 提醒（每 3 小時；同縣市同時段只提醒一次）

用法：python weather_feed.py 狀態檔.json
環境變數：CWA_API_KEY（氣象署授權碼）、DISCORD_WEATHER_WEBHOOK、MODE、PREVIEW=true（只印不發）
"""
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-C0032-001"
PAGE = "https://www.cwa.gov.tw/V8/C/W/County/index.html"
TW = timezone(timedelta(hours=8))
RAIN_ALERT = 70            # 降雨機率 ≥ 70% 就提醒
REGIONS = [               # 依地區分組（名字跟氣象署一樣，「臺」不是「台」）
    ("🏙 北部", ["基隆市", "臺北市", "新北市", "桃園市", "新竹市", "新竹縣", "宜蘭縣"]),
    ("🌾 中部", ["苗栗縣", "臺中市", "彰化縣", "南投縣", "雲林縣"]),
    ("🌴 南部", ["嘉義市", "嘉義縣", "臺南市", "高雄市", "屏東縣"]),
    ("⛰ 東部", ["花蓮縣", "臺東縣"]),
    ("🏝 離島", ["澎湖縣", "金門縣", "連江縣"]),
]


def wx_icon(wx):
    if "雷" in wx:
        return "⛈"
    if "雨" in wx:
        return "🌧"
    if "陰" in wx:
        return "☁"
    if "多雲" in wx:
        return "⛅" if "晴" in wx else "🌥"
    if "晴" in wx:
        return "☀"
    return "🌤"


def fetch():
    key = os.environ.get("CWA_API_KEY", "").strip()
    if not key:
        raise RuntimeError("沒有設定 CWA_API_KEY（氣象署授權碼）")
    url = API + "?" + urllib.parse.urlencode({"Authorization": key, "format": "JSON"})
    req = urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0", "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def parse(data):
    """→ {縣市: [{start, end, wx, pop, minT, maxT, ci}, … 3 個時段]}"""
    recs = data.get("records") or data.get("Records") or {}
    locs = recs.get("location") or recs.get("Location") or []
    out = {}
    for loc in locs:
        name = loc.get("locationName") or loc.get("LocationName")
        periods = {}
        for el in loc.get("weatherElement") or loc.get("WeatherElement") or []:
            en = el.get("elementName") or el.get("ElementName")
            for t in el.get("time") or el.get("Time") or []:
                k = (t.get("startTime") or t.get("StartTime"), t.get("endTime") or t.get("EndTime"))
                p = t.get("parameter") or t.get("Parameter") or {}
                periods.setdefault(k, {})[en] = p.get("parameterName") or p.get("ParameterName")
        rows = []
        for (st, et), v in sorted(periods.items()):
            def num(x):
                try:
                    return int(float(x))
                except (TypeError, ValueError):
                    return None
            rows.append({"start": st, "end": et, "wx": v.get("Wx") or "", "pop": num(v.get("PoP")),
                         "minT": num(v.get("MinT")), "maxT": num(v.get("MaxT")), "ci": v.get("CI") or ""})
        if name and rows:
            out[name] = rows
    return out


def when(st, et):
    """'2026-11-03 18:00:00' → '今晚 18–06'、'明天 06–18'…"""
    s = datetime.strptime(st[:16], "%Y-%m-%d %H:%M")
    e = datetime.strptime(et[:16], "%Y-%m-%d %H:%M")
    today = datetime.now(TW).date()
    day = "今天" if s.date() == today else "明天" if s.date() == today + timedelta(days=1) else s.strftime("%m/%d")
    if s.hour >= 18:
        day = "今晚" if s.date() == today else "明晚" if s.date() == today + timedelta(days=1) else day + "晚上"
    return f"{day} {s.hour:02d}–{e.hour:02d}"


def call(hook, payload):
    data = json.dumps(payload).encode("utf-8")
    for _ in range(5):
        req = urllib.request.Request(hook, data=data, method="POST",
                                     headers={"Content-Type": "application/json", "User-Agent": "VideoHelper-CI"})
        try:
            urllib.request.urlopen(req, timeout=20).read()
            return
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(min(float(json.loads(e.read() or b"{}").get("retry_after", 2)), 30) + 0.5)
                continue
            raise


def daily_embed(fc):
    first = next(iter(fc.values()))
    head = " ／ ".join(when(p["start"], p["end"]) for p in first[:2])
    fields = []
    for title, names in REGIONS:
        lines = []
        for n in names:
            ps = fc.get(n)
            if not ps:
                continue
            a, b = ps[0], ps[1] if len(ps) > 1 else ps[0]
            lo = min(x for x in (a["minT"], b["minT"]) if x is not None)
            hi = max(x for x in (a["maxT"], b["maxT"]) if x is not None)
            rain = max((x["pop"] or 0) for x in ps[:2])
            umb = " ☔" if rain >= RAIN_ALERT else ""
            lines.append(f"{wx_icon(a['wx'])}{wx_icon(b['wx'])} **{n}** {lo}–{hi}°C　💧{a['pop']}%／{b['pop']}%{umb}")
        fields.append({"name": title, "value": "\n".join(lines) or "—", "inline": False})
    wet = [n for n, ps in fc.items() if max((x["pop"] or 0) for x in ps[:2]) >= RAIN_ALERT]
    return {"title": f"🌤 每日氣象｜{datetime.now(TW).strftime('%m/%d')}（全台 22 縣市）", "url": PAGE, "color": 0x0288D1,
            "description": f"兩個圖示＝{head}的天氣｜💧＝這兩個時段的降雨機率\n"
                           + (f"☔ **記得帶傘：**{'、'.join(wet)}（降雨機率 ≥ {RAIN_ALERT}%）" if wet else "☀ 今明兩天各地降雨機率都低於 70%"),
            "fields": fields,
            "footer": {"text": "資料：中央氣象署 今明 36 小時天氣預報（F-C0032-001）"}}


def check_alerts(fc, state):
    """接下來兩個時段，降雨機率 ≥ 70% 而且還沒提醒過的"""
    sent = set(state.get("alerted", []))
    hits = []
    for n, ps in fc.items():
        for p in ps[:2]:
            key = f"{n}|{p['start']}"
            if (p["pop"] or 0) >= RAIN_ALERT and key not in sent:
                hits.append((n, p))
                sent.add(key)
    state["alerted"] = sorted(sent)[-500:]
    return hits


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    fc = parse(fetch())
    if len(fc) < 10:
        print(f"::warning title=氣象::只讀到 {len(fc)} 個縣市，資料格式可能改了")
        return
    mode = os.environ.get("MODE", "check")
    hook = os.environ.get("DISCORD_WEATHER_WEBHOOK", "").strip()
    preview = os.environ.get("PREVIEW") == "true" or not hook
    print("::notice title=氣象::" + "｜".join(f"{n} {ps[0]['wx']} {ps[0]['pop']}%" for n, ps in list(fc.items())[:8]))
    if mode == "daily":
        e = daily_embed(fc)
        if preview:
            print("::notice title=預覽 每日::" + json.dumps(e, ensure_ascii=False)[:3000])
        else:
            call(hook, {"username": "Video Helper 氣象", "embeds": [e]})
    hits = check_alerts(fc, state)
    if hits:
        by = {}
        for n, p in hits:
            by.setdefault(when(p["start"], p["end"]), []).append(f"{wx_icon(p['wx'])} **{n}** 💧{p['pop']}%　{p['wx']}")
        desc = "\n\n".join(f"**🕒 {k}**\n" + "\n".join(v) for k, v in by.items())
        e = {"title": f"☔ 降雨提醒：{len(hits)} 個縣市降雨機率 ≥ {RAIN_ALERT}%", "url": PAGE, "color": 0x1565C0,
             "description": desc[:3900] + "\n\n記得帶傘 ☂｜資料：中央氣象署"}
        if preview:
            print("::notice title=預覽 提醒::" + json.dumps(e, ensure_ascii=False)[:2500])
        else:
            call(hook, {"username": "Video Helper 氣象", "embeds": [e]})
    if not preview:
        state["last"] = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
        json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"::notice title=氣象::{mode} 完成；提醒 {len(hits)} 個縣市時段")


def probe():
    """診斷：看鄉鎮區 2 天預報（F-D0047-005 桃園市，大園區）的資料長怎樣"""
    key = os.environ.get("CWA_API_KEY", "").strip()
    url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/F-D0047-005?" + urllib.parse.urlencode(
        {"Authorization": key, "format": "JSON", "LocationName": "大園區"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0"}), timeout=30) as r:
        d = json.loads(r.read())
    txt = json.dumps(d, ensure_ascii=False)
    print(f"::notice title=結構 1::{len(txt)} 字｜{txt[:1500]}")
    recs = d.get("records", {})
    locs = recs.get("Locations") or recs.get("locations") or []
    loc = (locs[0].get("Location") or locs[0].get("location"))[0] if locs else {}
    for el in loc.get("WeatherElement") or loc.get("weatherElement") or []:
        name = el.get("ElementName") or el.get("elementName")
        t = (el.get("Time") or el.get("time") or [{}])
        print(f"::notice title=元素 {name}::{len(t)} 筆｜{json.dumps(t[:2], ensure_ascii=False)[:400]}")


if __name__ == "__main__":
    if os.environ.get("MODE") == "probe":
        probe()
        sys.exit(1)
    main(sys.argv[1])

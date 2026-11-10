"""
中央氣象署「一般天氣預報－今明 36 小時天氣預報」（F-C0032-001）→ Discord #每日氣象
  MODE=daily：🗓 大方向（每天早上 7:00）＝ 盯的地點「今天每 3 小時降雨機率時間表」＋ 全台 22 縣市今明總覽
  MODE=check：⚡ 小方向（每小時）＝ 盯的地點「接下來 3 小時」降雨機率從 < 70% 變成 ≥ 70% → 通知；
              下過雨之後降到 ≤ 30% → 通知雨停（只在狀態改變時通知，不重複吵）
  ⛈ 特報（兩種 MODE 都會看）：你的地點所在縣市，有新的大雨／豪雨／颱風特報 → 通知；解除也通知
  鄉鎮區資料：氣象署 F-D0047-xxx（各縣市鄉鎮 2 天預報，3 小時一筆）；特報：W-C0033-001

用法：python weather_feed.py 狀態檔.json
環境變數：CWA_API_KEY（氣象署授權碼）、DISCORD_WEATHER_WEBHOOK、MODE、PREVIEW=true（只印不發）
"""
import json
import os
import re
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


# 要盯的地點（使用者指定）：顯示名稱、氣象署資料集（各縣市 2 天預報）、鄉鎮區
SPOTS = [
    ("大園", "F-D0047-005", "大園區"),
    ("青埔（中壢）", "F-D0047-005", "中壢區"),
    ("土城", "F-D0047-069", "土城區"),
    ("台北101（信義）", "F-D0047-061", "信義區"),
    ("竹科（新竹東區）", "F-D0047-053", "東區"),
    ("竹北", "F-D0047-009", "竹北市"),
]
RAIN_STOP = 30            # 下過雨之後降到 ≤ 30% 就通知「雨停」
DS_COUNTY = {"F-D0047-005": "桃園市", "F-D0047-069": "新北市", "F-D0047-061": "臺北市",
             "F-D0047-053": "新竹市", "F-D0047-009": "新竹縣"}
NOTIFY_HAZ = ("雨", "颱風")   # 特報名稱有這些字才發通知（大雨、豪雨、大豪雨、超大豪雨、颱風）；其他特報只顯示在即時介面


def warnings():
    """天氣特報 → {縣市: [{"phen": 大雨, "sig": 特報, "start": …, "end": …}]}（只留你的地點所在縣市）"""
    key = os.environ.get("CWA_API_KEY", "").strip()
    url = API_BASE + "W-C0033-001?" + urllib.parse.urlencode({"Authorization": key, "format": "JSON"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0"}), timeout=30) as r:
        d = json.loads(r.read())
    watch = set(DS_COUNTY.values())
    out = {}
    for loc in (d.get("records", {}).get("location") or []):
        name = loc.get("locationName")
        if name not in watch:
            continue
        for h in (loc.get("hazardConditions") or {}).get("hazards") or []:
            info, vt = h.get("info") or {}, h.get("validTime") or {}
            out.setdefault(name, []).append({"phen": info.get("phenomena", ""), "sig": info.get("significance", ""),
                                             "start": vt.get("startTime", ""), "end": vt.get("endTime", "")})
    return out


def warning_alerts(wn, state):
    """新出現的雨／颱風特報 → 通知；之前有、現在沒有 → 解除"""
    old = set(state.get("warn", []))
    cur = {f"{c}|{h['phen']}{h['sig']}|{h['start'][:16]}|{h['end'][:16]}" for c, hs in wn.items() for h in hs
           if any(k in h["phen"] for k in NOTIFY_HAZ)}
    msgs = []
    for k in sorted(cur - old):
        c, name, st, et = k.split("|")
        msgs.append(f"⛈ **{c}　{name}**　{st[5:].replace('-', '/')} ～ {et[5:].replace('-', '/')}")
    for k in sorted(old - cur):
        c, name, _, _ = k.split("|")
        msgs.append(f"✅ {c}　{name} 已解除")
    state["warn"] = sorted(cur)
    return msgs
API_BASE = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/"


def spots_forecast():
    """→ {顯示名稱: {"blocks": [(開始, 結束, 降雨機率, 天氣)], "temp": {時間: 溫度}}}（時間都是台灣時間 datetime）"""
    key = os.environ.get("CWA_API_KEY", "").strip()
    out, by_ds = {}, {}
    for label, ds, town in SPOTS:
        by_ds.setdefault(ds, []).append((label, town))
    p = lambda t: datetime.fromisoformat(t).astimezone(TW)
    for ds, items in by_ds.items():
        url = API_BASE + ds + "?" + urllib.parse.urlencode(
            {"Authorization": key, "format": "JSON", "LocationName": ",".join(t for _, t in items)})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0"}), timeout=30) as r:
            d = json.loads(r.read())
        locs = (d.get("records", {}).get("Locations") or [{}])[0].get("Location") or []
        for loc in locs:
            town = loc.get("LocationName")
            label = next((l for l, t in items if t == town), None)
            if not label:
                continue
            els = {e.get("ElementName"): e.get("Time") or [] for e in loc.get("WeatherElement") or []}
            wx = {t["StartTime"]: (t["ElementValue"] or [{}])[0].get("Weather", "") for t in els.get("天氣現象", [])}
            blocks = []
            for t in els.get("3小時降雨機率", []):
                v = (t["ElementValue"] or [{}])[0].get("ProbabilityOfPrecipitation")
                try:
                    pop = int(v)
                except (TypeError, ValueError):
                    pop = None
                blocks.append((p(t["StartTime"]), p(t["EndTime"]), pop, wx.get(t["StartTime"], "")))
            temp = {}
            for t in els.get("溫度", []):
                try:
                    temp[p(t["DataTime"])] = int((t["ElementValue"] or [{}])[0].get("Temperature"))
                except (TypeError, ValueError, KeyError):
                    pass
            out[label] = {"blocks": sorted(blocks), "temp": temp}
    return out


def timetable_embed(sp):
    """🗓 大方向：今天每 3 小時的降雨機率，標出最可能下雨的時段"""
    now = datetime.now(TW)
    lines, peaks = [], []
    for label, _, _ in SPOTS:
        f = sp.get(label)
        if not f:
            lines.append(f"**{label}**：讀不到資料")
            continue
        today = [b for b in f["blocks"] if b[0].date() == now.date() and b[0].hour >= 6 and b[2] is not None]
        if not today:
            today = [b for b in f["blocks"] if b[2] is not None][:6]
        cells = "｜".join(f"{b[0].hour:02d} {b[2]}%{'☔' if b[2] >= RAIN_ALERT else ''}" for b in today)
        top = max(today, key=lambda b: b[2])
        ts = [v for k, v in f["temp"].items() if k.date() == now.date()]
        tr = f"{min(ts)}–{max(ts)}°C　" if ts else ""
        lines.append(f"**{label}**　{tr}最可能：{top[0].hour:02d}–{top[1].hour:02d} 點 **{top[2]}%**\n`{cells}`")
        peaks.append((top[2], label, top))
    hot = [x for x in peaks if x[0] >= RAIN_ALERT]
    head = ("☔ **今天要帶傘：**" + "、".join(f"{l}（{t[0].hour:02d}–{t[1].hour:02d} 點 {p}%）" for p, l, t in sorted(hot, reverse=True))
            if hot else "☀ 你盯的地點今天降雨機率都低於 70%")
    return {"title": f"🗓 今日降雨時間表｜{now.strftime('%m/%d')}", "url": PAGE, "color": 0x00897B,
            "description": head + "\n\n" + "\n\n".join(lines) +
                           "\n\n每格＝從幾點開始的 3 小時降雨機率（例：`15 80%`＝15–18 點 80%）",
            "footer": {"text": "資料：中央氣象署 鄉鎮區 2 天預報（3 小時一筆）"}}


def mutation_alerts(sp, state):
    """⚡ 小方向：接下來 3 小時的降雨機率，跨過 70%（變高）或下過雨後掉到 30% 以下（雨停）才通知"""
    now = datetime.now(TW)
    target = now + timedelta(minutes=60)
    st = state.setdefault("spots", {})
    msgs = []
    for label, _, _ in SPOTS:
        f = sp.get(label)
        if not f:
            continue
        cur = next((b for b in f["blocks"] if b[0] <= target < b[1] and b[2] is not None), None)
        if not cur:
            continue
        s0 = st.setdefault(label, {"raining": False, "pop": None})
        prev = s0["pop"]
        rng = f"{cur[0].hour:02d}–{cur[1].hour:02d} 點"
        if cur[2] >= RAIN_ALERT and not s0["raining"]:
            msgs.append(f"☔ **{label}**　{rng} 降雨機率 **{cur[2]}%**" + (f"（剛剛 {prev}%）" if prev is not None else "") + f"　{cur[3]}")
            s0["raining"] = True
        elif s0["raining"] and cur[2] <= RAIN_STOP:
            msgs.append(f"🌤 **{label}**　{rng} 降雨機率降到 **{cur[2]}%**，應該快停了　{cur[3]}")
            s0["raining"] = False
        s0["pop"] = cur[2]
    return msgs


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


# ---- 🌫 空氣品質（環境部 aqx_p_432，每小時更新；要免費申請的 MOENV_API_KEY）----
AQI_SITES = {"大園": "大園", "青埔（中壢）": "中壢", "土城": "土城", "台北101（信義）": "松山",
             "竹科（新竹東區）": "新竹", "竹北": "湖口"}          # 地點 → 最近的測站
AQI_ALERT, AQI_CLEAR = 151, 100                                  # ≥151 紅色（對所有人不健康）提醒；降到 ≤100 才解除


def aqi_level(v):
    for lim, name, ic in ((50, "良好", "🟢"), (100, "普通", "🟡"), (150, "敏感族群不健康", "🟠"),
                          (200, "不健康", "🔴"), (300, "非常不健康", "🟣")):
        if v <= lim:
            return name, ic
    return "危害", "🟤"


def aqi():
    key = os.environ.get("MOENV_API_KEY", "").strip()
    if not key:
        return None
    url = "https://data.moenv.gov.tw/api/v2/aqx_p_432?" + urllib.parse.urlencode({"api_key": key, "format": "json", "limit": 1000})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0"}), timeout=30) as r:
        d = json.loads(r.read())
    rows = d.get("records", d) if isinstance(d, dict) else d
    by = {str(x.get("sitename", "")).strip(): x for x in rows if isinstance(x, dict)}
    out = {}
    for spot, site in AQI_SITES.items():
        x = by.get(site)
        try:
            v = int(float(x.get("aqi")))
        except Exception:
            continue
        lv, ic = aqi_level(v)
        out[spot] = {"site": site, "aqi": v, "level": lv, "icon": ic, "pollutant": (x.get("pollutant") or "").strip(),
                     "time": (x.get("publishtime") or "")[-8:-3] or (x.get("publishtime") or "")}
    return out


def aqi_line(aq):
    return "　".join(f"{s.split('（')[0]} {v['icon']}{v['aqi']}" for s, v in aq.items())


def aqi_alerts(aq, state):
    on = state.setdefault("aqi_alert", {})
    msgs = []
    for spot, v in aq.items():
        if v["aqi"] >= AQI_ALERT and not on.get(spot):
            on[spot] = True
            msgs.append(f"{v['icon']} **{spot}**（{v['site']}測站）AQI **{v['aqi']}**｜{v['level']}" + (f"｜主要：{v['pollutant']}" if v["pollutant"] else ""))
        elif v["aqi"] <= AQI_CLEAR and on.get(spot):
            on[spot] = False
            msgs.append(f"🟢 {spot} 空氣好轉，AQI {v['aqi']}（{v['level']}）")
    return msgs


# ---- 🌏 地震：氣象署地震報告（E-A0015-001 顯著有感、E-A0016-001 小區域有感）----
QUAKE_DS = ("E-A0015-001", "E-A0016-001")
MY_COUNTIES = ("桃園市", "新北市", "臺北市", "新竹市", "新竹縣")   # 你那 6 個地點所在的縣市
QUAKE_SMALL_MIN = 3        # 小區域地震：你的縣市震度 ≥ 3 級才通知（顯著有感地震全部通知）


def lvl(s):
    """「4級」「5弱」「5強」→ 可以比大小的數字"""
    m = re.match(r"(\d)\s*(弱|強)?", str(s or ""))
    if not m:
        return 0
    return int(m.group(1)) + (0.5 if m.group(2) == "強" else 0)


def quakes():
    key = os.environ.get("CWA_API_KEY", "").strip()
    out = []
    for ds in QUAKE_DS:
        url = f"https://opendata.cwa.gov.tw/api/v1/rest/datastore/{ds}?" + urllib.parse.urlencode(
            {"Authorization": key, "format": "JSON", "limit": 5})
        with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0"}), timeout=30) as r:
            for e in json.loads(r.read())["records"]["Earthquake"]:
                e["_ds"] = ds
                out.append(e)
    return out


def quake_id(e):
    return str(e.get("Web") or f'{e.get("EarthquakeNo")}-{e.get("EarthquakeInfo", {}).get("OriginTime")}')


def quake_embed(e):
    info = e.get("EarthquakeInfo", {})
    mag = (info.get("EarthquakeMagnitude") or {}).get("MagnitudeValue")
    areas = (e.get("Intensity") or {}).get("ShakingArea") or []
    county = {}
    for a in areas:
        c, v = a.get("CountyName"), a.get("AreaIntensity")
        if c and lvl(v) > lvl(county.get(c)):
            county[c] = v
    mine = [f"**{c}** {county[c]}" if lvl(county[c]) >= 3 else f"{c} {county[c]}" for c in MY_COUNTIES if c in county]
    top = max((lvl(v) for v in county.values()), default=0)
    color = 0xB71C1C if top >= 5 else 0xE65100 if top >= 4 else 0xF9A825 if top >= 3 else 0x2E7D32
    t = (info.get("OriginTime") or "")[5:16].replace("T", " ")
    kind = "顯著有感地震" if e["_ds"] == "E-A0015-001" else "小區域有感地震"
    desc = [e.get("ReportContent", ""), "",
            f"🕐 {t}｜規模 **{mag}**｜深度 {info.get('FocalDepth')} 公里",
            f"📍 {(info.get('Epicenter') or {}).get('Location', '').replace('  ', ' ')}",
            f"🏠 你常去的地方：{'、'.join(mine) if mine else '沒有感覺（不在有感範圍）'}",
            "", "震度 3 級以上會加粗｜資料：中央氣象署 地震報告（地震後約 5～10 分鐘發布）"]
    emb = {"title": f"🌏 {kind}｜最大震度 {max(county.values(), key=lvl) if county else '—'}", "url": e.get("Web"),
           "color": color, "description": "\n".join(desc)}
    if e.get("ReportImageURI"):
        emb["image"] = {"url": e["ReportImageURI"]}
    return emb, max((lvl(county[c]) for c in MY_COUNTIES if c in county), default=0)


def quake_main(state, hook, preview):
    seen = state.setdefault("quake_seen", [])
    first = not seen and not preview                 # 預覽模式：全部印出來看格式
    try:
        qs = quakes()
    except Exception as ex:
        print(f"::warning title=地震::讀不到地震報告：{ex}")
        return
    new = [e for e in sorted(qs, key=lambda e: e.get("EarthquakeInfo", {}).get("OriginTime", "")) if quake_id(e) not in seen]
    sent = 0
    for e in new:
        seen.append(quake_id(e))
        if first:                                    # 第一次跑：只記住現有的，不把舊地震全部發出來
            continue
        emb, mine = quake_embed(e)
        if e["_ds"] == "E-A0016-001" and mine < QUAKE_SMALL_MIN:
            continue                                 # 別的地方的小地震，不吵你
        sent += 1
        if preview:
            print("::notice title=預覽 地震::" + json.dumps(emb, ensure_ascii=False)[:2500])
        else:
            call(hook, {"username": "Video Helper 地震", "embeds": [emb]})
    state["quake_seen"] = seen[-60:]
    latest = max(qs, key=lambda e: e.get("EarthquakeInfo", {}).get("OriginTime", ""), default=None)
    if latest:                                       # 給即時介面：最近一次地震
        emb, _ = quake_embed(latest)
        state.setdefault("dash", {})["quake"] = {"title": emb["title"], "text": latest.get("ReportContent", ""),
                                                  "url": latest.get("Web"), "time": latest["EarthquakeInfo"].get("OriginTime")}
    print(f"::notice title=地震::報告 {len(qs)} 筆、新的 {len(new)} 筆、發出 {sent} 則" + ("（第一次跑，只記住不發）" if first else ""))


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    mode = os.environ.get("MODE", "check")
    hook = os.environ.get("DISCORD_WEATHER_WEBHOOK", "").strip()
    preview = os.environ.get("PREVIEW") == "true" or not hook
    if mode == "quake":                              # 🌏 地震：鬧鐘每 5 分鐘叫一次，只看地震（很快）
        quake_main(state, os.environ.get("DISCORD_QUAKE_WEBHOOK", "").strip() or hook, preview)
        if not preview:
            json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False)
        return
    sp = spots_forecast()
    print("::notice title=氣象 地點::" + "｜".join(
        f"{l} " + ",".join(f"{b[0].hour:02d}:{b[2]}%" for b in v["blocks"][:4]) for l, v in sp.items()))
    try:
        aq = aqi()
    except Exception as e:
        print(f"::warning title=空氣品質::讀不到：{e}")
        aq = None
    if aq:
        print("::notice title=空氣品質::" + aqi_line(aq))
    if mode == "daily":
        fc = parse(fetch())
        embeds = [timetable_embed(sp)] + ([daily_embed(fc)] if len(fc) >= 10 else [])
        if aq:
            embeds[0]["description"] += "\n\n🌫 **空氣品質（AQI）**　" + aqi_line(aq) + \
                "\n🟢良好 🟡普通 🟠敏感族群不健康 🔴不健康 🟣非常不健康｜資料：環境部（最近的測站）"
        if preview:
            for e in embeds:
                print("::notice title=預覽 每日::" + json.dumps(e, ensure_ascii=False)[:3000])
        else:
            call(hook, {"username": "Video Helper 氣象", "embeds": embeds})
    try:
        wn = warnings()
    except Exception as e:
        print(f"::warning title=氣象::讀不到天氣特報：{e}")
        wn = None
    if wn is not None:
        wmsgs = warning_alerts(wn, state)
        if wmsgs:
            e = {"title": "⛈ 天氣特報（你常去的縣市）", "url": "https://www.cwa.gov.tw/V8/C/P/Warning/FIFOWS.html",
                 "color": 0xC62828, "description": "\n".join(wmsgs) + "\n\n資料：中央氣象署 天氣特報"}
            if preview:
                print("::notice title=預覽 特報::" + json.dumps(e, ensure_ascii=False)[:2000])
            else:
                call(hook, {"username": "Video Helper 氣象", "embeds": [e]})
    if aq:
        am = aqi_alerts(aq, state)
        if am:
            e = {"title": "🌫 空氣品質提醒", "url": "https://airtw.moenv.gov.tw/", "color": 0xC62828,
                 "description": "\n".join(am) + f"\n\nAQI ≥ {AQI_ALERT}（紅色）＝對所有人都不健康，少出門、戴口罩\n資料：環境部 空氣品質監測"}
            if preview:
                print("::notice title=預覽 空氣::" + json.dumps(e, ensure_ascii=False)[:1500])
            else:
                call(hook, {"username": "Video Helper 氣象", "embeds": [e]})
    msgs = mutation_alerts(sp, state)
    if msgs:
        e = {"title": "⚡ 降雨突變提醒（接下來 3 小時）", "url": PAGE, "color": 0x1565C0,
             "description": "\n".join(msgs) + f"\n\n☔＝降雨機率剛升到 {RAIN_ALERT}% 以上｜🌤＝下過雨後降到 {RAIN_STOP}% 以下\n資料：中央氣象署（3 小時一筆）"}
        if preview:
            print("::notice title=預覽 突變::" + json.dumps(e, ensure_ascii=False)[:2500])
        else:
            call(hook, {"username": "Video Helper 氣象", "embeds": [e]})
    # 給即時介面用：每個地點「現在這 3 小時」和「今天最可能下雨的時段」
    now = datetime.now(TW)
    dash = {}
    for label, _, _ in SPOTS:
        f = sp.get(label)
        if not f:
            continue
        bl = [b for b in f["blocks"] if b[2] is not None]
        cur = next((b for b in bl if b[0] <= now < b[1]), bl[0] if bl else None)
        today = [b for b in bl if b[0].date() == now.date() and b[1] > now] or bl[:4]
        top = max(today, key=lambda b: b[2]) if today else None
        ts = [v for k, v in f["temp"].items() if k.date() == now.date()]
        dash[label] = {
            "now": {"range": f"{cur[0].hour:02d}–{cur[1].hour:02d}", "pop": cur[2], "wx": cur[3]} if cur else None,
            "peak": {"range": f"{top[0].hour:02d}–{top[1].hour:02d}", "pop": top[2]} if top else None,
            "temp": [min(ts), max(ts)] if ts else None}
    state["dash"] = {"updated": now.strftime("%Y-%m-%d %H:%M"), "spots": dash,
                     # 給即時介面的「查某個時間」和 Discord /天氣 用：未來 2 天每 3 小時的預報、每小時氣溫
                     "blocks": {l: [[b[0].isoformat(), b[1].isoformat(), b[2], b[3]] for b in v["blocks"]] for l, v in sp.items()},
                     "temps": {l: [[k.isoformat(), t] for k, t in sorted(v["temp"].items())] for l, v in sp.items()},
                     "warnings": wn or {},
                     "quake": (state.get("dash") or {}).get("quake"),   # 地震那邊寫的，保留
                     "aqi": aq or (state.get("dash") or {}).get("aqi")}
    if not preview:
        state["last"] = datetime.now(TW).strftime("%Y-%m-%d %H:%M")
        json.dump(state, open(path, "w", encoding="utf-8"), ensure_ascii=False)
    print(f"::notice title=氣象::{mode} 完成；突變提醒 {len(msgs)} 則")


def probe():
    """診斷：看天氣特報（W-C0033-001）的資料長怎樣"""
    key = os.environ.get("CWA_API_KEY", "").strip()
    url = "https://opendata.cwa.gov.tw/api/v1/rest/datastore/W-C0033-001?" + urllib.parse.urlencode({"Authorization": key, "format": "JSON"})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-WeatherBot/1.0"}), timeout=30) as r:
        d = json.loads(r.read())
    txt = json.dumps(d, ensure_ascii=False)
    print(f"::notice title=特報 結構::{len(txt)} 字｜{txt[:1200]}")
    recs = d.get("records", {})
    locs = recs.get("location") or recs.get("Location") or []
    with_hz = [l for l in locs if (l.get("hazardConditions") or {}).get("hazards")]
    print(f"::notice title=特報 有發布的::{len(locs)} 縣市，{len(with_hz)} 個有特報｜" + json.dumps(with_hz[:2], ensure_ascii=False)[:1500])
    return
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

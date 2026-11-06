"""
匯率預報擂台：幾種預報方法用同一份歷史資料「考試」，看誰比較準。每週發一次到 #匯率分析。

怎麼考（回測，不偷看未來）：
  每隔 5 個營業日當一次「考試日 t」，每種方法只能看 t 以前（含 t）的價格，
  猜 5 個營業日後會比 t 高還是低；同時給一個「68% 會落在裡面」的範圍。
  每次考試的答案期間不重疊（每 5 天一次），統計才誠實。

方向方法：
  永遠猜漲   ＝ 基準線（看這段時間本來就比較常漲還是跌）
  動能       ＝ 最近 5 天漲 → 猜繼續漲
  均值回歸   ＝ 比 20 天平均貴 → 猜會跌回來
  類比       ＝ 跟正式預報一樣：最近 10 天形狀找過去最像的 20 段，多數漲就猜漲
  機器學習   ＝ 邏輯迴歸，看 5 天、20 天漲跌、離平均多遠、波動多大；只用考試日以前的資料訓練
範圍方法：30 天波動度（正式預報用的）、EWMA 波動度（近期加重）、歷史分位（過去一年真實 5 天漲跌的 16%～84%）

用法：python fx_backtest.py 結果.json
環境變數：PREVIEW=true 只印、SYNTH=true 用模擬資料測程式（開發機連不到 FRED）
"""
import json
import math
import os
import random
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fx_feed as fx  # noqa: E402

YEARS = 6                 # 拿過去幾年的資料考試
WARMUP = 500              # 前 2 年只當「課本」，不考試
H = fx.FC_DAYS            # 猜幾天後（5）
STEP = H                  # 每 5 天考一次 → 答案期間不重疊
CODES = fx.FC_CODES
DIR_METHODS = ["永遠猜漲", "動能", "均值回歸", "類比", "機器學習"]
RANGE_METHODS = ["30天波動度", "EWMA波動度", "歷史分位"]


# ---------- 方向方法：回傳 1（猜漲）、0（猜跌）、None（不出手） ----------
def m_up(p, t):
    return 1


def m_mom(p, t):
    return 1 if p[t] > p[t - H] else 0


def m_rev(p, t):
    ma = sum(p[t - 19:t + 1]) / 20
    return 1 if p[t] < ma else 0


def m_analog(p, t):
    a = fx.analog(p[:t + 1], p[t - fx.W:t + 1])
    if not a or a["up"] * 2 == a["n"]:
        return None
    return 1 if a["up"] * 2 > a["n"] else 0


def feats(p, t):
    r5 = math.log(p[t] / p[t - 5])
    r20 = math.log(p[t] / p[t - 20])
    win = p[t - 19:t + 1]
    ma = sum(win) / 20
    sd = (sum((x - ma) ** 2 for x in win) / 19) ** 0.5 or 1e-9
    rets = [math.log(b / a) for a, b in zip(p[t - 20:t], p[t - 19:t + 1])]
    vol = (sum(r * r for r in rets) / len(rets)) ** 0.5
    return [r5, r20, (p[t] - ma) / sd, vol]


class Logit:
    """邏輯迴歸（自己寫，不裝套件）：每 20 次考試重新訓練一次，只用已經知道答案的過去資料"""

    def __init__(self):
        self.w, self.cache_t = None, -10 ** 9

    def fit(self, p, t):
        X, y = [], []
        for s in range(25, t - H + 1):                    # 答案（s+H）要在 t 以前就已知
            X.append(feats(p, s))
            y.append(1 if p[s + H] > p[s] else 0)
        if len(X) < 100:
            self.w = None
            return
        k = len(X[0])
        self.mu = [sum(r[j] for r in X) / len(X) for j in range(k)]
        self.sd = [(sum((r[j] - self.mu[j]) ** 2 for r in X) / len(X)) ** 0.5 or 1 for j in range(k)]
        Z = [[(r[j] - self.mu[j]) / self.sd[j] for j in range(k)] for r in X]
        w = [0.0] * (k + 1)
        for _ in range(300):                              # 梯度下降＋一點 L2，避免背答案
            g = [0.0] * (k + 1)
            for z, yy in zip(Z, y):
                s = w[0] + sum(a * b for a, b in zip(w[1:], z))
                e = 1 / (1 + math.exp(-max(-30, min(30, s)))) - yy
                g[0] += e
                for j in range(k):
                    g[j + 1] += e * z[j]
            n = len(Z)
            w = [wi - 0.5 * (gi / n + (0.01 * wi if i else 0)) for i, (wi, gi) in enumerate(zip(w, g))]
        self.w = w

    def __call__(self, p, t):
        if t - self.cache_t >= 20 * STEP:
            self.fit(p, t)
            self.cache_t = t
        if not self.w:
            return None
        z = [(v - m) / s for v, m, s in zip(feats(p, t), self.mu, self.sd)]
        s = self.w[0] + sum(a * b for a, b in zip(self.w[1:], z))
        return 1 if s > 0 else 0


# ---------- 範圍方法：回傳 (下限, 上限) ----------
def r_vol30(p, t):
    return fx.vol_range(p[t - 30:t + 1], p[t])


def r_ewma(p, t, lam=0.94):
    var = None
    for a, b in zip(p[t - 250:t], p[t - 249:t + 1]):
        r = math.log(b / a)
        var = r * r if var is None else lam * var + (1 - lam) * r * r
    sd = var ** 0.5 * H ** 0.5
    return p[t] * math.exp(-sd), p[t] * math.exp(sd)


def r_hist(p, t):
    outs = sorted(p[s + H] / p[s] for s in range(t - 250, t - H + 1))
    q = lambda f: outs[int(f * (len(outs) - 1))]
    return p[t] * q(0.16), p[t] * q(0.84)


def wilson(k, n, z=1.96):
    """命中率的 95% 信賴區間（考的次數少，區間就寬）"""
    if not n:
        return 0, 0
    ph = k / n
    c = (ph + z * z / (2 * n)) / (1 + z * z / n)
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return (c - h) * 100, (c + h) * 100


def run(series):
    """series：{貨幣: [每日價格…]} → 每種方法的成績"""
    res = {"dir": {m: {"k": 0, "n": 0, "by": {}} for m in DIR_METHODS},
           "rng": {m: {"in": 0, "n": 0, "w": 0.0} for m in RANGE_METHODS}}
    for code, p in series.items():
        if len(p) < WARMUP + 50:
            print(f"::warning title=擂台::{code} 資料太少（{len(p)} 天），跳過")
            continue
        dirs = {"永遠猜漲": m_up, "動能": m_mom, "均值回歸": m_rev, "類比": m_analog, "機器學習": Logit()}
        rngs = {"30天波動度": r_vol30, "EWMA波動度": r_ewma, "歷史分位": r_hist}
        for t in range(WARMUP, len(p) - H, STEP):
            up = p[t + H] > p[t]
            for name, fn in dirs.items():
                g = fn(p, t)
                if g is None:
                    continue
                d = res["dir"][name]
                hit = int(g == up)
                d["k"] += hit
                d["n"] += 1
                b = d["by"].setdefault(code, [0, 0])
                b[0] += hit
                b[1] += 1
            for name, fn in rngs.items():
                r = fn(p, t)
                if not r:
                    continue
                x = res["rng"][name]
                x["in"] += int(r[0] <= p[t + H] <= r[1])
                x["n"] += 1
                x["w"] += (r[1] - r[0]) / p[t] * 100
    return res


def pad(t, n):
    """中文字在等寬字型佔 2 格，補空白讓表格對齊"""
    import unicodedata
    w = sum(2 if unicodedata.east_asian_width(ch) in "WF" else 1 for ch in t)
    return t + " " * max(0, n - w)


Z_STRICT = 2.58           # 一次比 5 種方法，總有一種會「運氣好」→ 判斷有沒有用要更嚴（約 99%，Bonferroni）


def verdict(lo, hi):
    if lo > 50:
        return "可能真的有用"
    if hi < 50:
        return "反著用才準？"
    return "跟丟硬幣分不出來"


def report(res, days, live=""):
    rows = []
    for m in DIR_METHODS:
        d = res["dir"][m]
        if not d["n"]:
            continue
        lo, hi = wilson(d["k"], d["n"])
        slo, shi = wilson(d["k"], d["n"], Z_STRICT)
        rows.append((d["k"] / d["n"] * 100, m, d, lo, hi, verdict(slo, shi), slo))
    rows.sort(key=lambda r: -r[0])
    t1 = [pad("#  Method", 13) + " Hit%    N  95%CI      判讀"]
    for i, (pct, m, d, lo, hi, v, _) in enumerate(rows):
        t1.append(f"{i + 1}  " + pad(m, 10) + f" {pct:4.1f} {d['n']:4d}  {lo:2.0f}-{hi:2.0f}%    {v}")
    t2 = [pad("各貨幣 Hit%", 10) + "  " + "  ".join(f"{c:>4}" for c in CODES)]
    for _, m, d, *_ in rows:
        cells = []
        for c in CODES:
            b = d["by"].get(c)
            cells.append(f"{b[0] / b[1] * 100:4.0f}" if b and b[1] else "   -")
        t2.append(pad(m, 10) + "  " + "  ".join(cells))
    t3 = [pad("Range", 12) + "Cover%  Width%  判讀（目標 68%）"]
    for m in RANGE_METHODS:
        x = res["rng"][m]
        if not x["n"]:
            continue
        cov = x["in"] / x["n"] * 100
        lo, hi = wilson(x["in"], x["n"])
        v = "剛好" if lo <= 68 <= hi else ("太窄（常跑出去）" if cov < 68 else "太寬（太保守）")
        t3.append(pad(m, 12) + f" {cov:5.1f}   {x['w'] / x['n']:5.2f}  {v}")
    best = rows[0] if rows else None
    note = []
    if best:
        note.append(f"• 這次第一名是 **{best[1]}**（{best[0]:.1f}%）。"
                    + ("用嚴格標準（同時比 5 種，要 99% 信心）仍贏過 50%，**可能真的有一點用**。" if best[6] > 50
                       else "但用嚴格標準（同時比 5 種，總有一種會運氣好），**還不能說它比丟硬幣強**。"))
    note.append("• 「永遠猜漲」是基準線：別的方法要贏它才算有學到東西，不然只是剛好碰上一段比較常漲（或跌）的時期。")
    note.append("• 範圍：Cover% 越接近 68% 越好；一樣準的話 Width%（範圍寬度）越窄越有用。")
    note.append("• 這是用過去資料考試，不是投資建議；過去準不代表以後準。")
    desc = ("**方向：5 個營業日後會比現在高還是低**\n```\n" + "\n".join(t1) + "\n```\n"
            + "```\n" + "\n".join(t2) + "\n```\n"
            + "**範圍：說 68% 會落在裡面，實際有幾 %**\n```\n" + "\n".join(t3) + "\n```\n"
            + (live + "\n" if live else "")
            + "**解讀**\n" + "\n".join(note))
    return {"title": "🏟 匯率預報擂台（每週）", "color": 0xF1C40F, "description": desc[:4096],
            "footer": {"text": f"資料：FRED 美聯準會每日匯率，換算成台幣｜{', '.join(CODES)}｜約 {days} 個營業日｜"
                               f"前 {WARMUP} 天只當課本｜每 {STEP} 天考一次、答案不重疊"},
            "url": fx.DOC}


def live_score():
    """正式預報（每天 17:00 那則）已經對過答案的成績"""
    try:
        st = json.load(open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "fx_state.json"), encoding="utf-8"))
        return "📏 **正式預報實戰成績**：" + fx.score(st, {}, None).split("：", 1)[-1]
    except Exception:
        return ""


def synth():
    random.seed(1)
    out = {}
    for c, p0 in (("USD", 31.0), ("EUR", 34.0), ("JPY", 0.22), ("CNY", 4.4)):
        p = [p0]
        for _ in range(1500):
            p.append(p[-1] * math.exp(random.gauss(0, 0.004)))
        out[c] = p
    return out


def main(path):
    if os.environ.get("SYNTH") == "true":
        series = synth()
    else:
        twd = fx.fred("DEXTAUS", YEARS)
        series = {}
        for c in CODES:
            try:
                series[c] = fx.long_series(c, twd, YEARS)
            except Exception as e:
                print(f"::warning title=擂台::{c} 讀不到 FRED：{e}")
    res = run(series)
    days = max((len(p) for p in series.values()), default=0)
    emb = report(res, days, live_score())
    print(emb["description"])
    json.dump({"updated": datetime.now(fx.TW).strftime("%Y-%m-%d %H:%M"), "dir": {m: {k: v for k, v in d.items()}
               for m, d in res["dir"].items()}, "rng": res["rng"]},
              open(path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    hook = (os.environ.get("DISCORD_FX_ANALYSIS_WEBHOOK") or os.environ.get("DISCORD_FX_WEBHOOK") or "").strip()
    if os.environ.get("PREVIEW") == "true" or not hook:
        print("::notice title=擂台::只預覽，沒發 Discord")
        return
    fx.call("POST", hook + "?wait=true", {"username": "匯率預報擂台", "embeds": [emb]})


if __name__ == "__main__":
    main(sys.argv[1])

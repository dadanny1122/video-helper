"""意見回饋＋回饋獎勵（電腦版 v1.49.0，docs/infra/04-意見回饋與回饋獎勵規格.md；F1～F5 照建議）

- fb_rule()：字數規則（中日韓文字 ≥16，或英文單字 ≥20；同一個字／單字連續超過 5 次只算 1；網址不算；最多 1000 字）
  跟腳本、新介面、Cloudflare Worker 的 fbRule() 同一套（.github/feedback_test.py 用同一份 cases.json 比對）
- fb_mask()：網址換成「[網址已移除]」再送
- send()：POST 到你的 Worker /feedback → Discord #意見回饋；回一組回饋代碼（Worker 用 Ed25519 簽）
- verify_token()／rewards()：代碼驗證、算好處（徽章、限定配色、搶先試用）
"""
import base64
import json
import re
import urllib.error
import urllib.request

# F1：你的 Cloudflare Worker（不是秘密；Worker 收到後轉到 Discord #意見回饋）
FEEDBACK_URL = "https://vh-discord.dadanny1122.workers.dev"
# Worker 第一次用時自己產生的簽名公鑰（GET /feedback/pubkey 拿；私鑰只在 Cloudflare KV）
FB_PUBKEY = "uxr-uJFENv6QW2uwAExFfkX1kZcpYyud4rEl70GoIM8"

KINDS = {"suggest": "💡 建議", "bug": "🐞 問題", "praise": "👍 稱讚", "other": "💬 其他"}
MAX_CHARS = 1000
NEED_CJK, NEED_WORDS = 16, 20            # 中文「超過 15 字」、英文「20 個單字」
LEVELS = ((10, "gold", "🥇 金牌支持者"), (3, "silver", "🥈 銀牌支持者"), (1, "bronze", "🥉 銅牌支持者"))   # F3
THEMES = {"aurora": "極光", "dusk": "夕暮"}                                                            # F4
BETA_AT = 3                                                                                           # F5

# FB-RULE（四個地方要一樣：這裡、video-helper.user.js、webui/app.js、cloudflare/worker.js）
FB_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯豈-﫿]")
FB_URL = re.compile(r"(?:https?://|www\.)\S+", re.I)
FB_WORD = re.compile(r"[A-Za-z0-9]+(?:'[A-Za-z]+)?")


def _runs(items):
    """連續一樣的東西：超過 5 個只算 1 個"""
    n, i = 0, 0
    while i < len(items):
        j = i
        while j < len(items) and items[j] == items[i]:
            j += 1
        n += 1 if j - i > 5 else j - i
        i = j
    return n


def fb_rule(text):
    raw = "" if text is None else str(text)
    clean = FB_URL.sub(" ", raw)
    chars = list(clean)
    cjk, i = 0, 0
    while i < len(chars):
        j = i
        while j < len(chars) and chars[j] == chars[i]:
            j += 1
        if FB_CJK.match(chars[i]):
            cjk += 1 if j - i > 5 else j - i
        i = j
    words = _runs([w.lower() for w in FB_WORD.findall(clean)])
    too_long = len(raw) > MAX_CHARS
    return {"cjk": cjk, "words": words, "too_long": too_long, "ok": (cjk >= NEED_CJK or words >= NEED_WORDS) and not too_long}


def fb_mask(text):
    return FB_URL.sub("[網址已移除]", str(text))


def need_text(r):
    """還差多少（給畫面顯示）"""
    if r["too_long"]:
        return f"太長了（最多 {MAX_CHARS} 字）"
    if r["ok"]:
        return "可以送出了"
    return f"還差 {NEED_CJK - r['cjk']} 個中文字，或 {NEED_WORDS - r['words']} 個英文單字"


# ---- 回饋代碼：VH1.<內容>.<簽章>（Worker 的 Ed25519 私鑰簽，這裡用公鑰驗）----
def _b64d(s):
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def verify_token(token, pubkey=None):
    """對就回 {"id", "t"}；不對回 None"""
    pubkey = FB_PUBKEY if pubkey is None else pubkey
    try:
        tag, p, s = str(token).strip().split(".")
        if tag != "VH1" or len(token) > 400 or not pubkey:
            return None
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(_b64d(pubkey)).verify(_b64d(s), p.encode())
        d = json.loads(_b64d(p))
        if d.get("v") != 1 or not re.fullmatch(r"[0-9a-f]{16}", str(d.get("id"))):
            return None
        return {"id": d["id"], "t": int(d.get("t") or 0)}
    except Exception:
        return None


def rewards(tokens, pubkey=None):
    """手上的代碼 → 好處（同一個 id 只算一次）"""
    ids = {}
    for t in tokens or []:
        v = verify_token(t, pubkey)
        if v:
            ids[v["id"]] = v
    n = len(ids)
    level = next(((key, name) for need, key, name in LEVELS if n >= need), ("", ""))
    return {"count": n, "level": level[0], "badge": level[1], "themes": list(THEMES) if n >= 1 else [],
            "beta": n >= BETA_AT, "next": next((need for need, _, _ in reversed(LEVELS) if n < need), None)}


def send(kind, text, app, version, lang, url=None, timeout=20):
    """送出；成功回 {"ok": True, "token"}，失敗回 {"ok": False, "msg"}（中文，給畫面）"""
    url = FEEDBACK_URL if url is None else url
    if not url:
        return {"ok": False, "msg": "意見回饋還沒設定好（開發者還沒填 Worker 網址）"}
    if kind not in KINDS:
        return {"ok": False, "msg": "類型不對"}
    r = fb_rule(text)
    if not r["ok"]:
        return {"ok": False, "msg": need_text(r)}
    body = json.dumps({"kind": kind, "text": fb_mask(text), "app": app, "ver": version, "lang": lang}).encode()
    req = urllib.request.Request(url.rstrip("/") + "/feedback", data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": f"VideoHelper/{version}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            d = json.loads(resp.read(20000) or b"{}")
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read(20000) or b"{}")
        except ValueError:
            d = {}
        return {"ok": False, "msg": d.get("msg") or f"送不出去（{e.code}），等一下再試"}
    except Exception as e:
        return {"ok": False, "msg": f"連不到意見回饋服務（{type(e).__name__}），等一下再試"}
    if not d.get("ok") or not d.get("token"):
        return {"ok": False, "msg": d.get("msg") or "回應怪怪的，等一下再試"}
    return {"ok": True, "token": str(d["token"])[:400], "pub": str(d.get("pub") or "")[:100]}

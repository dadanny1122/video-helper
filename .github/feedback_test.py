"""意見回饋字數規則：電腦版（vh_feedback.py）跟 .github/feedback/cases.json 一樣；JS 那邊由 feedback_test.js 比對
python3 -I .github/feedback_test.py"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
import vh_feedback as F  # noqa: E402

fails = 0
for c in json.load(open(os.path.join(ROOT, ".github", "feedback", "cases.json"), encoding="utf-8")):
    t = c["unit"] * c["repeat"] if c["text"] == "LONG" else c["text"]
    r = F.fb_rule(t)
    good = r["cjk"] == c["cjk"] and r["words"] == c["words"] and r["ok"] == c["ok"] and r["too_long"] == c.get("too_long", False)
    if not good:
        fails += 1
        print("❌", t[:40], r, c)
assert F.fb_mask("看 https://x.com/a?b=1 和 www.y.com/z 這裡") == "看 [網址已移除] 和 [網址已移除] 這裡"

# 回饋代碼：真的 Ed25519 簽、改一個字就不過、同一個 id 只算一次
import base64  # noqa: E402
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives import serialization  # noqa: E402
k = Ed25519PrivateKey.generate()
pub = base64.urlsafe_b64encode(k.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode().rstrip("=")


def tok(i, key=k):
    p = base64.urlsafe_b64encode(json.dumps({"v": 1, "id": f"{i:016x}", "t": 1}).encode()).decode().rstrip("=")
    s = base64.urlsafe_b64encode(key.sign(p.encode())).decode().rstrip("=")
    return f"VH1.{p}.{s}"


t1 = tok(1)
checks = [
    (F.verify_token(t1, pub) == {"id": "0000000000000001", "t": 1}, "真代碼驗證通過"),
    (F.verify_token(t1[:-2] + ("AA" if t1[-2:] != "AA" else "BB"), pub) is None, "改簽章 → 不通過"),
    (F.verify_token(tok(1, Ed25519PrivateKey.generate()), pub) is None, "別人的私鑰簽的 → 不通過"),
    (F.verify_token("VH1.abc", pub) is None and F.verify_token("", pub) is None, "亂打 → 不通過"),
    (F.verify_token(t1, "") is None, "還沒有公鑰 → 不通過"),
    (F.rewards([], pub)["count"] == 0 and F.rewards([], pub)["themes"] == [], "沒回饋 → 沒好處"),
    (F.rewards([t1, t1], pub)["count"] == 1 and F.rewards([t1], pub)["level"] == "bronze" and F.rewards([t1], pub)["themes"] == ["aurora", "dusk"], "1 則 → 銅＋限定配色"),
    (F.rewards([tok(i) for i in range(3)], pub)["level"] == "silver" and F.rewards([tok(i) for i in range(3)], pub)["beta"], "3 則 → 銀＋搶先試用"),
    (not F.rewards([tok(i) for i in range(2)], pub)["beta"], "2 則 → 還沒有搶先試用"),
    (F.rewards([tok(i) for i in range(10)], pub)["level"] == "gold", "10 則 → 金"),
    (F.send("suggest", "太短", "desktop", "1", "zh", url="https://x.workers.dev")["ok"] is False, "字數不夠 → 不送"),
    (F.send("suggest", "這個工具很好用但是希望可以多一點", "desktop", "1", "zh", url="")["ok"] is False, "沒設定網址 → 不送"),
]
for ok, name in checks:
    print(("✅ " if ok else "❌ ") + name)
    fails += not ok
print("全部通過" if not fails else f"❌ {fails} 項沒過")
sys.exit(1 if fails else 0)

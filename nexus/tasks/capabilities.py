"""
心跳裡的節點能力（協定 1.1）：Master 端嚴格驗證，不合格就整份丟掉（照樣算心跳，只是不會被派任務）
協定 1.2（V3）：resources 多 isolation（basic／none）、isolation_reason（沙箱不能用的原因）
協定 1.4（APEX PR2）：resources 多 hardening（appcontainer／none）、hardening_reason——**只是偵測結果**，排程還不看它
協定 1.5（APEX，借 Dask）：resources 多 named＝自己取名字的資源 {"gpu": 1, "pysam": 1}，任務 requirements.resources 要的
         排程會算「宣告數量 − 正在跑的任務佔掉的」夠不夠
"""
import re

from nexus.core.errors import NexusError

_RES = {"cpu_count": (int, 1, 1024), "cpu_load": ((int, float), 0, 1), "memory_total_mb": (int, 0, 16_777_216),
        "memory_available_mb": (int, 0, 16_777_216), "disk_free_mb": (int, 0, 1 << 40), "platform": (str, 1, 16),
        "arch": (str, 1, 16), "isolation": (str, 1, 8), "isolation_reason": (str, 0, 200),
        "hardening": (str, 1, 16), "hardening_reason": (str, 0, 200)}
ISOLATION_LEVELS = ("basic", "none")
HARDENING_LEVELS = ("appcontainer", "none")
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,31}$")
_VER = re.compile(r"^\d{1,4}(\.\d{1,4}){0,2}$")
_UUID = re.compile(r"^[0-9a-f-]{36}$")


NAMED_MAX_KEYS = 16
NAMED_MAX_COUNT = 1024


def _bad(field):
    return NexusError("SCHEMA_INVALID", details={"where": "capabilities", "field": field})


def named_problem(d) -> str | None:
    """自訂資源（節點宣告的、任務要求的共用規則）：{名字: 數量}；名字小寫英數，數量 1～1024"""
    if not isinstance(d, dict) or len(d) > NAMED_MAX_KEYS:
        return f"要是 {{名字: 數量}}，最多 {NAMED_MAX_KEYS} 種"
    for k, v in d.items():
        if not isinstance(k, str) or not _NAME.match(k):
            return f"名字不行：{str(k)[:32]}（小寫英文開頭，只能有英數 . _ -，最多 32 字）"
        if isinstance(v, bool) or not isinstance(v, int) or not 1 <= v <= NAMED_MAX_COUNT:
            return f"{k} 的數量要是 1～{NAMED_MAX_COUNT} 的整數"
    return None


def parse_named(text: str) -> dict:
    """節點小視窗輸入「gpu=1, pysam」→ {"gpu": 1, "pysam": 1}（沒寫數量＝1）；格式不對丟 ValueError（中文原因）"""
    out = {}
    for part in (text or "").replace("，", ",").replace("、", ",").split(","):
        part = part.strip()
        if not part:
            continue
        name, _, num = part.partition("=")
        name = name.strip().lower()
        try:
            n = int(num.strip()) if num.strip() else 1
        except ValueError:
            raise ValueError(f"{name} 的數量要是整數") from None
        out[name] = n
    p = named_problem(out)
    if p:
        raise ValueError(p)
    return out


def named_text(d: dict | None) -> str:
    return "、".join(f"{k}×{v}" for k, v in sorted((d or {}).items()))


def normalize(body: dict) -> dict | None:
    """回傳 {"resources", "adapters", "max_concurrency", "accepting", "running"}；舊版（沒帶）回 None"""
    if "resources" not in body and "adapters" not in body:
        return None
    res, ads = body.get("resources") or {}, body.get("adapters") or {}
    if not isinstance(res, dict) or not isinstance(ads, dict):
        raise _bad("resources/adapters")
    if "named" in res:
        if named_problem(res["named"]):
            raise _bad("named")
        res = {k: v for k, v in res.items() if k != "named"} | {"named": dict(res["named"])}
    if set(res) - set(_RES) - {"named"}:
        raise _bad("resources")
    for k, v in res.items():
        if k == "named":
            continue
        typ, lo, hi = _RES[k]
        if isinstance(v, bool) or not isinstance(v, typ):
            raise _bad(k)
        n = len(v) if isinstance(v, str) else v
        if not lo <= n <= hi:
            raise _bad(k)
    if "isolation" in res and res["isolation"] not in ISOLATION_LEVELS:
        raise _bad("isolation")
    if "hardening" in res and res["hardening"] not in HARDENING_LEVELS:
        raise _bad("hardening")
    if len(ads) > 32 or any(not (isinstance(k, str) and _NAME.match(k) and isinstance(v, str) and _VER.match(v)) for k, v in ads.items()):
        raise _bad("adapters")
    mc = body.get("max_concurrency", 1)
    if isinstance(mc, bool) or not isinstance(mc, int) or not 1 <= mc <= 4:
        raise _bad("max_concurrency")
    acc = body.get("accepting", True)
    if not isinstance(acc, bool):
        raise _bad("accepting")
    running = body.get("running", [])
    if not isinstance(running, list) or len(running) > 64 or any(not (isinstance(x, str) and _UUID.match(x)) for x in running):
        raise _bad("running")
    return {"resources": res, "adapters": ads, "max_concurrency": mc, "accepting": acc, "running": running}

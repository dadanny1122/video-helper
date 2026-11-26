"""
心跳裡的節點能力（協定 1.1）：Master 端嚴格驗證，不合格就整份丟掉（照樣算心跳，只是不會被派任務）
協定 1.2（V3）：resources 多 isolation（basic／none）、isolation_reason（沙箱不能用的原因）
協定 1.4（APEX PR2）：resources 多 hardening（appcontainer／none）、hardening_reason——**只是偵測結果**，排程還不看它
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


def _bad(field):
    return NexusError("SCHEMA_INVALID", details={"where": "capabilities", "field": field})


def normalize(body: dict) -> dict | None:
    """回傳 {"resources", "adapters", "max_concurrency", "accepting", "running"}；舊版（沒帶）回 None"""
    if "resources" not in body and "adapters" not in body:
        return None
    res, ads = body.get("resources") or {}, body.get("adapters") or {}
    if not isinstance(res, dict) or not isinstance(ads, dict):
        raise _bad("resources/adapters")
    if set(res) - set(_RES):
        raise _bad("resources")
    for k, v in res.items():
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

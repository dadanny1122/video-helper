"""
Task Manifest（規格 §9.2／06-V2 §3.1）：Master 產生、算 SHA-256；Worker 收到時重算，不符就拒收（T-05）。
嚴格驗證：不認得的欄位、型別錯、超出範圍、整份超過 16KB 都在排程前拒絕（AT-005）。
"""
import datetime as dt
import hashlib
import json
import uuid

from nexus.core.models import parse_utc, utcnow
from nexus.tasks import adapters

MANIFEST_VERSION = 1
MAX_MANIFEST_BYTES = 16 * 1024
MAX_RESULT_BYTES = 4096
DEFAULT_TTL_SECONDS = 3600
PLATFORMS = ("windows", "any")

# 頂層欄位：名稱 → (型別, 必要)
TOP = {
    "manifest_version": (int, True), "task_id": (str, True), "type": (str, True), "adapter_version": (str, True),
    "created_at": (str, True), "expires_at": (str, True), "idempotency_key": (str, True), "params": (dict, True),
    "limits": (dict, True), "requirements": (dict, True), "retry_policy": (dict, True), "priority": (int, True),
}
SUB = {
    "limits": {"max_runtime_seconds": (int, 1, 900), "max_result_bytes": (int, 1, MAX_RESULT_BYTES)},
    "requirements": {"platform": (str, None, None), "min_memory_mb": (int, 0, 1_048_576), "min_disk_mb": (int, 0, 10_485_760)},
    "retry_policy": {"max_attempts": (int, 1, 5)},
}


def canonical(m: dict) -> bytes:
    return json.dumps(m, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def sha256(m: dict) -> str:
    return hashlib.sha256(canonical(m)).hexdigest()


def build(task_type: str, params: dict, *, idempotency_key: str | None = None, max_runtime_seconds: int | None = None,
          max_attempts: int = 2, min_memory_mb: int = 0, min_disk_mb: int = 0, platform: str = "any", priority: int = 0,
          ttl_seconds: int = DEFAULT_TTL_SECONDS) -> dict:
    """組一份 manifest（還沒驗證；交給 validate）"""
    spec = adapters.get(task_type)
    now = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
    return {
        "manifest_version": MANIFEST_VERSION,
        "task_id": str(uuid.uuid4()),
        "type": task_type,
        "adapter_version": spec.version if spec else "0",
        "created_at": utcnow(),
        "expires_at": (now + dt.timedelta(seconds=ttl_seconds)).isoformat().replace("+00:00", "Z"),
        "idempotency_key": idempotency_key or str(uuid.uuid4()),
        "params": params,
        "limits": {"max_runtime_seconds": max_runtime_seconds or (spec.default_runtime_seconds if spec else 60),
                   "max_result_bytes": MAX_RESULT_BYTES},
        "requirements": {"platform": platform, "min_memory_mb": min_memory_mb, "min_disk_mb": min_disk_mb},
        "retry_policy": {"max_attempts": max_attempts},
        "priority": priority,
    }


def validate(m) -> list[str]:
    """回傳問題清單（空＝合格）"""
    if not isinstance(m, dict):
        return ["manifest 必須是物件"]
    if len(canonical(m)) > MAX_MANIFEST_BYTES:
        return [f"manifest 超過 {MAX_MANIFEST_BYTES // 1024}KB"]
    problems = [f"不認得的欄位：{k}" for k in sorted(set(m) - set(TOP))]
    for k, (typ, req) in TOP.items():
        if k not in m:
            if req:
                problems.append(f"缺少欄位：{k}")
            continue
        if (typ is int and isinstance(m[k], bool)) or not isinstance(m[k], typ):
            problems.append(f"{k} 型別不對")
    if problems:
        return problems
    if m["manifest_version"] != MANIFEST_VERSION:
        problems.append("manifest_version 不支援")
    if not (1 <= len(m["idempotency_key"]) <= 64):
        problems.append("idempotency_key 長度要 1～64")
    if not -10 <= m["priority"] <= 10:
        problems.append("priority 要在 -10～10")
    for ts in ("created_at", "expires_at"):
        try:
            parse_utc(m[ts])
        except Exception:
            problems.append(f"{ts} 不是 UTC 時間")
    try:
        uuid.UUID(m["task_id"])
    except Exception:
        problems.append("task_id 不是 UUID")
    for name, fields in SUB.items():
        sub = m[name]
        problems += [f"{name} 不認得的欄位：{k}" for k in sorted(set(sub) - set(fields))]
        for k, (typ, lo, hi) in fields.items():
            if k not in sub:
                problems.append(f"{name} 缺少：{k}")
                continue
            v = sub[k]
            if (typ is int and isinstance(v, bool)) or not isinstance(v, typ):
                problems.append(f"{name}.{k} 型別不對")
            elif typ is int and not (lo <= v <= hi):
                problems.append(f"{name}.{k} 要在 {lo}～{hi}")
    if isinstance(m["requirements"].get("platform"), str) and m["requirements"]["platform"] not in PLATFORMS:
        problems.append("requirements.platform 只能是 windows／any")
    spec = adapters.get(m["type"])
    if spec is None:
        problems.append(f"不支援的任務類型：{m['type']}")
        return problems
    if not adapters.compatible(spec, m["adapter_version"]):
        problems.append(f"{m['type']} 版本 {m['adapter_version']} 不相容（目前 {spec.version}）")
    rt = m["limits"].get("max_runtime_seconds")
    if isinstance(rt, int) and rt > spec.max_runtime_seconds:
        problems.append(f"max_runtime_seconds 超過這個任務類型的上限 {spec.max_runtime_seconds}")
    problems += adapters.check_params(spec, m["params"])
    return problems


def expired(m: dict) -> bool:
    return parse_utc(m["expires_at"]) <= dt.datetime.now(dt.timezone.utc)

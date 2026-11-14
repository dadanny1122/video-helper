"""
訊息外框（規格 §14.1）：每個請求／回應都有
  protocol_version、message_id、sent_at、sender_id、correlation_id；請求另有 nonce
驗證：欄位型別、時間偏移（CLOCK_SKEW）、nonce 沒用過（REPLAY_DETECTED）。
Schema 驗證用手寫的小型檢查器（不加 pydantic）：禁止未定義欄位。
"""
import re
import threading
import time
import uuid
from datetime import datetime, timezone

from nexus import PROTOCOL_VERSION
from nexus.core.errors import NexusError

_UUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
_ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?(Z|[+-]\d{2}:\d{2})$")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def envelope(sender_id: str, correlation_id: str | None = None, **body) -> dict:
    """做一個帶外框的訊息（請求用；nonce 每次都新的）"""
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": str(uuid.uuid4()),
        "sent_at": now_iso(),
        "sender_id": sender_id,
        "correlation_id": correlation_id or str(uuid.uuid4()),
        "nonce": uuid.uuid4().hex,
        **body,
    }


def reply(sender_id: str, correlation_id: str, **body) -> dict:
    return {
        "protocol_version": PROTOCOL_VERSION,
        "message_id": str(uuid.uuid4()),
        "sent_at": now_iso(),
        "sender_id": sender_id,
        "correlation_id": correlation_id,
        **body,
    }


# ---------- Schema：{"欄位": (型別, 必要?, 最長/最大)} ----------
ENVELOPE_SCHEMA = {
    "protocol_version": (str, True, 16),
    "message_id": (str, True, 36),
    "sent_at": (str, True, 40),
    "sender_id": (str, True, 64),
    "correlation_id": (str, True, 36),
    "nonce": (str, True, 64),
}


def validate(data, schema: dict, extra_allowed: set | None = None, where: str = "body"):
    """型別、必要欄位、長度／大小、未定義欄位都檢查；錯就丟 SCHEMA_INVALID（不回傳對方的內容）"""
    if not isinstance(data, dict):
        raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "not an object"})
    allowed = set(schema) | (extra_allowed or set())
    unknown = set(data) - allowed
    if unknown:
        raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "unknown fields", "fields": sorted(unknown)[:10]})
    for key, (typ, required, limit) in schema.items():
        if key not in data:
            if required:
                raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "missing", "field": key})
            continue
        v = data[key]
        if typ is int and isinstance(v, bool):
            raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "type", "field": key})
        if not isinstance(v, typ):
            raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "type", "field": key})
        if isinstance(v, str) and limit and len(v) > limit:
            raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "too long", "field": key})
        if isinstance(v, (int, float)) and limit and v > limit:
            raise NexusError("SCHEMA_INVALID", details={"where": where, "problem": "too large", "field": key})
    return data


def check_envelope(data: dict, body_schema: dict, *, require_nonce=True) -> dict:
    """驗外框＋內容；回傳內容部分"""
    env = {k: data.get(k) for k in ENVELOPE_SCHEMA if k in data}
    schema = dict(ENVELOPE_SCHEMA)
    if not require_nonce:
        schema["nonce"] = (str, False, 64)
    validate(env, schema, where="envelope")
    if not _ISO.match(env["sent_at"]):
        raise NexusError("SCHEMA_INVALID", details={"where": "envelope", "field": "sent_at"})
    if not _UUID.match(env["message_id"]) or not _UUID.match(env["correlation_id"]):
        raise NexusError("SCHEMA_INVALID", details={"where": "envelope", "field": "message_id"})
    body = {k: v for k, v in data.items() if k not in ENVELOPE_SCHEMA}
    validate(body, body_schema)
    return body


class ReplayGuard:
    """(sender_id, nonce) 在 ttl 內只能出現一次；sent_at 偏移超過 skew 直接拒絕（不信任對方時鐘）"""

    def __init__(self, ttl_seconds=600, skew_seconds=300, max_entries=50000):
        self.ttl, self.skew, self.max = ttl_seconds, skew_seconds, max_entries
        self.seen = {}
        self.lock = threading.Lock()

    def check(self, sender_id: str, nonce: str, sent_at: str):
        try:
            ts = datetime.fromisoformat(sent_at.replace("Z", "+00:00")).timestamp()
        except Exception as e:
            raise NexusError("SCHEMA_INVALID", details={"field": "sent_at"}) from e
        now = time.time()
        if abs(now - ts) > self.skew:
            raise NexusError("CLOCK_SKEW", details={"max_skew_seconds": self.skew})
        key = (sender_id, nonce)
        with self.lock:
            if len(self.seen) > self.max or (len(self.seen) % 1000 == 0):
                cutoff = now - self.ttl
                self.seen = {k: t for k, t in self.seen.items() if t > cutoff}
            if key in self.seen and self.seen[key] > now - self.ttl:
                raise NexusError("REPLAY_DETECTED")
            self.seen[key] = now

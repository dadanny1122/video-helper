"""資料模型（純資料，不含行為）。欄位對應 docs/nexus/04 §7。時間一律 UTC ISO-8601 字串。"""
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(s: str) -> datetime:
    return datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone(timezone.utc)


@dataclass
class Device:
    device_id: str
    name: str
    platform: str
    agent_version: str
    protocol_version: str
    status: str
    public_key_fingerprint: str
    source_ip_first: str = ""
    created_at: str = field(default_factory=utcnow)
    approved_at: str | None = None
    approved_reason: str | None = None
    revoked_at: str | None = None
    revoked_reason: str | None = None
    last_seen_at: str | None = None
    connection_state: str = "UNKNOWN"
    last_sequence: int = 0
    version: int = 1                     # 樂觀鎖

    def to_dict(self):
        return asdict(self)


@dataclass
class Credential:
    credential_id: str
    device_id: str
    serial_hex: str
    fingerprint_sha256: str
    issued_at: str
    expires_at: str
    status: str = "ACTIVE"
    superseded_by: str | None = None
    revoked_at: str | None = None


@dataclass
class AuditEvent:
    action: str
    actor: str                           # owner | worker:<device_id> | system
    resource_type: str
    resource_id: str
    outcome: str                         # ok | denied | error
    reason: str = ""
    correlation_id: str = ""
    details: dict = field(default_factory=dict)
    ts: str = field(default_factory=utcnow)

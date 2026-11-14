"""
裝置登錄簿：所有裝置狀態改變的**唯一入口**（GUI、API、背景清查都只能呼叫這裡）。
每個改變＝檢查合法轉移 → 樂觀鎖更新 → 同一個交易寫稽核。
"""
import datetime as dt
import hmac
import logging
import secrets
import threading
import time
import uuid

from nexus import MIN_AGENT_VERSION, MIN_PEER_PROTOCOL, protocol_compatible, version_tuple
from nexus.core.enums import (
    OFFLINE_AFTER_SECONDS,
    STALE_AFTER_SECONDS,
    ConnectionState,
    CredentialStatus,
    DeviceStatus,
    can_transition,
)
from nexus.core.errors import NexusError
from nexus.core.models import AuditEvent, parse_utc, utcnow
from nexus.identity import certs

log = logging.getLogger("nexus.registry")
PAIR_SECONDS = 300
PAIR_MAX_FAILS = 5
REGISTRATION_TTL_SECONDS = 3600


class PairCode:
    """6 位數、5 分鐘、一碼一次、猜錯 5 次作廢（跟瀏覽器配對同一套規矩）"""

    def __init__(self):
        self.code, self.expires, self.fails, self.burned = None, 0.0, 0, False
        self.lock = threading.Lock()

    def new(self) -> str:
        with self.lock:
            self.code = f"{secrets.randbelow(10 ** 6):06d}"
            self.expires, self.fails, self.burned = time.time() + PAIR_SECONDS, 0, False
            return self.code

    def remaining(self) -> int:
        return max(0, int(self.expires - time.time())) if self.code else 0

    def consume(self, given: str):
        with self.lock:
            if self.burned:
                raise NexusError("PAIR_CODE_BURNED")
            if not self.code or time.time() > self.expires:
                raise NexusError("PAIR_CODE_EXPIRED")
            if not hmac.compare_digest(str(given).strip(), self.code):
                self.fails += 1
                if self.fails >= PAIR_MAX_FAILS:
                    self.code, self.burned = None, True
                    raise NexusError("PAIR_CODE_BURNED")
                raise NexusError("PAIR_CODE_INVALID")
            self.code = None


class DeviceRegistry:
    def __init__(self, db, repo, ca, connections, config):
        self.db, self.repo, self.ca, self.conns, self.cfg = db, repo, ca, connections, config
        self.pair = PairCode()
        self.lock = threading.Lock()           # 管理操作一次一個（樂觀鎖是第二道）

    # ---------- 共用 ----------
    def _audit(self, c, action, actor, resource_id, outcome="ok", reason="", correlation_id="", **details):
        self.repo.audit(AuditEvent(action, actor, "device", resource_id, outcome, reason, correlation_id, details), c)

    def _transition(self, c, device: dict, new: DeviceStatus, actor: str, action: str, reason: str, correlation_id="", **fields):
        cur = DeviceStatus(device["status"])
        if not can_transition(cur, new):
            self._audit(c, action, actor, device["device_id"], "denied", f"{cur.value}→{new.value} 不合法", correlation_id)
            raise NexusError("DEVICE_NOT_ACTIVE", message=f"狀態 {cur.value} 不能變成 {new.value}")
        if not self.repo.update_device(c, device["device_id"], device["version"], status=new.value, **fields):
            raise NexusError("INTERNAL_ERROR", message="裝置剛被別的操作改過，請重試")
        self._audit(c, action, actor, device["device_id"], "ok", reason, correlation_id, from_status=cur.value, to_status=new.value)

    # ---------- 註冊（Worker 呼叫，無憑證） ----------
    def register(self, *, pair_code: str, name: str, platform: str, agent_version: str, protocol_version: str,
                 csr_pem: str, source_ip: str, correlation_id: str = "") -> dict:
        actor = "anonymous"
        if not protocol_compatible(protocol_version) or version_tuple(protocol_version) < version_tuple(MIN_PEER_PROTOCOL):
            self._audit(self.db.conn(), "device.register_rejected", actor, "-", "denied", "protocol", correlation_id,
                        protocol_version=protocol_version, source_ip=source_ip)
            raise NexusError("PROTOCOL_INCOMPATIBLE", details={"min_protocol": MIN_PEER_PROTOCOL})
        try:
            if version_tuple(agent_version) < version_tuple(MIN_AGENT_VERSION):
                raise ValueError
        except ValueError as e:
            self._audit(self.db.conn(), "device.register_rejected", actor, "-", "denied", "agent_version", correlation_id,
                        agent_version=agent_version, source_ip=source_ip)
            raise NexusError("AGENT_VERSION_TOO_OLD", details={"min_agent_version": MIN_AGENT_VERSION}) from e
        try:
            self.pair.consume(pair_code)
        except NexusError as e:
            self._audit(self.db.conn(), "device.register_rejected", actor, "-", "denied", e.code, correlation_id, source_ip=source_ip)
            raise
        try:
            csr = certs.load_csr(csr_pem)
        except Exception as e:
            self._audit(self.db.conn(), "device.register_rejected", actor, "-", "denied", "bad_csr", correlation_id, source_ip=source_ip)
            raise NexusError("SCHEMA_INVALID", details={"field": "csr_pem"}) from e
        device_id = str(uuid.uuid4())
        token = secrets.token_urlsafe(32)
        fp = certs.public_key_fingerprint(csr.public_key())
        expires = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=REGISTRATION_TTL_SECONDS)).isoformat(timespec="seconds")
        with self.db.tx() as c:
            self.repo.insert_device(c, dict(device_id=device_id, name=name[:64], platform=platform[:32], agent_version=agent_version,
                                            protocol_version=protocol_version, status=DeviceStatus.PENDING.value,
                                            public_key_fingerprint=fp, source_ip_first=source_ip, created_at=utcnow()))
            self.repo.insert_registration(c, device_id, token, csr_pem, expires.replace("+00:00", "Z"))
            self._audit(c, "device.register_requested", actor, device_id, "ok", "", correlation_id,
                        name=name, platform=platform, agent_version=agent_version, source_ip=source_ip, fingerprint=fp)
        log.info("新裝置註冊 %s（%s，%s）等待核准", name, device_id[:8], source_ip)
        return {"device_id": device_id, "registration_token": token, "status": DeviceStatus.PENDING.value,
                "fingerprint": fp, "master_ca_fingerprint": certs.cert_fingerprint(self.ca.ca_cert)}

    def registration_status(self, device_id: str, token: str) -> dict:
        """Worker 輪詢；核准後憑證只給一次"""
        reg = self.repo.registration_by_token(device_id, token)
        dev = self.repo.get_device(device_id)
        if not reg or not dev:
            raise NexusError("REGISTRATION_NOT_FOUND")
        st = DeviceStatus(dev["status"])
        if st == DeviceStatus.PENDING:
            if parse_utc(reg["expires_at"]) < dt.datetime.now(dt.timezone.utc):
                with self.db.tx() as c:
                    self._transition(c, dev, DeviceStatus.REJECTED, "system", "device.rejected", "registration expired")
                raise NexusError("REGISTRATION_NOT_FOUND")
            return {"status": st.value}
        if st != DeviceStatus.ACTIVE:
            return {"status": st.value}
        creds = [x for x in self.repo.credentials_of(device_id) if x["status"] == CredentialStatus.ACTIVE.value]
        if not creds or reg["delivered_at"] or not reg.get("cert_pem"):
            raise NexusError("REGISTRATION_NOT_FOUND")
        with self.db.tx() as c:
            self.repo.mark_delivered(c, device_id)
            self._audit(c, "credential.delivered", f"worker:{device_id}", device_id, "ok")
        return {"status": st.value, "certificate_pem": reg["cert_pem"], "ca_pem": self.ca.ca_pem(), "expires_at": creds[-1]["expires_at"]}

    # ---------- 管理（只有本機 GUI／測試呼叫） ----------
    def approve(self, device_id: str, actor: str, reason: str) -> dict:
        if not reason or not reason.strip():
            raise NexusError("SCHEMA_INVALID", details={"field": "reason"})
        with self.lock, self.db.tx() as c:
            dev = self.repo.get_device(device_id, c)
            reg = self.repo.get_registration(device_id, c)
            if not dev or not reg:
                raise NexusError("NOT_FOUND")
            cert = self.ca.issue_device_cert(reg["csr_pem"], device_id, days=int(self.cfg["device_cert_days"]))
            cred = dict(credential_id=str(uuid.uuid4()), device_id=device_id, serial_hex=format(cert.serial_number, "x"),
                        fingerprint_sha256=certs.cert_fingerprint(cert), issued_at=utcnow(),
                        expires_at=cert.not_valid_after_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
                        status=CredentialStatus.ACTIVE.value)
            self.repo.insert_credential(c, cred)
            self._transition(c, dev, DeviceStatus.ACTIVE, actor, "device.approved", reason, approved_at=utcnow(), approved_reason=reason)
            self._audit(c, "credential.issued", actor, device_id, "ok", "", serial=cred["serial_hex"], expires_at=cred["expires_at"])
            self.repo.set_registration_cert(c, device_id, certs.pem(cert))
        log.info("核准裝置 %s：%s", device_id[:8], reason)
        return cred

    def reject(self, device_id: str, actor: str, reason: str):
        with self.lock, self.db.tx() as c:
            dev = self.repo.get_device(device_id, c)
            if not dev:
                raise NexusError("NOT_FOUND")
            self._transition(c, dev, DeviceStatus.REJECTED, actor, "device.rejected", reason)

    def suspend(self, device_id: str, actor: str, reason: str):
        with self.lock, self.db.tx() as c:
            dev = self.repo.get_device(device_id, c)
            if not dev:
                raise NexusError("NOT_FOUND")
            self._transition(c, dev, DeviceStatus.SUSPENDED, actor, "device.suspended", reason)
        self.conns.close_all(device_id)

    def resume(self, device_id: str, actor: str, reason: str):
        with self.lock, self.db.tx() as c:
            dev = self.repo.get_device(device_id, c)
            if not dev:
                raise NexusError("NOT_FOUND")
            self._transition(c, dev, DeviceStatus.ACTIVE, actor, "device.resumed", reason)

    def revoke(self, device_id: str, actor: str, reason: str) -> int:
        """撤銷：裝置 REVOKED、全部憑證 REVOKED、指紋進永久黑名單、關掉活連線。終態，永不復活。"""
        if not reason or not reason.strip():
            raise NexusError("SCHEMA_INVALID", details={"field": "reason"})
        with self.lock, self.db.tx() as c:
            dev = self.repo.get_device(device_id, c)
            if not dev:
                raise NexusError("NOT_FOUND")
            self._transition(c, dev, DeviceStatus.REVOKED, actor, "device.revoked", reason, revoked_at=utcnow(), revoked_reason=reason)
            self.repo.revoke_all_credentials(c, device_id, reason)
            c.execute("UPDATE registrations SET cert_pem=NULL WHERE device_id=?", (device_id,))
        closed = self.conns.close_all(device_id)
        self.repo.set_connection_state(device_id, ConnectionState.DISCONNECTED.value)
        log.warning("撤銷裝置 %s：%s（關閉 %d 條連線）", device_id[:8], reason, closed)
        return closed

    # ---------- 已驗證 Worker 的請求（server 在每個請求前呼叫） ----------
    def authenticate(self, peer_fingerprint: str, claimed_device_id: str | None, correlation_id: str = "") -> dict:
        """握手過了還不算：憑證要在 DB、ACTIVE／重疊期內、裝置 ACTIVE、路徑 id 要等於憑證 id"""
        if self.repo.is_fingerprint_revoked(peer_fingerprint):
            self._audit(self.db.conn(), "credential.rejected", "unknown", claimed_device_id or "-", "denied", "revoked", correlation_id)
            raise NexusError("CREDENTIAL_REVOKED")
        cred = self.repo.credential_by_fingerprint(peer_fingerprint)
        if not cred:
            self._audit(self.db.conn(), "credential.rejected", "unknown", claimed_device_id or "-", "denied", "unknown", correlation_id)
            raise NexusError("CREDENTIAL_MISMATCH")
        device_id = cred["device_id"]
        if claimed_device_id and claimed_device_id != device_id:
            self._audit(self.db.conn(), "credential.rejected", f"worker:{device_id}", claimed_device_id, "denied", "id mismatch",
                        correlation_id)
            raise NexusError("CREDENTIAL_MISMATCH")
        now = dt.datetime.now(dt.timezone.utc)
        if parse_utc(cred["expires_at"]) < now:
            self._audit(self.db.conn(), "credential.rejected", f"worker:{device_id}", device_id, "denied", "expired", correlation_id)
            raise NexusError("CREDENTIAL_EXPIRED")
        if cred["status"] == CredentialStatus.SUPERSEDED.value:
            overlap = dt.timedelta(hours=int(self.cfg["supersede_overlap_hours"]))
            if not cred["superseded_at"] or parse_utc(cred["superseded_at"]) + overlap < now:
                self._audit(self.db.conn(), "credential.rejected", f"worker:{device_id}", device_id, "denied", "superseded", correlation_id)
                raise NexusError("CREDENTIAL_EXPIRED", message="舊憑證已過重疊期，請用新憑證")
        elif cred["status"] != CredentialStatus.ACTIVE.value:
            self._audit(self.db.conn(), "credential.rejected", f"worker:{device_id}", device_id, "denied", cred["status"], correlation_id)
            raise NexusError("CREDENTIAL_REVOKED")
        dev = self.repo.get_device(device_id)
        st = DeviceStatus(dev["status"])
        if st == DeviceStatus.SUSPENDED:
            raise NexusError("DEVICE_SUSPENDED")
        if st == DeviceStatus.PENDING:
            raise NexusError("DEVICE_PENDING")
        if st != DeviceStatus.ACTIVE:
            raise NexusError("DEVICE_NOT_ACTIVE")
        return dev

    def heartbeat(self, dev: dict, sequence: int, agent_version: str, protocol_version: str, status: str) -> dict:
        if not protocol_compatible(protocol_version):
            raise NexusError("PROTOCOL_INCOMPATIBLE", details={"min_protocol": MIN_PEER_PROTOCOL})
        was = dev["connection_state"]
        self.repo.touch_seen(dev["device_id"], sequence, ConnectionState.CONNECTED.value)
        if was != ConnectionState.CONNECTED.value:
            self._audit(self.db.conn(), "device.connected", f"worker:{dev['device_id']}", dev["device_id"], "ok", "",
                        previous=was, agent_version=agent_version)
        if agent_version != dev["agent_version"] or protocol_version != dev["protocol_version"]:
            self.db.conn().execute("UPDATE devices SET agent_version=?, protocol_version=? WHERE device_id=?",
                                   (agent_version, protocol_version, dev["device_id"]))
        return {"status": dev["status"], "server_time": utcnow(), "heartbeat_seconds": 10,
                "renew_recommended": self._renew_recommended(dev["device_id"])}

    def _renew_recommended(self, device_id: str) -> bool:
        creds = [x for x in self.repo.credentials_of(device_id) if x["status"] == CredentialStatus.ACTIVE.value]
        if not creds:
            return False
        left = parse_utc(creds[-1]["expires_at"]) - dt.datetime.now(dt.timezone.utc)
        return left < dt.timedelta(days=int(self.cfg["renew_before_days"]))

    def renew(self, dev: dict, csr_pem: str, actor_fp: str) -> dict:
        """憑證更新：不需人工核准（同一身分）；舊憑證 SUPERSEDED 給重疊期"""
        device_id = dev["device_id"]
        with self.lock, self.db.tx() as c:
            cert = self.ca.issue_device_cert(csr_pem, device_id, days=int(self.cfg["device_cert_days"]))
            new = dict(credential_id=str(uuid.uuid4()), device_id=device_id, serial_hex=format(cert.serial_number, "x"),
                       fingerprint_sha256=certs.cert_fingerprint(cert), issued_at=utcnow(),
                       expires_at=cert.not_valid_after_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
                       status=CredentialStatus.ACTIVE.value)
            self.repo.insert_credential(c, new)
            for old in self.repo.credentials_of(device_id, c):
                if old["status"] == CredentialStatus.ACTIVE.value and old["credential_id"] != new["credential_id"]:
                    self.repo.set_credential_status(c, old["credential_id"], CredentialStatus.SUPERSEDED.value,
                                                    superseded_by=new["credential_id"], superseded_at=utcnow())
            self._audit(c, "credential.renewed", f"worker:{device_id}", device_id, "ok", "", serial=new["serial_hex"],
                        expires_at=new["expires_at"], old_fingerprint=actor_fp[:16])
        return {"certificate_pem": certs.pem(cert), "expires_at": new["expires_at"],
                "overlap_hours": int(self.cfg["supersede_overlap_hours"])}

    def disconnect(self, dev: dict):
        self.repo.set_connection_state(dev["device_id"], ConnectionState.DISCONNECTED.value)
        self._audit(self.db.conn(), "device.disconnected", f"worker:{dev['device_id']}", dev["device_id"], "ok")

    # ---------- 背景清查（Master 每 10 秒呼叫一次） ----------
    def sweep(self, now=None) -> list:
        """心跳逾時 → STALE／OFFLINE；憑證全部過期的 ACTIVE 裝置 → EXPIRED。回傳改了哪些"""
        now = now or dt.datetime.now(dt.timezone.utc)
        changed = []
        for dev in self.repo.list_devices():
            did = dev["device_id"]
            if dev["status"] == DeviceStatus.ACTIVE.value and dev["last_seen_at"]:
                age = (now - parse_utc(dev["last_seen_at"])).total_seconds()
                st = dev["connection_state"]
                if age > OFFLINE_AFTER_SECONDS and st not in (ConnectionState.OFFLINE.value, ConnectionState.DISCONNECTED.value):
                    self.repo.set_connection_state(did, ConnectionState.OFFLINE.value)
                    self._audit(self.db.conn(), "device.offline", "system", did, "ok", f"{int(age)}s")
                    changed.append((did, "OFFLINE"))
                elif STALE_AFTER_SECONDS < age <= OFFLINE_AFTER_SECONDS and st == ConnectionState.CONNECTED.value:
                    self.repo.set_connection_state(did, ConnectionState.STALE.value)
                    self._audit(self.db.conn(), "device.heartbeat_stale", "system", did, "ok", f"{int(age)}s")
                    changed.append((did, "STALE"))
            if dev["status"] == DeviceStatus.ACTIVE.value:
                creds = self.repo.credentials_of(did)
                live = [x for x in creds if x["status"] in (CredentialStatus.ACTIVE.value, CredentialStatus.SUPERSEDED.value)
                        and parse_utc(x["expires_at"]) >= now]
                if creds and not live:
                    with self.lock, self.db.tx() as c:
                        d2 = self.repo.get_device(did, c)
                        self._transition(c, d2, DeviceStatus.EXPIRED, "system", "device.expired", "all credentials expired")
                        for x in creds:
                            self.repo.set_credential_status(c, x["credential_id"], CredentialStatus.EXPIRED.value)
                    self.conns.close_all(did)
                    changed.append((did, "EXPIRED"))
        return changed

    # ---------- 啟動自檢 ----------
    def selfcheck(self) -> dict:
        problems = []
        try:
            if certs.days_left(self.ca.ca_cert) <= 0:
                problems.append("CA 憑證已過期")
            if certs.days_left(self.ca.server_cert) <= 0:
                problems.append("伺服器憑證已過期")
            self.ca.ks.load_private_key("ca")
        except Exception as e:
            problems.append(f"金鑰或憑證載入失敗：{type(e).__name__}")
        try:
            self.db.conn().execute("SELECT 1 FROM audit_events LIMIT 1")
        except Exception as e:
            problems.append(f"資料庫：{type(e).__name__}")
        return {"ok": not problems, "problems": problems}

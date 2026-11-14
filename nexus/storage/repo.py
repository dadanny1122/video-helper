"""資料存取（只走參數化 SQL）。狀態轉移的「合法性」在 master/registry 檢查；這裡只負責讀寫與樂觀鎖。"""
import hashlib
import json
import time

from nexus.core.masking import mask_dict
from nexus.core.models import AuditEvent, utcnow


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


class Repo:
    def __init__(self, db):
        self.db = db

    # ---- devices ----
    def insert_device(self, c, d: dict):
        cols = ",".join(d)
        c.execute(f"INSERT INTO devices({cols}) VALUES ({','.join('?' * len(d))})", tuple(d.values()))

    def get_device(self, device_id: str, c=None):
        c = c or self.db.conn()
        r = c.execute("SELECT * FROM devices WHERE device_id=?", (device_id,)).fetchone()
        return dict(r) if r else None

    def list_devices(self, status=None):
        c = self.db.conn()
        if status:
            rows = c.execute("SELECT * FROM devices WHERE status=? ORDER BY created_at", (status,)).fetchall()
        else:
            rows = c.execute("SELECT * FROM devices ORDER BY created_at").fetchall()
        return [dict(r) for r in rows]

    def update_device(self, c, device_id: str, expected_version: int, **fields) -> bool:
        """樂觀鎖：version 不符就回 False（別人先改了）"""
        fields["version"] = expected_version + 1
        sets = ",".join(f"{k}=?" for k in fields)
        cur = c.execute(f"UPDATE devices SET {sets} WHERE device_id=? AND version=?",
                        (*fields.values(), device_id, expected_version))
        return cur.rowcount == 1

    def touch_seen(self, device_id: str, sequence: int, state: str):
        """心跳：只更新最後看到的時間／序號／連線狀態，不動 version（避免跟管理操作搶鎖）"""
        self.db.conn().execute(
            "UPDATE devices SET last_seen_at=?, last_sequence=?, connection_state=? WHERE device_id=?",
            (utcnow(), sequence, state, device_id))

    def set_connection_state(self, device_id: str, state: str):
        self.db.conn().execute("UPDATE devices SET connection_state=? WHERE device_id=?", (state, device_id))

    # ---- credentials ----
    def insert_credential(self, c, cred: dict):
        cols = ",".join(cred)
        c.execute(f"INSERT INTO credentials({cols}) VALUES ({','.join('?' * len(cred))})", tuple(cred.values()))

    def credential_by_fingerprint(self, fp: str, c=None):
        c = c or self.db.conn()
        r = c.execute("SELECT * FROM credentials WHERE fingerprint_sha256=?", (fp,)).fetchone()
        return dict(r) if r else None

    def credentials_of(self, device_id: str, c=None):
        c = c or self.db.conn()
        return [dict(r) for r in c.execute("SELECT * FROM credentials WHERE device_id=? ORDER BY issued_at", (device_id,))]

    def set_credential_status(self, c, credential_id: str, status: str, **extra):
        sets = "status=?" + "".join(f",{k}=?" for k in extra)
        c.execute(f"UPDATE credentials SET {sets} WHERE credential_id=?", (status, *extra.values(), credential_id))

    def revoke_all_credentials(self, c, device_id: str, reason: str):
        now = utcnow()
        for cred in self.credentials_of(device_id, c):
            c.execute("UPDATE credentials SET status='REVOKED', revoked_at=? WHERE credential_id=?", (now, cred["credential_id"]))
            c.execute("INSERT OR IGNORE INTO revoked_fingerprints(fingerprint_sha256, device_id, revoked_at, reason) VALUES (?,?,?,?)",
                      (cred["fingerprint_sha256"], device_id, now, reason))

    def is_fingerprint_revoked(self, fp: str) -> bool:
        return self.db.conn().execute("SELECT 1 FROM revoked_fingerprints WHERE fingerprint_sha256=?", (fp,)).fetchone() is not None

    # ---- registrations ----
    def insert_registration(self, c, device_id: str, token: str, csr_pem: str, expires_at: str):
        c.execute("INSERT INTO registrations(device_id, registration_token_hash, csr_pem, created_at, expires_at) VALUES (?,?,?,?,?)",
                  (device_id, sha256_hex(token), csr_pem, utcnow(), expires_at))

    def registration_by_token(self, device_id: str, token: str):
        r = self.db.conn().execute("SELECT * FROM registrations WHERE device_id=? AND registration_token_hash=?",
                                   (device_id, sha256_hex(token))).fetchone()
        return dict(r) if r else None

    def get_registration(self, device_id: str, c=None):
        c = c or self.db.conn()
        r = c.execute("SELECT * FROM registrations WHERE device_id=?", (device_id,)).fetchone()
        return dict(r) if r else None

    def mark_delivered(self, c, device_id: str):
        c.execute("UPDATE registrations SET delivered_at=?, registration_token_hash=? WHERE device_id=?",
                  (utcnow(), "delivered:" + sha256_hex(device_id + utcnow()), device_id))

    # ---- nonces ----
    def nonce_seen(self, sender_id: str, nonce: str, ttl: int) -> bool:
        """回 True＝已經用過（重放）；順便清過期"""
        c = self.db.conn()
        now = time.time()
        if int(now) % 60 == 0:
            c.execute("DELETE FROM nonces WHERE seen_at < ?", (now - ttl,))
        r = c.execute("SELECT seen_at FROM nonces WHERE sender_id=? AND nonce=?", (sender_id, nonce)).fetchone()
        if r and r[0] > now - ttl:
            return True
        c.execute("INSERT OR REPLACE INTO nonces(sender_id, nonce, seen_at) VALUES (?,?,?)", (sender_id, nonce, now))
        return False

    # ---- audit ----
    def audit(self, ev: AuditEvent, c=None):
        c = c or self.db.conn()
        c.execute("INSERT INTO audit_events(ts, actor, action, resource_type, resource_id, outcome, reason, correlation_id, details_json)"
                  " VALUES (?,?,?,?,?,?,?,?,?)",
                  (ev.ts, ev.actor, ev.action, ev.resource_type, ev.resource_id, ev.outcome, ev.reason, ev.correlation_id,
                   json.dumps(mask_dict(ev.details), ensure_ascii=False)))

    def audit_tail(self, limit=200, resource_id=None):
        c = self.db.conn()
        if resource_id:
            rows = c.execute("SELECT * FROM audit_events WHERE resource_id=? ORDER BY audit_id DESC LIMIT ?", (resource_id, limit))
        else:
            rows = c.execute("SELECT * FROM audit_events ORDER BY audit_id DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

"""
每個版本一段 SQL；用 PRAGMA user_version 記目前到哪。
升級規則（AT-019）：先備份 → 整個升級包在一個交易 → 任一步失敗就 rollback 並把備份放回去。
"""
MIGRATIONS = {
    1: """
    CREATE TABLE devices (
        device_id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        platform TEXT NOT NULL,
        agent_version TEXT NOT NULL,
        protocol_version TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('PENDING','ACTIVE','SUSPENDED','REVOKED','EXPIRED','REJECTED')),
        public_key_fingerprint TEXT NOT NULL,
        source_ip_first TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        approved_at TEXT, approved_reason TEXT,
        revoked_at TEXT, revoked_reason TEXT,
        last_seen_at TEXT,
        connection_state TEXT NOT NULL DEFAULT 'UNKNOWN',
        last_sequence INTEGER NOT NULL DEFAULT 0,
        version INTEGER NOT NULL DEFAULT 1
    );
    CREATE TABLE credentials (
        credential_id TEXT PRIMARY KEY,
        device_id TEXT NOT NULL REFERENCES devices(device_id),
        serial_hex TEXT NOT NULL UNIQUE,
        fingerprint_sha256 TEXT NOT NULL UNIQUE,
        issued_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('ACTIVE','SUPERSEDED','REVOKED','EXPIRED')),
        superseded_by TEXT,
        superseded_at TEXT,
        revoked_at TEXT
    );
    CREATE INDEX idx_credentials_device ON credentials(device_id);
    CREATE TABLE revoked_fingerprints (
        fingerprint_sha256 TEXT PRIMARY KEY,
        device_id TEXT NOT NULL,
        revoked_at TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT ''
    );
    CREATE TABLE registrations (
        device_id TEXT PRIMARY KEY REFERENCES devices(device_id),
        registration_token_hash TEXT NOT NULL UNIQUE,
        csr_pem TEXT NOT NULL,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        delivered_at TEXT
    );
    CREATE TABLE nonces (
        sender_id TEXT NOT NULL,
        nonce TEXT NOT NULL,
        seen_at REAL NOT NULL,
        PRIMARY KEY (sender_id, nonce)
    );
    CREATE TABLE audit_events (
        audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
        ts TEXT NOT NULL,
        actor TEXT NOT NULL,
        action TEXT NOT NULL,
        resource_type TEXT NOT NULL,
        resource_id TEXT NOT NULL,
        outcome TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        correlation_id TEXT NOT NULL DEFAULT '',
        details_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX idx_audit_ts ON audit_events(ts);
    CREATE INDEX idx_audit_resource ON audit_events(resource_type, resource_id);
    -- 稽核只能新增：資料庫層就擋掉改與刪
    CREATE TRIGGER audit_no_update BEFORE UPDATE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
    CREATE TRIGGER audit_no_delete BEFORE DELETE ON audit_events
        BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END;
    CREATE TABLE schema_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
    """,
}
LATEST = max(MIGRATIONS)

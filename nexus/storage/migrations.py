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
        cert_pem TEXT,                      -- 核准後簽好的憑證（公開資料），交付一次就清掉
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
    # V2：任務、嘗試、事件、排程決策、節點能力（只新增表，不改 V1 的表）
    2: """
    CREATE TABLE tasks (
        task_id TEXT PRIMARY KEY,
        type TEXT NOT NULL,
        adapter_version TEXT NOT NULL,
        manifest_json TEXT NOT NULL,
        manifest_sha256 TEXT NOT NULL,
        idempotency_key TEXT NOT NULL UNIQUE,
        status TEXT NOT NULL,
        version INTEGER NOT NULL DEFAULT 1,
        priority INTEGER NOT NULL DEFAULT 0,
        attempt_count INTEGER NOT NULL DEFAULT 0,
        max_attempts INTEGER NOT NULL DEFAULT 1,
        assigned_device_id TEXT,
        current_attempt_id TEXT,
        result_json TEXT,
        error_code TEXT,
        error_message TEXT,
        created_by TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        expires_at TEXT NOT NULL
    );
    CREATE INDEX idx_tasks_status ON tasks(status, priority, created_at);
    CREATE INDEX idx_tasks_device ON tasks(assigned_device_id, status);
    CREATE TABLE task_attempts (
        attempt_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        attempt_no INTEGER NOT NULL,
        device_id TEXT NOT NULL,
        lease_id TEXT NOT NULL UNIQUE,
        lease_expires_at TEXT NOT NULL,
        status TEXT NOT NULL,
        offered_at TEXT NOT NULL,
        accepted_at TEXT,
        started_at TEXT,
        ended_at TEXT,
        last_seq INTEGER NOT NULL DEFAULT 0,
        error_code TEXT,
        UNIQUE(task_id, attempt_no)
    );
    CREATE INDEX idx_attempts_device ON task_attempts(device_id, status);
    CREATE TABLE task_events (
        event_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        attempt_id TEXT,
        device_id TEXT,
        seq INTEGER NOT NULL,
        type TEXT NOT NULL,
        data_json TEXT NOT NULL DEFAULT '{}',
        received_at TEXT NOT NULL
    );
    CREATE UNIQUE INDEX idx_events_attempt_seq ON task_events(attempt_id, seq) WHERE attempt_id IS NOT NULL;
    CREATE INDEX idx_events_task ON task_events(task_id, received_at);
    CREATE TABLE schedule_decisions (
        decision_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL,
        decided_at TEXT NOT NULL,
        weights_version TEXT NOT NULL,
        candidates_json TEXT NOT NULL,
        chosen_device_id TEXT
    );
    CREATE INDEX idx_decisions_task ON schedule_decisions(task_id, decided_at);
    CREATE TABLE device_capabilities (
        device_id TEXT PRIMARY KEY,
        resources_json TEXT NOT NULL,
        adapters_json TEXT NOT NULL,
        max_concurrency INTEGER NOT NULL DEFAULT 1,
        accepting INTEGER NOT NULL DEFAULT 1,
        observed_at TEXT NOT NULL
    );
    """,
    # V4：節點把影片傳回 Master（10-V4 §6）；一個檔案一筆，收到哪幾段記在 have（位元圖）
    3: """
    CREATE TABLE uploads (
        upload_id TEXT PRIMARY KEY,
        task_id TEXT NOT NULL REFERENCES tasks(task_id),
        file_index INTEGER NOT NULL,
        device_id TEXT NOT NULL,
        name TEXT NOT NULL,
        size INTEGER NOT NULL,
        sha256 TEXT NOT NULL,
        node_path TEXT NOT NULL DEFAULT '',
        chunk_size INTEGER NOT NULL,
        chunks_total INTEGER NOT NULL,
        have BLOB NOT NULL,
        status TEXT NOT NULL,
        hash_failures INTEGER NOT NULL DEFAULT 0,
        master_path TEXT NOT NULL DEFAULT '',
        note TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE(task_id, file_index)
    );
    CREATE INDEX idx_uploads_task ON uploads(task_id);
    """,
}
LATEST = max(MIGRATIONS)

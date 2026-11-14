import os
import sqlite3

import pytest

from nexus.core.models import AuditEvent
from nexus.storage import migrations
from nexus.storage.db import Database, MigrationError
from nexus.storage.repo import Repo


def fresh(tmp_path):
    db = Database(str(tmp_path / "nexus.db"))
    db.migrate()
    return db, Repo(db)


def test_migrate_fresh_and_idempotent(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    r = db.migrate()
    assert r["from"] == 0 and r["to"] == migrations.LATEST and r["backup"] is None
    assert db.user_version() == migrations.LATEST
    assert db.migrate()["backup"] is None                    # 再跑一次不做事
    tables = {x[0] for x in db.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"devices", "credentials", "revoked_fingerprints", "registrations", "nonces", "audit_events", "schema_meta"} <= tables


def test_audit_is_append_only(tmp_path):
    db, repo = fresh(tmp_path)
    repo.audit(AuditEvent("device.approved", "owner", "device", "d1", "ok", reason="test", details={"token": "secret123"}))
    row = repo.audit_tail(1)[0]
    assert "secret123" not in row["details_json"]           # 寫進稽核前已遮罩
    with pytest.raises(sqlite3.DatabaseError):
        db.conn().execute("UPDATE audit_events SET outcome='x' WHERE audit_id=?", (row["audit_id"],))
    with pytest.raises(sqlite3.DatabaseError):
        db.conn().execute("DELETE FROM audit_events WHERE audit_id=?", (row["audit_id"],))


def test_migration_failure_keeps_original_and_backup(tmp_path, monkeypatch):
    """AT-019：升級到壞掉的新版 → 原資料不變、有備份、丟 MigrationError"""
    db, repo = fresh(tmp_path)
    with db.tx() as c:
        repo.insert_device(c, dict(device_id="d1", name="pc", platform="win", agent_version="0.1.0", protocol_version="1.0",
                                   status="PENDING", public_key_fingerprint="fp1", created_at="2026-01-01T00:00:00Z"))
    db.close()
    bad = dict(migrations.MIGRATIONS)
    bad[migrations.LATEST + 1] = "ALTER TABLE devices ADD COLUMN extra TEXT; CREATE TABLE devices (x);"   # 第二句一定失敗
    monkeypatch.setattr(migrations, "MIGRATIONS", bad)
    monkeypatch.setattr(migrations, "LATEST", migrations.LATEST + 1)
    import nexus.storage.db as dbmod
    monkeypatch.setattr(dbmod, "MIGRATIONS", bad)
    monkeypatch.setattr(dbmod, "LATEST", migrations.LATEST)
    db2 = Database(db.path)
    with pytest.raises(MigrationError) as ei:
        db2.migrate()
    assert ei.value.backup_path and os.path.isfile(ei.value.backup_path)
    db3 = Database(db.path)
    assert db3.user_version() == 1
    cols = [r[1] for r in db3.conn().execute("PRAGMA table_info(devices)")]
    assert "extra" not in cols                               # 半套升級沒留下來
    assert Repo(db3).get_device("d1")["name"] == "pc"


def test_newer_db_than_program_is_refused(tmp_path):
    db, _ = fresh(tmp_path)
    db.conn().execute("PRAGMA user_version=99")
    db.close()
    with pytest.raises(MigrationError):
        Database(db.path).migrate()


def test_optimistic_lock_and_unique_constraints(tmp_path):
    db, repo = fresh(tmp_path)
    with db.tx() as c:
        repo.insert_device(c, dict(device_id="d1", name="pc", platform="win", agent_version="0.1.0", protocol_version="1.0",
                                   status="PENDING", public_key_fingerprint="fp1", created_at="2026-01-01T00:00:00Z"))
    with db.tx() as c:
        assert repo.update_device(c, "d1", 1, status="ACTIVE")
        assert not repo.update_device(c, "d1", 1, status="SUSPENDED")     # 舊版本號 → 失敗
    assert repo.get_device("d1")["status"] == "ACTIVE" and repo.get_device("d1")["version"] == 2
    cred = dict(credential_id="c1", device_id="d1", serial_hex="01", fingerprint_sha256="f1",
                issued_at="2026-01-01T00:00:00Z", expires_at="2026-04-01T00:00:00Z", status="ACTIVE")
    with db.tx() as c:
        repo.insert_credential(c, cred)
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        repo.insert_credential(c, dict(cred, credential_id="c2"))        # 指紋 UNIQUE
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        repo.insert_credential(c, dict(cred, credential_id="c3", serial_hex="02", fingerprint_sha256="f2", device_id="nope"))  # 外鍵


def test_revoke_all_keeps_fingerprints_forever(tmp_path):
    db, repo = fresh(tmp_path)
    with db.tx() as c:
        repo.insert_device(c, dict(device_id="d1", name="pc", platform="win", agent_version="0.1.0", protocol_version="1.0",
                                   status="ACTIVE", public_key_fingerprint="fp1", created_at="2026-01-01T00:00:00Z"))
        repo.insert_credential(c, dict(credential_id="c1", device_id="d1", serial_hex="01", fingerprint_sha256="f1",
                                       issued_at="2026-01-01T00:00:00Z", expires_at="2026-04-01T00:00:00Z", status="ACTIVE"))
        repo.revoke_all_credentials(c, "d1", "test")
    assert repo.credential_by_fingerprint("f1")["status"] == "REVOKED"
    assert repo.is_fingerprint_revoked("f1") and not repo.is_fingerprint_revoked("f2")


def test_nonce_store(tmp_path):
    db, repo = fresh(tmp_path)
    assert not repo.nonce_seen("w1", "n1", 600)
    assert repo.nonce_seen("w1", "n1", 600)
    assert not repo.nonce_seen("w2", "n1", 600)


def test_each_thread_gets_own_connection(tmp_path):
    import threading
    db, repo = fresh(tmp_path)
    ids = set()

    def work():
        ids.add(id(db.conn()))
        repo.audit(AuditEvent("x", "system", "t", "1", "ok"))
    ts = [threading.Thread(target=work) for _ in range(4)]
    [t.start() for t in ts]
    [t.join() for t in ts]
    assert len(ids) == 4 and len(repo.audit_tail(10)) == 4

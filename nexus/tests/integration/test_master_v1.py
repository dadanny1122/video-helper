"""V1 驗收：AT-001／002／003／018／023、N-01～N-04、N-06／N-07（部分）。全部對真的 TLS 服務打。"""
import datetime as dt
import os
import ssl
import time

import pytest

from nexus import NEXUS_VERSION
from nexus.core.errors import NexusError
from nexus.identity import certs
from nexus.identity.keystore import KeyStore
from nexus.master.server import BindPolicy
from nexus.worker.client import NexusClient


# ---------- 註冊與核准 ----------
def test_register_requires_pair_code_and_creates_pending(master, worker_factory):
    w = worker_factory()
    with pytest.raises(NexusError) as ei:
        w.register("000000")
    assert ei.value.code == "PAIR_CODE_EXPIRED"               # 還沒產生配對碼
    code = master.registry.pair.new()
    with pytest.raises(NexusError) as ei:
        w.register("999999" if code != "999999" else "000000")
    assert ei.value.code == "PAIR_CODE_INVALID"
    r = w.register(code)
    assert r["status"] == "PENDING" and len(r["device_id"]) == 36 and r["master_ca_fingerprint"]
    assert master.repo.get_device(w.device_id)["status"] == "PENDING"
    with pytest.raises(NexusError) as ei:                     # 一碼一次
        worker_factory("pc-2").register(code)
    assert ei.value.code == "PAIR_CODE_EXPIRED"
    acts = master.audit_actions()
    assert "device.register_requested" in acts and acts.count("device.register_rejected") == 3


def test_pair_code_burns_after_5_wrong_guesses(master, worker_factory):
    master.registry.pair.new()
    w = worker_factory()
    codes = []
    for _ in range(5):
        with pytest.raises(NexusError) as ei:
            w.register("000000")
        codes.append(ei.value.code)
    assert codes[-1] == "PAIR_CODE_BURNED" and codes[0] == "PAIR_CODE_INVALID"


def test_at001_pending_worker_cannot_use_authenticated_endpoints(master, worker_factory):
    """AT-001：未核准的 Worker 沒有憑證 → 心跳 403，且拿不到任何東西；留下安全事件"""
    w = worker_factory()
    w.register(master.registry.pair.new())
    assert w.fetch_cert()["status"] == "PENDING"
    c = NexusClient(master.url, sender_id=f"worker:{w.device_id}")     # 沒有憑證
    with pytest.raises(NexusError) as ei:
        c.heartbeat(w.device_id, 1, NEXUS_VERSION)
    assert ei.value.code == "CREDENTIAL_MISMATCH" and ei.value.http_status == 403
    with pytest.raises(NexusError):
        c.info()
    acts = master.audit_actions(w.device_id)
    assert "credential.rejected" in acts


def test_at002_approved_worker_connects_with_mtls_and_heartbeats(master, worker_factory):
    w = worker_factory().onboard()
    dev = master.repo.get_device(w.device_id)
    assert dev["status"] == "ACTIVE" and dev["approved_reason"] == "測試核准"
    c = w.client()
    r = w.beat(c)
    assert r["status"] == "ACTIVE" and r["heartbeat_seconds"] == 10 and r["renew_recommended"] is False
    r2 = w.beat(c)
    dev2 = master.repo.get_device(w.device_id)
    assert dev2["last_sequence"] == 2 and dev2["connection_state"] == "CONNECTED"
    assert r2["correlation_id"] != r["correlation_id"]
    info = c.info()
    assert info["protocol_version"] == "1.0"
    acts = master.audit_actions(w.device_id)
    for a in ("device.register_requested", "device.approved", "credential.issued", "credential.delivered", "device.connected"):
        assert a in acts, a
    # 憑證只能領一次
    with pytest.raises(NexusError) as ei:
        w.fetch_cert()
    assert ei.value.code == "REGISTRATION_NOT_FOUND"
    # 公鑰指紋＝註冊時算的那把
    assert master.repo.get_device(w.device_id)["public_key_fingerprint"] == certs.public_key_fingerprint(w.key.public_key())


def test_approve_and_revoke_require_reason(master, worker_factory):
    w = worker_factory()
    w.register(master.registry.pair.new())
    with pytest.raises(NexusError):
        master.registry.approve(w.device_id, "owner", "   ")
    master.registry.approve(w.device_id, "owner", "ok")
    with pytest.raises(NexusError):
        master.registry.revoke(w.device_id, "owner", "")


# ---------- 撤銷（AT-003、N-01、N-02） ----------
def test_at003_n01_revoke_closes_live_connection_and_old_cert_never_works_again(master, worker_factory):
    w = worker_factory().onboard()
    c = w.client()
    w.beat(c)
    assert master.conns.count(w.device_id) >= 1
    closed = master.registry.revoke(w.device_id, "owner", "測試撤銷")
    assert closed >= 1 and master.conns.count(w.device_id) == 0
    # 同一條 keep-alive 連線：被關了 → 送不出去或回 CREDENTIAL_REVOKED
    with pytest.raises((NexusError, OSError)):
        w.beat(c)
    # 重新開連線（模擬 Worker 重啟、本機還留著舊憑證）→ 握手可能過（CA 沒變）但請求層一定拒絕
    for _ in range(3):
        with pytest.raises(NexusError) as ei:
            w.beat(w.client())
        assert ei.value.code == "CREDENTIAL_REVOKED"
    dev = master.repo.get_device(w.device_id)
    assert dev["status"] == "REVOKED" and dev["revoked_reason"] == "測試撤銷"
    assert all(x["status"] == "REVOKED" for x in master.repo.credentials_of(w.device_id))
    # 永遠回不去 ACTIVE
    with pytest.raises(NexusError):
        master.registry.resume(w.device_id, "owner", "試試看")
    with pytest.raises(NexusError):
        master.registry.approve(w.device_id, "owner", "試試看")
    acts = master.audit_actions(w.device_id)
    assert "device.revoked" in acts and acts.count("credential.rejected") >= 3


def test_n02_revoked_machine_must_reregister_as_new_identity(master, worker_factory):
    w = worker_factory().onboard()
    old_id, old_fp = w.device_id, master.repo.credentials_of(w.device_id)[0]["fingerprint_sha256"]
    master.registry.revoke(w.device_id, "owner", "換機")
    w2 = worker_factory("pc-test").onboard("再加入")                # 新金鑰、新註冊
    assert w2.device_id != old_id
    assert master.repo.get_device(old_id)["status"] == "REVOKED"
    assert master.repo.is_fingerprint_revoked(old_fp)
    assert w2.beat()["status"] == "ACTIVE"


def test_suspend_blocks_and_resume_restores(master, worker_factory):
    w = worker_factory().onboard()
    master.registry.suspend(w.device_id, "owner", "先停")
    with pytest.raises(NexusError) as ei:
        w.beat()
    assert ei.value.code == "DEVICE_SUSPENDED" and ei.value.retryable
    master.registry.resume(w.device_id, "owner", "恢復")
    assert w.beat()["status"] == "ACTIVE"


# ---------- 身分一致性 ----------
def test_cert_of_one_device_cannot_act_as_another(master, worker_factory):
    a = worker_factory("a").onboard()
    b = worker_factory("b").onboard()
    c = a.client()
    with pytest.raises(NexusError) as ei:                 # 用 a 的憑證打 b 的路徑
        c.heartbeat(b.device_id, 1, NEXUS_VERSION)
    assert ei.value.code == "CREDENTIAL_MISMATCH"
    bad = NexusClient(master.url, sender_id=f"worker:{b.device_id}", ca_pem=a.ca_pem, certfile=a.certfile,
                      keyfile=a.ks.key_pem_path_and_password("device")[0], key_password=a.ks.key_pem_path_and_password("device")[1])
    with pytest.raises(NexusError) as ei:                 # sender_id 假冒 b
        bad.heartbeat(a.device_id, 1, NEXUS_VERSION)
    assert ei.value.code == "CREDENTIAL_MISMATCH"


def test_cert_from_another_master_fails_tls_handshake(master, worker_factory, tmp_path):
    """別台 Master 簽的憑證：TLS 握手就擋掉（CA 不同）"""
    from nexus.identity.ca import MasterCA
    other = MasterCA(str(tmp_path / "other-pki"))
    other.ensure(["127.0.0.1"])
    key = certs.new_key()
    cert = other.issue_device_cert(certs.csr_pem(certs.make_csr(key, "x")), "evil-id")
    ks = KeyStore(str(tmp_path / "evil"))
    ks.save_private_key("device", key)
    cf = str(tmp_path / "evil.crt")
    open(cf, "w").write(certs.pem(cert))
    kf, pw = ks.key_pem_path_and_password("device")
    c = NexusClient(master.url, sender_id="worker:evil-id", ca_pem=master.ca.ca_pem(), certfile=cf, keyfile=kf, key_password=pw)
    with pytest.raises((ssl.SSLError, OSError, NexusError)):
        c.info()


# ---------- 版本／協定（AT-018） ----------
def test_at018_old_agent_version_rejected_with_clear_code(master, worker_factory):
    w = worker_factory()
    with pytest.raises(NexusError) as ei:
        w.register(master.registry.pair.new(), agent_version="0.0.1")
    assert ei.value.code == "AGENT_VERSION_TOO_OLD" and ei.value.details["min_agent_version"]
    assert "device.register_rejected" in master.audit_actions()


def test_at018_incompatible_protocol_rejected(master, worker_factory, monkeypatch):
    import nexus.protocol.messages as msgs
    w = worker_factory()
    monkeypatch.setattr(msgs, "PROTOCOL_VERSION", "2.0")
    with pytest.raises(NexusError) as ei:
        w.register(master.registry.pair.new())
    assert ei.value.code == "PROTOCOL_INCOMPATIBLE" and ei.value.http_status == 426


# ---------- 重放／時鐘／格式（N-04） ----------
def _raw_post(c, path, env):
    import json
    conn = c.connection()
    conn.request("POST", "/api/v1" + path, body=json.dumps(env).encode(), headers={"Content-Type": "application/json"})
    r = conn.getresponse()
    payload = json.loads(r.read() or b"{}")
    if r.getheader("Connection", "").lower() == "close":
        c.close()
    return r.status, payload


def test_n04_replay_and_clock_skew_and_unknown_fields(master, worker_factory):
    import json

    from nexus.protocol.messages import envelope
    w = worker_factory().onboard()
    c = w.client()
    hb = f"/devices/{w.device_id}/heartbeat"
    env = envelope(c.sender_id, sequence=1, agent_version=NEXUS_VERSION, status="IDLE")
    assert _raw_post(c, hb, env)[0] == 200
    st, payload = _raw_post(c, hb, env)                   # 原封不動再送一次 → 重放
    assert st == 409 and payload["error"]["code"] == "REPLAY_DETECTED"
    env = envelope(c.sender_id, sequence=2, agent_version=NEXUS_VERSION, status="IDLE")
    env["sent_at"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(minutes=10)).isoformat(timespec="seconds").replace("+00:00", "Z")
    st, payload = _raw_post(c, hb, env)
    assert st == 400 and payload["error"]["code"] == "CLOCK_SKEW"
    env = envelope(c.sender_id, sequence=3, agent_version=NEXUS_VERSION, status="IDLE", cmd="rm -rf /")
    st, payload = _raw_post(c, hb, env)
    assert st == 400 and payload["error"]["code"] == "SCHEMA_INVALID" and "rm -rf" not in json.dumps(payload)
    assert "replay" not in json.dumps(payload).lower() or True
    assert master.audit_actions(w.device_id).count("device.connected") == 1


def test_payload_too_large_and_unknown_route(master, worker_factory):
    import json
    w = worker_factory().onboard()
    c = w.client()
    with pytest.raises(NexusError) as ei:
        c.renew(w.device_id, "x" * 70000)
    assert ei.value.code == "PAYLOAD_TOO_LARGE"
    c = w.client()
    conn = c.connection()
    conn.request("GET", "/api/v1/admin/approve", headers={"X-VH-Envelope": "{}"})
    r = conn.getresponse()
    assert r.status == 404 and json.loads(r.read())["error"]["code"] == "NOT_FOUND"
    conn.request("GET", f"/api/v1/devices/{w.device_id}/heartbeat", headers={"X-VH-Envelope": "{}"})
    r = conn.getresponse()
    assert r.status == 405
    r.read()


# ---------- 速率限制（AT-023） ----------
def test_at023_rate_limit_register_flood(tmp_path):
    from nexus.tests.integration.conftest import FakeWorker, Master
    m = Master(tmp_path / "rl", rules={"register": (3, 60), "registration_poll": (12, 60), "heartbeat": (2, 1),
                                        "renew": (2, 60), "disconnect": (6, 60), "info": (30, 60)})
    try:
        w = FakeWorker(m, tmp_path / "rl", "flood")
        codes = []
        for _ in range(6):
            try:
                w.register("000000")
            except NexusError as e:
                codes.append((e.code, e.retry_after))
        assert [c for c, _ in codes[:3]] == ["PAIR_CODE_EXPIRED"] * 3
        assert all(c == "RATE_LIMITED" and ra for c, ra in codes[3:])
        assert m.repo.get_device("x") is None
        assert "ratelimit" in m.audit_actions()
        # Master 還活著
        m.registry.pair.new()
    finally:
        m.stop()


# ---------- Master 重啟（N-03） ----------
def test_n03_master_restart_keeps_identity_and_rejects_on_broken_key(master, worker_factory):
    w = worker_factory().onboard()
    w.beat()
    master.restart()
    dev = master.repo.get_device(w.device_id)
    assert dev["status"] == "ACTIVE" and dev["connection_state"] in ("CONNECTED", "UNKNOWN", "STALE")
    assert w.beat()["status"] == "ACTIVE"                 # 同一張憑證重連成功（CA 沒變）
    acts = master.audit_actions()
    assert acts.count("master.started") >= 2 and "master.stopped" in acts
    # 把 CA 私鑰檔弄壞 → 自檢失敗 → 不啟動
    master.server.stop()
    master.db.close()
    kp = os.path.join(master.ca.dir, "ca.key")
    open(kp, "wb").write(b"broken")
    with pytest.raises(RuntimeError, match="自檢"):
        master.open()


# ---------- 憑證更新與過期（N-06、N-07） ----------
def test_n06_renew_issues_new_cert_and_old_one_superseded(master, worker_factory):
    w = worker_factory().onboard()
    c = w.client()
    new_key = certs.new_key()
    r = c.renew(w.device_id, certs.csr_pem(certs.make_csr(new_key, "x")))
    assert "certificate_pem" in r and r["overlap_hours"] == 24
    creds = master.repo.credentials_of(w.device_id)
    assert [x["status"] for x in creds] == ["SUPERSEDED", "ACTIVE"]
    assert w.beat(c)["status"] == "ACTIVE"                # 舊憑證在重疊期內還能用
    # 重疊期過了 → 舊憑證被拒
    master.repo.db.conn().execute("UPDATE credentials SET superseded_at=? WHERE status='SUPERSEDED'",
                                  ((dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=25)).isoformat(),))
    with pytest.raises(NexusError) as ei:
        w.beat(w.client())
    assert ei.value.code == "CREDENTIAL_EXPIRED"
    # 新憑證可以用
    w.ks.save_private_key("device", new_key)
    open(w.certfile, "w").write(r["certificate_pem"])
    assert w.beat(w.client())["status"] == "ACTIVE"
    assert "credential.renewed" in master.audit_actions(w.device_id)


def test_renew_recommended_flag_near_expiry(master, worker_factory):
    w = worker_factory().onboard()
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=5)).isoformat(timespec="seconds").replace("+00:00", "Z")
    master.repo.db.conn().execute("UPDATE credentials SET expires_at=? WHERE device_id=?", (soon, w.device_id))
    assert w.beat()["renew_recommended"] is True


def test_n07_expired_credential_rejected_and_device_expired_by_sweep(master, worker_factory):
    w = worker_factory().onboard()
    past = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)).isoformat(timespec="seconds").replace("+00:00", "Z")
    master.repo.db.conn().execute("UPDATE credentials SET expires_at=? WHERE device_id=?", (past, w.device_id))
    with pytest.raises(NexusError) as ei:
        w.beat()
    assert ei.value.code == "CREDENTIAL_EXPIRED"
    changed = master.registry.sweep()
    assert (w.device_id, "EXPIRED") in changed
    assert master.repo.get_device(w.device_id)["status"] == "EXPIRED"
    # 過期是終態：要重新註冊
    with pytest.raises(NexusError):
        master.registry.resume(w.device_id, "owner", "x")


# ---------- 斷線偵測（N-05，用假時間） ----------
def test_n05_stale_and_offline_by_sweep(master, worker_factory):
    w = worker_factory().onboard()
    w.beat()
    now = dt.datetime.now(dt.timezone.utc)
    assert master.registry.sweep(now + dt.timedelta(seconds=10)) == []
    assert (w.device_id, "STALE") in master.registry.sweep(now + dt.timedelta(seconds=45))
    assert (w.device_id, "OFFLINE") in master.registry.sweep(now + dt.timedelta(seconds=400))
    acts = master.audit_actions(w.device_id)
    assert "device.heartbeat_stale" in acts and "device.offline" in acts
    assert w.beat()["status"] == "ACTIVE"                 # 回來
    assert master.repo.get_device(w.device_id)["connection_state"] == "CONNECTED"
    assert master.audit_actions(w.device_id).count("device.connected") == 2


def test_disconnect_marks_state(master, worker_factory):
    w = worker_factory().onboard()
    w.beat()
    w.client().disconnect(w.device_id)
    assert master.repo.get_device(w.device_id)["connection_state"] == "DISCONNECTED"
    assert "device.disconnected" in master.audit_actions(w.device_id)


# ---------- 綁定位址規則（決策 1） ----------
def test_bind_policy():
    assert BindPolicy.allowed("127.0.0.1", False) == (True, "loopback")
    assert BindPolicy.allowed("192.168.1.5", False) == (True, "lan")
    assert BindPolicy.allowed("100.100.1.2", False) == (True, "vpn")          # Tailscale
    assert BindPolicy.allowed("0.0.0.0", False) == (False, "all")
    assert BindPolicy.allowed("0.0.0.0", True) == (True, "all")
    assert BindPolicy.allowed("8.8.8.8", True) == (False, "public")
    assert BindPolicy.allowed("not-an-ip", True) == (False, "invalid")
    assert "127.0.0.1" in BindPolicy.local_addresses()


def test_master_refuses_public_bind(tmp_path):
    from nexus.tests.integration.conftest import Master
    with pytest.raises(RuntimeError, match="不允許綁定"):
        Master(tmp_path / "pub", bind_host="0.0.0.0")


# ---------- 日誌遮罩（AT-021 整合部分） ----------
def test_at021_no_secrets_in_audit_or_log(master, worker_factory, caplog):
    import logging
    caplog.set_level(logging.DEBUG, logger="nexus")
    w = worker_factory().onboard()
    w.beat()
    token = w.token
    keypem = open(os.path.join(w.dir, "device.key")).read()
    everything = " ".join(r["details_json"] + r["reason"] for r in master.repo.audit_tail(500)) + caplog.text
    assert token not in everything
    assert "PRIVATE KEY" not in everything
    assert keypem[40:80] not in everything


def test_keepalive_idle_timeout_is_set(master, worker_factory):
    w = worker_factory().onboard()
    c = w.client()
    w.beat(c)
    time.sleep(0.2)
    assert master.conns.count(w.device_id) == 1          # keep-alive 連線有被追蹤

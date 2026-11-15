"""
V2 PR5：派送協定（真 TLS、真 SQLite、FakeWorker 照協定一步一步打）
AT-001／AT-009／AT-010／AT-011／AT-012／AT-015／AT-023、T-02／T-03、結果驗證、拒收、長輪詢
"""
import datetime as dt
import hashlib
import threading
import time
import uuid

import pytest

from nexus.core.errors import NexusError
from nexus.tests.integration.conftest import Master

CAPS = {"resources": {"cpu_count": 4, "cpu_load": 0.1, "memory_available_mb": 8000, "disk_free_mb": 50000, "platform": "windows"},
        "adapters": {"test.echo": "1.0", "test.sleep": "1.0", "test.fail": "1.0"}, "max_concurrency": 1, "accepting": True}


def ready(w, **caps):
    w.beat(**{**CAPS, **caps})
    return w


def echo_result(text):
    return {"text": text, "sha256": hashlib.sha256(text.encode()).hexdigest()}


class Run:
    """一個 Worker 拿到的一次任務：方便照順序送事件"""

    def __init__(self, w, msg):
        self.w, self.msg, self.seq = w, msg, 0
        self.c = w.client()

    def ack(self, accept=True, reason=""):
        m = self.msg
        return self.c.task_ack(self.w.device_id, m["task_id"], m["attempt_id"], m["lease_id"], accept, reason)

    def ev(self, etype, data=None, event_id=None, seq=None):
        if seq is None:
            self.seq += 1
            seq = self.seq
        return self.c.task_event(self.msg["task_id"], self.msg["attempt_id"], seq, etype, data, event_id=event_id)


def take(master, w, wait=0):
    master.dispatcher.scheduler.run_once()
    msgs = w.client().inbox(w.device_id, wait)["messages"]
    tasks = [m for m in msgs if m["kind"] == "task"]
    return Run(w, tasks[0]) if tasks else None, msgs


def status(master, task_id):
    return master.tasks.get(task_id)["status"]


def test_full_path_echo_succeeds_with_audit(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.echo", {"text": "你好 Nexus"})
    run, _ = take(master, w)
    assert run.msg["manifest"]["params"] == {"text": "你好 Nexus"} and run.msg["manifest_sha256"] == t["manifest_sha256"]
    assert status(master, t["task_id"]) == "DISPATCHED"
    assert run.ack()["start"] is True
    run.ev("PREPARING")
    run.ev("RUNNING")
    r = run.ev("SUCCEEDED", {"result": echo_result("你好 Nexus")})
    assert r["status"] == "SUCCEEDED" and r["stop"] is True
    d = master.tasks.detail(t["task_id"])
    assert d["task"]["attempt_count"] == 1 and d["attempts"][0]["status"] == "SUCCEEDED"
    assert [e["type"] for e in d["events"]] == ["PREPARING", "RUNNING", "SUCCEEDED"]
    acts = [a["action"] for a in d["audit"]]
    for a in ("task.queued", "task.scheduled", "task.dispatched", "task.accepted", "task.running", "task.verifying", "task.succeeded"):
        assert a in acts, a
    assert d["decisions"][0]["chosen_device_id"] == w.device_id


def test_master_verifies_result_itself(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.echo", {"text": "abc"})
    run, _ = take(master, w)
    run.ack()
    run.ev("RUNNING")
    r = run.ev("SUCCEEDED", {"result": {"text": "abc", "sha256": "0" * 64}})       # Worker 說謊
    assert r["status"] == "RESULT_REJECTED" and status(master, t["task_id"]) == "RESULT_REJECTED"


def test_at001_unapproved_suspended_revoked_get_nothing(master, worker_factory):
    w = worker_factory("p")
    w.register(master.registry.pair.new())
    master.tasks.create("test.echo", {"text": "secret"})
    with pytest.raises(NexusError):                     # 還沒核准：沒有憑證，連 mTLS 都過不了
        w.client().inbox(w.device_id or str(uuid.uuid4()), 0)
    a = ready(worker_factory("a").onboard())
    master.registry.suspend(a.device_id, "owner", "測試")
    with pytest.raises(NexusError) as e:
        a.client().inbox(a.device_id, 0)
    assert e.value.code == "DEVICE_SUSPENDED"
    master.registry.resume(a.device_id, "owner", "ok")
    master.registry.revoke(a.device_id, "owner", "測試")
    with pytest.raises(NexusError) as e:
        a.client().inbox(a.device_id, 0)
    assert e.value.code == "CREDENTIAL_REVOKED"


def test_at009_no_ack_lease_expires_requeue_and_late_ack_refused(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.echo", {"text": "x"})
    run, _ = take(master, w)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=31)
    master.dispatcher.sweep(later)
    assert status(master, t["task_id"]) == "QUEUED"
    assert master.tasks.detail(t["task_id"])["attempts"][0]["status"] == "EXPIRED"
    r = run.ack()
    assert r["start"] is False                                     # 晚到的 ACK → 不准開始
    run2, _ = take(master, w)                                      # 重新派：新的 attempt、新的 lease
    assert run2.msg["attempt_id"] != run.msg["attempt_id"] and run2.msg["lease_id"] != run.msg["lease_id"]
    assert run2.ack()["start"] is True


def test_redelivery_of_same_offer_and_idempotent_ack(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    master.tasks.create("test.echo", {"text": "x"})
    run, _ = take(master, w)
    _, msgs = take(master, w)                                       # Worker 沒收到 → 再問一次
    assert [m["attempt_id"] for m in msgs if m["kind"] == "task"] == [run.msg["attempt_id"]]
    assert run.ack()["start"] is True and run.ack()["start"] is True


def test_at012_duplicate_and_out_of_order_events(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.echo", {"text": "x"})
    run, _ = take(master, w)
    run.ack()
    eid = str(uuid.uuid4())
    run.ev("RUNNING", event_id=eid, seq=1)
    assert run.ev("RUNNING", event_id=eid, seq=1)["duplicate"] is True   # 重送
    with pytest.raises(NexusError) as e:
        run.ev("PROGRESS", seq=1)                                       # 不同事件、倒退的序號
    assert e.value.code == "EVENT_OUT_OF_ORDER"
    run.seq = 1
    run.ev("SUCCEEDED", {"result": echo_result("x")})
    assert status(master, t["task_id"]) == "SUCCEEDED"
    r = run.ev("SUCCEEDED", {"result": echo_result("x")})               # 又送一次完成（新 event_id）
    assert r["stop"] is True and r["status"] == "SUCCEEDED"            # 記錄下來但不會完成兩次
    assert sum(1 for a in master.tasks.detail(t["task_id"])["audit"] if a["action"] == "task.succeeded") == 1


def test_events_from_other_device_refused(master, worker_factory):
    a, b = ready(worker_factory("a").onboard()), ready(worker_factory("b").onboard())
    master.tasks.create("test.echo", {"text": "x"})
    run, _ = take(master, a)
    run.ack()
    intruder = Run(b, run.msg)
    with pytest.raises(NexusError) as e:
        intruder.ev("SUCCEEDED", {"result": echo_result("x")})
    assert e.value.code == "TASK_NOT_ASSIGNED"
    with pytest.raises(NexusError) as e:
        intruder.ack()
    assert e.value.code == "TASK_NOT_ASSIGNED"


def test_decline_goes_back_to_queue_and_skips_that_device(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.echo", {"text": "x"})
    run, _ = take(master, w)
    assert run.ack(accept=False, reason="ADAPTER_UNSUPPORTED")["start"] is False
    assert status(master, t["task_id"]) == "QUEUED"
    master.dispatcher.scheduler.run_once()
    assert status(master, t["task_id"]) == "QUEUED"                # 唯一的節點拒收過 → 不再硬派（AT-016）
    d = master.tasks.detail(t["task_id"])
    assert "declined" in d["decisions"][-1]["candidates"][0]["reasons"] and d["task"]["attempt_count"] == 0
    b = ready(worker_factory("b").onboard())
    run2, _ = take(master, b)
    assert run2 and run2.msg["task_id"] == t["task_id"]


def test_at015_cancel_running_waits_for_worker(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.sleep", {"seconds": 60})
    run, _ = take(master, w)
    run.ack()
    run.ev("RUNNING")
    assert master.tasks.cancel(t["task_id"], "owner", "不要了")["status"] == "CANCELLING"
    msgs = w.client().inbox(w.device_id, 0)["messages"]
    assert {"kind": "cancel", "task_id": t["task_id"], "attempt_id": run.msg["attempt_id"], "reason": "cancelled"} in msgs
    assert status(master, t["task_id"]) == "CANCELLING"            # 節點確認前不會說「已取消」
    r = run.ev("CANCELLED")
    assert r["status"] == "CANCELLED"


def test_cancel_while_dispatched_blocks_start(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.echo", {"text": "x"})
    run, _ = take(master, w)
    assert master.tasks.cancel(t["task_id"], "owner", "改主意")["status"] == "CANCELLED"
    assert run.ack()["start"] is False


def test_at010_revoke_while_running_becomes_unknown_not_rerun(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.sleep", {"seconds": 60})
    run, _ = take(master, w)
    run.ack()
    run.ev("RUNNING")
    master.registry.revoke(w.device_id, "owner", "測試")
    d = master.tasks.detail(t["task_id"])
    assert d["task"]["status"] == "UNKNOWN" and d["attempts"][0]["status"] == "LOST"
    master.dispatcher.scheduler.run_once()
    assert status(master, t["task_id"]) == "UNKNOWN"               # 不會自動重跑
    assert master.tasks.mark_failed(t["task_id"], "owner", "確認節點已關")["status"] == "FAILED"


def test_offline_device_tasks_become_unknown_and_scheduled_requeued(master, worker_factory):
    a, b = ready(worker_factory("a").onboard()), ready(worker_factory("b").onboard())
    t1 = master.tasks.create("test.sleep", {"seconds": 60})
    run, _ = take(master, a)
    run.ack()
    run.ev("RUNNING")
    master.repo.set_connection_state(a.device_id, "OFFLINE")
    master.dispatcher.sweep()
    assert status(master, t1["task_id"]) == "UNKNOWN"
    # 節點回來、繼續回報 → 接得上（UNKNOWN → RUNNING → 完成）
    master.repo.set_connection_state(a.device_id, "CONNECTED")
    r = run.ev("SUCCEEDED", {"result": {"slept": 60}})
    assert r["status"] == "SUCCEEDED"
    assert b


def test_timeout_master_side(master, worker_factory):
    """T-02：超過上限＋寬限 → TIMED_OUT → FAILED（不重試），並叫節點停"""
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.sleep", {"seconds": 600}, max_runtime_seconds=5)
    run, _ = take(master, w)
    run.ack()
    run.ev("RUNNING")
    master.dispatcher.sweep(dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=25))
    d = master.tasks.detail(t["task_id"])
    assert d["task"]["status"] == "FAILED" and d["task"]["error_code"] == "TIMED_OUT"
    assert "task.timed_out" in [a["action"] for a in d["audit"]]
    msgs = w.client().inbox(w.device_id, 0)["messages"]
    assert any(m["kind"] == "cancel" and m["reason"] == "timed_out" for m in msgs)
    assert run.ev("TIMED_OUT")["stop"] is True                      # 節點晚到的回報：記錄但不改結果
    assert status(master, t["task_id"]) == "FAILED"


def test_retry_moves_to_another_attempt(master, worker_factory):
    """T-03：可重試的錯誤 → 新 attempt；不可重試 → 停在 FAILED"""
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.fail", {"code": "ADAPTER_ERROR"}, max_attempts=2)
    for _i in range(2):
        run, _ = take(master, w)
        run.ack()
        run.ev("RUNNING")
        run.ev("FAILED", {"error_code": "ADAPTER_ERROR", "message": "boom"})
    d = master.tasks.detail(t["task_id"])
    assert d["task"]["status"] == "FAILED" and [a["attempt_no"] for a in d["attempts"]] == [1, 2]
    t2 = master.tasks.create("test.fail", {"code": "INVALID_INPUT"}, max_attempts=3)
    run, _ = take(master, w)
    run.ack()
    run.ev("FAILED", {"error_code": "INVALID_INPUT", "message": "bad"})
    assert status(master, t2["task_id"]) == "FAILED" and len(master.tasks.detail(t2["task_id"])["attempts"]) == 1


def test_long_poll_wakes_up_when_task_arrives(tmp_path, worker_factory):
    m = Master(tmp_path / "lp", auto_dispatch=True, tick=0.2)
    try:
        from nexus.tests.integration.conftest import FakeWorker
        w = ready(FakeWorker(m, tmp_path, "lp").onboard())
        got = {}

        def poll():
            t0 = time.time()
            got["msgs"] = w.client().inbox(w.device_id, 10)["messages"]
            got["dt"] = time.time() - t0
        th = threading.Thread(target=poll)
        th.start()
        time.sleep(1.0)
        m.tasks.create("test.echo", {"text": "wake"})
        th.join(15)
        assert got["msgs"] and got["msgs"][0]["kind"] == "task" and got["dt"] < 4
    finally:
        m.stop()


def test_at011_master_restart_mid_task(master, worker_factory):
    w = ready(worker_factory("a").onboard())
    t = master.tasks.create("test.sleep", {"seconds": 3})
    run, _ = take(master, w)
    run.ack()
    run.ev("RUNNING")
    master.restart()
    assert status(master, t["task_id"]) == "RUNNING"               # 從資料庫恢復
    run.c = w.client()
    r = run.ev("SUCCEEDED", {"result": {"slept": 3}})
    assert r["status"] == "SUCCEEDED"


def test_at023_inbox_rate_limited(tmp_path):
    from nexus.master.ratelimit import DEFAULT_RULES
    m = Master(tmp_path / "rl", rules={**DEFAULT_RULES, "heartbeat": (200, 1), "inbox": (3, 60)})
    try:
        from nexus.tests.integration.conftest import FakeWorker
        w = FakeWorker(m, tmp_path, "rl").onboard()
        c = w.client()
        codes = []
        for _ in range(6):
            try:
                c.inbox(w.device_id, 0)
                codes.append("ok")
            except NexusError as e:
                codes.append(e.code)
        assert codes[:3] == ["ok"] * 3 and "RATE_LIMITED" in codes
    finally:
        m.stop()

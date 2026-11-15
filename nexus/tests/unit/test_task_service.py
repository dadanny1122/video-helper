"""V2 PR2：TaskService——建立／驗證／idempotency（AT-004）／非法內容（AT-005）／取消／單一轉移入口＋樂觀鎖＋同交易稽核"""
import json
import threading

import pytest

from nexus.core.errors import NexusError
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.tasks import manifest
from nexus.tasks.service import TaskService
from nexus.tasks.states import TaskStatus

S = TaskStatus


@pytest.fixture
def svc(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    changes = []
    s = TaskService(db, Repo(db), on_change=changes.append)
    s.changes = changes
    yield s
    db.close()


def actions(svc, task_id):
    return [a["action"] for a in reversed(svc.repo.audit_tail(100, task_id))]


def test_create_goes_to_queued_with_audit_trail(svc):
    t = svc.create("test.echo", {"text": "hi"})
    assert t["status"] == "QUEUED" and t["attempt_count"] == 0 and t["max_attempts"] == 2
    assert actions(svc, t["task_id"]) == ["task.created", "task.validating", "task.queued"]
    assert svc.changes and svc.changes[-1]["status"] == "QUEUED"


def test_idempotency_same_key_same_content_returns_existing(svc):
    """AT-004"""
    a = svc.create("test.echo", {"text": "hi"}, idempotency_key="k-1")
    b = svc.create("test.echo", {"text": "hi"}, idempotency_key="k-1")
    assert a["task_id"] == b["task_id"] and len(svc.list()) == 1


def test_idempotency_same_key_different_content_conflicts(svc):
    svc.create("test.echo", {"text": "hi"}, idempotency_key="k-2")
    with pytest.raises(NexusError) as e:
        svc.create("test.echo", {"text": "bye"}, idempotency_key="k-2")
    assert e.value.code == "IDEMPOTENCY_CONFLICT" and len(svc.list()) == 1


@pytest.mark.parametrize("ttype,params", [
    ("test.echo", {"text": "x", "cmd": "calc"}), ("test.sleep", {"seconds": 9999}), ("shell.run", {"cmd": "dir"}),
    ("test.fail", {"code": "NOPE"})])
def test_invalid_becomes_rejected_never_queued(svc, ttype, params):
    """AT-005：不合格 → REJECTED（看得到原因），永遠不會被排程"""
    t = svc.create(ttype, params)
    assert t["status"] == "REJECTED" and t["error_message"]
    assert svc.list(active_only=True) == []


def test_garbage_manifest_is_refused_without_creating_rows(svc):
    with pytest.raises(NexusError) as e:
        svc.submit({"cmd": "format c:"})
    assert e.value.code == "MANIFEST_INVALID" and svc.list() == []


def test_oversized_manifest_rejected_and_not_stored_whole(svc):
    m = manifest.build("test.echo", {"text": "x" * 50000})
    t = svc.submit(m)
    assert t["status"] == "REJECTED" and len(t["manifest_json"]) < 200


def test_illegal_transition_and_stale_version(svc):
    t = svc.create("test.echo", {"text": "hi"})
    with pytest.raises(NexusError) as e:
        svc.transition(t["task_id"], S.RUNNING, "test")
    assert e.value.code == "INVALID_TRANSITION"
    with pytest.raises(NexusError) as e:
        svc.transition(t["task_id"], S.SCHEDULED, "test", expected_version=t["version"] - 1)
    assert e.value.code == "CONCURRENT_UPDATE"
    assert svc.get(t["task_id"])["status"] == "QUEUED"


def test_concurrent_transitions_only_one_wins(svc):
    t = svc.create("test.echo", {"text": "hi"})
    results = []

    def go():
        try:
            svc.transition(t["task_id"], S.SCHEDULED, "race", expected_version=t["version"])
            results.append("ok")
        except NexusError as e:
            results.append(e.code)
    ths = [threading.Thread(target=go) for _ in range(8)]
    [x.start() for x in ths]
    [x.join() for x in ths]
    assert results.count("ok") == 1 and all(r in ("ok", "CONCURRENT_UPDATE") for r in results)
    assert actions(svc, t["task_id"]).count("task.scheduled") == 1


def test_cancel_paths(svc):
    q = svc.create("test.echo", {"text": "a"})
    assert svc.cancel(q["task_id"], "owner", "不要了")["status"] == "CANCELLED"
    with pytest.raises(NexusError):
        svc.cancel(q["task_id"], "owner", "再取消一次")                   # 終態不能取消
    with pytest.raises(NexusError):
        svc.cancel(svc.create("test.echo", {"text": "b"})["task_id"], "owner", " ")   # 沒理由
    r = svc.create("test.echo", {"text": "c"})
    for st in (S.SCHEDULED, S.DISPATCHED, S.ACCEPTED, S.PREPARING, S.RUNNING):
        svc.transition(r["task_id"], st, "test")
    t = svc.cancel(r["task_id"], "owner", "停")
    assert t["status"] == "CANCELLING"                                     # 在 Worker 上 → 等確認
    assert svc.cancel(r["task_id"], "owner", "又按一次")["status"] == "CANCELLING"


def test_retry_policy(svc):
    def run_and_fail(t, code):
        for st in (S.SCHEDULED, S.DISPATCHED, S.ACCEPTED, S.PREPARING, S.RUNNING):
            t = svc.transition(t["task_id"], st, "test")
        with svc.db.tx() as c:
            t = svc.tr.get_task(t["task_id"], c)
            svc.tr.update_task(c, t["task_id"], t["version"], attempt_count=t["attempt_count"] + 1)
            t = svc.tr.get_task(t["task_id"], c)
            return svc.fail_in_tx(c, t, code, "boom", "worker:x")
    t = svc.create("test.fail", {"code": "ADAPTER_ERROR"}, max_attempts=2)
    t = run_and_fail(t, "ADAPTER_ERROR")
    assert t["status"] == "QUEUED"                                         # 第 1 次失敗 → 重試
    t = run_and_fail(t, "ADAPTER_ERROR")
    assert t["status"] == "FAILED"                                         # 用完次數
    t2 = run_and_fail(svc.create("test.fail", {"code": "INVALID_INPUT"}, max_attempts=3), "INVALID_INPUT")
    assert t2["status"] == "FAILED"                                        # 不可重試的錯誤
    acts = actions(svc, t["task_id"])
    assert acts.count("task.failed") == 2 and acts.count("task.queued") == 2


def test_mark_failed_only_for_unknown(svc):
    t = svc.create("test.echo", {"text": "a"})
    with pytest.raises(NexusError):
        svc.mark_failed(t["task_id"], "owner", "x")
    for st in (S.SCHEDULED, S.DISPATCHED, S.ACCEPTED, S.PREPARING, S.UNKNOWN):
        t = svc.transition(t["task_id"], st, "test")
    t = svc.mark_failed(t["task_id"], "owner", "節點壞掉，確認沒跑")
    assert t["status"] == "FAILED" and t["error_code"] == "MANUAL"


def test_detail(svc):
    t = svc.create("test.echo", {"text": "a"})
    d = svc.detail(t["task_id"])
    assert d["manifest"]["params"] == {"text": "a"} and d["attempts"] == [] and len(d["audit"]) == 3
    assert json.loads(d["task"]["manifest_json"])["task_id"] == t["task_id"]

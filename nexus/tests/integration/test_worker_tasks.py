"""
V2 PR6：真的 Worker Agent＋Executor 對真 Master（自動排程／派送）——端到端
T-01（兩個節點各拿不同任務）、取消、Worker 端逾時、重試、AT-018（版本不合拒收）、T-05（manifest 被改拒收）、撤銷時停止
"""
import os
import time

import pytest

from nexus.core.config import Config
from nexus.tasks import manifest
from nexus.tests.integration.conftest import Master
from nexus.worker.agent import WorkerAgent
from nexus.worker.credstore import CredStore


def wait_for(pred, timeout=15):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture
def m(tmp_path):
    mm = Master(tmp_path / "m", auto_dispatch=True, tick=0.2)
    yield mm
    mm.stop()


@pytest.fixture
def agents(m, tmp_path):
    made = []

    def make(name, **cfg):
        os.environ["VH_NEXUS_ROOT"] = str(tmp_path / name)
        c = Config("worker")
        for k, v in cfg.items():
            c[k] = v
        a = WorkerAgent(c, CredStore(str(tmp_path / name / "identity")), heartbeat_seconds=1, poll_seconds=0.3)
        a.executor.poll_wait = 2
        a.start()
        a.pair(m.url, m.registry.pair.new(), name)
        m.registry.approve(a.store.state["device_id"], "owner", "ok")
        assert wait_for(lambda: m.registry.tr.capabilities(a.store.state["device_id"]) is not None), a.status()
        made.append(a)
        return a
    yield make
    for a in made:
        a.stop(disconnect=False)


def st(m, tid):
    return m.tasks.get(tid)["status"]


def test_t01_two_workers_get_different_tasks_and_succeed(m, agents):
    a, b = agents("pc-a"), agents("pc-b")
    t1 = m.tasks.create("test.sleep", {"seconds": 2})
    t2 = m.tasks.create("test.sleep", {"seconds": 2})
    assert wait_for(lambda: st(m, t1["task_id"]) == "SUCCEEDED" and st(m, t2["task_id"]) == "SUCCEEDED", 20)
    d1, d2 = m.tasks.get(t1["task_id"]), m.tasks.get(t2["task_id"])
    assert {d1["assigned_device_id"], d2["assigned_device_id"]} == {a.store.state["device_id"], b.store.state["device_id"]}
    ev_types = [e["type"] for e in m.tasks.detail(t1["task_id"])["events"]]
    assert ev_types[:2] == ["PREPARING", "RUNNING"] and "PROGRESS" in ev_types and ev_types[-1] == "SUCCEEDED"


def test_echo_end_to_end(m, agents):
    agents("pc-e")
    t = m.tasks.create("test.echo", {"text": "中文 OK ✓"})
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED")
    import json
    assert json.loads(m.tasks.get(t["task_id"])["result_json"])["text"] == "中文 OK ✓"


def test_cancel_running_sleep(m, agents):
    a = agents("pc-c")
    t = m.tasks.create("test.sleep", {"seconds": 60})
    assert wait_for(lambda: st(m, t["task_id"]) == "RUNNING")
    m.tasks.cancel(t["task_id"], "owner", "測試取消")
    assert wait_for(lambda: st(m, t["task_id"]) == "CANCELLED", 10)
    assert wait_for(lambda: a.executor.running_ids() == [], 5)            # 節點真的停了


def test_worker_side_timeout(m, agents):
    agents("pc-t")
    t = m.tasks.create("test.sleep", {"seconds": 30}, max_runtime_seconds=2)
    assert wait_for(lambda: st(m, t["task_id"]) == "FAILED", 15)
    assert m.tasks.get(t["task_id"])["error_code"] == "TIMED_OUT"


def test_retry_then_fail(m, agents):
    agents("pc-r")
    t = m.tasks.create("test.fail", {"code": "ADAPTER_ERROR"}, max_attempts=2)
    assert wait_for(lambda: st(m, t["task_id"]) == "FAILED" and len(m.tasks.detail(t["task_id"])["attempts"]) == 2, 15)


def test_revoke_stops_running_task_and_marks_unknown(m, agents):
    a = agents("pc-x")
    t = m.tasks.create("test.sleep", {"seconds": 60})
    assert wait_for(lambda: st(m, t["task_id"]) == "RUNNING")
    m.registry.revoke(a.store.state["device_id"], "owner", "測試")
    assert st(m, t["task_id"]) == "UNKNOWN"
    assert wait_for(lambda: a.state == "REVOKED", 10)
    assert wait_for(lambda: a.executor.running_ids() == [], 5)


def test_not_accepting_gets_nothing(m, agents):
    agents("pc-n", accept_tasks=False)
    t = m.tasks.create("test.echo", {"text": "x"})
    time.sleep(2)
    assert st(m, t["task_id"]) == "QUEUED"
    d = m.tasks.detail(t["task_id"])["decisions"][-1]
    assert "not_accepting" in d["candidates"][0]["reasons"]


def test_worker_checks_manifest_and_version(m, agents):
    """T-05（內容被改）、AT-018（版本不相容）：Worker 自己拒收"""
    a = agents("pc-v")
    mf = manifest.build("test.echo", {"text": "x"})
    msg = {"kind": "task", "task_id": mf["task_id"], "attempt_id": "a", "lease_id": "l", "manifest": mf,
           "manifest_sha256": manifest.sha256(mf)}
    assert a.executor.check(msg) == ""
    tampered = {**msg, "manifest": {**mf, "params": {"text": "rm -rf"}}}
    assert a.executor.check(tampered) == "MANIFEST_INVALID"
    a.executor.versions = {"test.echo": "2.0"}
    assert a.executor.check(msg) == "ADAPTER_UNSUPPORTED"
    assert a.capabilities()["adapters"] == {"test.echo": "2.0"}           # 心跳也照實回報 → 排程器會排除它

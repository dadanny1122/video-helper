"""V2 PR4：排程器——硬性篩選先於評分、可解釋分數、同分可重現（AT-016／AT-017）、決策紀錄"""
import datetime as dt
import json

import pytest

from nexus.core.models import utcnow
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.tasks import manifest
from nexus.tasks.scheduler import REASONS, WEIGHTS, Scheduler, evaluate
from nexus.tasks.service import TaskService

NOW = dt.datetime(2026, 11, 20, 10, 0, 0, tzinfo=dt.timezone.utc)
SEEN = "2026-11-20T09:59:55Z"


def dev(did, **kw):
    d = dict(device_id=did, name=f"pc-{did}", status="ACTIVE", connection_state="CONNECTED", last_seen_at=SEEN,
             protocol_version="1.1")
    d.update(kw)
    return d


def cap(**kw):
    c = {"resources": {"cpu_count": 8, "cpu_load": 0.2, "memory_available_mb": 8000, "disk_free_mb": 50000, "platform": "windows"},
         "adapters": {"test.echo": "1.0", "test.sleep": "1.0", "test.fail": "1.0"}, "max_concurrency": 1, "accepting": True}
    res = kw.pop("resources", None)
    if res:
        c["resources"] = {**c["resources"], **res}
    c.update(kw)
    return c


TASK = {"task_id": "t", "type": "test.echo"}
M = manifest.build("test.echo", {"text": "x"})


def ev(devices, caps, busy=None, history=None, declined=None, m=M, task=TASK):
    return evaluate(task, m, devices, caps, busy or {}, history or {}, declined or set(), now=NOW)


def test_weights_sum_to_one_and_no_gpu_penalty():
    for w in WEIGHTS.values():
        assert abs(sum(w.values()) - 1) < 1e-9 and all(v >= 0 for v in w.values())
        assert "gpu" not in w                     # V2 不用 GPU → 沒有 GPU 的節點不會被扣分


@pytest.mark.parametrize("dkw,ckw,reason", [
    ({"status": "SUSPENDED"}, {}, "not_active"),
    ({"status": "REVOKED"}, {}, "not_active"),
    ({"connection_state": "STALE"}, {}, "not_connected"),
    ({"last_seen_at": "2026-11-20T09:58:00Z"}, {}, "not_connected"),
    ({"protocol_version": "1.0"}, {}, "old_protocol"),
    ({}, None, "no_capabilities"),
    ({}, {"accepting": False}, "not_accepting"),
    ({}, {"adapters": {"test.sleep": "1.0"}}, "adapter_missing"),
    ({}, {"adapters": {"test.echo": "2.0"}}, "adapter_version"),
])
def test_hard_filters(dkw, ckw, reason):
    caps = {} if ckw is None else {"a": cap(**ckw)}
    r = ev([dev("a", **dkw)], caps)[0]
    assert not r["eligible"] and reason in r["reasons"] and reason in REASONS


def test_requirements_and_capacity_filters():
    m = manifest.build("test.echo", {"text": "x"}, min_memory_mb=16000, min_disk_mb=100, platform="windows")
    r = ev([dev("a")], {"a": cap(resources={"memory_available_mb": 4000, "platform": "linux"})}, m=m)[0]
    assert {"memory", "platform"} <= set(r["reasons"])
    r = ev([dev("a")], {"a": cap()}, busy={"a": 1})[0]
    assert "full" in r["reasons"]
    r = ev([dev("a")], {"a": cap()}, declined={"a"})[0]
    assert "declined" in r["reasons"]


def test_high_score_cannot_override_filter():
    """被排除的節點資源再好也不會被選"""
    rows = ev([dev("strong", status="SUSPENDED"), dev("weak")],
              {"strong": cap(resources={"cpu_load": 0.0, "memory_available_mb": 64000}),
               "weak": cap(resources={"cpu_load": 0.9, "memory_available_mb": 600})})
    assert rows[0]["device_id"] == "weak" and rows[0]["eligible"] and not rows[1]["eligible"]


def test_scores_are_explained_and_bounded():
    r = ev([dev("a")], {"a": cap()}, history={"a": ["SUCCEEDED"] * 8 + ["FAILED"] * 2})[0]
    assert set(r["scores"]) == set(WEIGHTS["w1"]) and all(0 <= v <= 1 for v in r["scores"].values())
    assert 0 <= r["total"] <= 100
    assert r["scores"]["reliability"] == round(9 / 12, 3)


def test_less_loaded_wins():
    rows = ev([dev("a"), dev("b")], {"a": cap(resources={"cpu_load": 0.9}), "b": cap(resources={"cpu_load": 0.1})})
    assert rows[0]["device_id"] == "b"


def test_tie_breaker_is_stable():
    """AT-017：分數一樣 → 執行中少的 → device_id 字典序；跑幾次都一樣"""
    devices = [dev(x) for x in ("c", "a", "b")]
    caps = {x: cap(max_concurrency=2) for x in ("a", "b", "c")}
    picks = {ev(devices[::k], caps)[0]["device_id"] for k in (1, -1)}
    assert picks == {"a"}
    assert ev(devices, caps, busy={"a": 1})[0]["device_id"] == "b"


# ---------- 接上資料庫 ----------
@pytest.fixture
def env(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    repo = Repo(db)
    svc = TaskService(db, repo)
    yield svc, repo, Scheduler(svc, repo)
    db.close()


def add_device(svc, repo, did, caps=True, **kw):
    with svc.db.tx() as c:
        repo.insert_device(c, dict(device_id=did, name=f"pc-{did}", platform="windows", agent_version="0.2.0", protocol_version="1.1",
                                   status=kw.get("status", "ACTIVE"), public_key_fingerprint="fp-" + did, created_at=utcnow(),
                                   connection_state="CONNECTED", last_seen_at=utcnow()))
    if caps:
        svc.tr.upsert_capabilities(did, cap()["resources"], cap()["adapters"], 1, True)


def test_no_eligible_stays_queued_with_reasons_once(env):
    """AT-016：沒有合格節點 → 留在 QUEUED，排除原因看得到；同樣的結果不會一直重複存"""
    svc, repo, sch = env
    add_device(svc, repo, "a", status="SUSPENDED")
    t = svc.create("test.echo", {"text": "x"})
    for _ in range(3):
        assert sch.run_once() == [(t["task_id"], None)]
    assert svc.get(t["task_id"])["status"] == "QUEUED"
    ds = svc.tr.decisions_of(t["task_id"])
    assert len(ds) == 1 and ds[0]["chosen_device_id"] is None
    assert "not_active" in ds[0]["candidates"][0]["reasons"]


def test_assigns_and_respects_capacity(env):
    svc, repo, sch = env
    add_device(svc, repo, "a")
    add_device(svc, repo, "b")
    t1, t2, t3 = (svc.create("test.echo", {"text": str(i)}) for i in range(3))
    res = dict(sch.run_once())
    assert res[t1["task_id"]] == "a" and res[t2["task_id"]] == "b" and res[t3["task_id"]] is None   # 每台 1 個名額
    t1 = svc.get(t1["task_id"])
    assert t1["status"] == "SCHEDULED" and t1["assigned_device_id"] == "a"
    d = svc.tr.decisions_of(t1["task_id"])[0]
    assert d["weights_version"] == "w1" and len(d["candidates"]) == 2
    assert json.loads(json.dumps(d["candidates"]))[0]["total"] > 0

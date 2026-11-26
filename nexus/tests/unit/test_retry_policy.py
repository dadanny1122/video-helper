"""APEX（借 Temporal）：重試政策寫成資料＋重試要等時間＋逾時分開顯示"""
import datetime as dt
import random

import pytest

from nexus import display as nd
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.tasks import adapters
from nexus.tasks.adapters import RetryPolicy
from nexus.tasks.service import TaskService
from nexus.tasks.states import TaskStatus

S = TaskStatus


class _NoJitter:
    @staticmethod
    def uniform(a, b):
        return 0.0


# ---------- 政策本身 ----------
def test_delay_grows_then_caps():
    p = RetryPolicy(initial_seconds=30, backoff=2, max_seconds=600)
    assert [p.delay(n, rng=_NoJitter) for n in (1, 2, 3, 4, 5, 6)] == [30, 60, 120, 240, 480, 600]


def test_jitter_stays_in_range():
    p = RetryPolicy(initial_seconds=100, jitter=0.2)
    rng = random.Random(1)
    vals = [p.delay(1, rng=rng) for _ in range(200)]
    assert min(vals) >= 80 and max(vals) <= 120 and len(set(vals)) > 10


def test_min_by_error_wins():
    p = RetryPolicy(initial_seconds=30, min_by_error=(("RATE_LIMITED", 120),))
    assert p.delay(1, "RATE_LIMITED", rng=_NoJitter) == 120
    assert p.delay(1, "NETWORK", rng=_NoJitter) == 30


def test_policies_per_adapter():
    for name in ("test.echo", "test.sleep", "test.fail", "test.file"):
        assert adapters.get(name).retry.delay(3) == 0                 # 測試任務不等
    v = adapters.get("video.download").retry
    assert v.delay(1, "NETWORK", rng=_NoJitter) == 30 and v.delay(1, "RATE_LIMITED", rng=_NoJitter) == 120


# ---------- 服務＋排程 ----------
@pytest.fixture
def svc(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    s = TaskService(db, Repo(db))
    yield s
    db.close()


def _fail_once(svc, t, code):
    for st in (S.SCHEDULED, S.DISPATCHED, S.ACCEPTED, S.PREPARING, S.RUNNING):
        t = svc.transition(t["task_id"], st, "test")
    with svc.db.tx() as c:
        t = svc.tr.get_task(t["task_id"], c)
        svc.tr.update_task(c, t["task_id"], t["version"], attempt_count=t["attempt_count"] + 1)
        return svc.fail_in_tx(c, svc.tr.get_task(t["task_id"], c), code, "boom", "worker:x")


def test_test_adapter_retries_immediately(svc):
    t = _fail_once(svc, svc.create("test.fail", {"code": "ADAPTER_ERROR"}, max_attempts=2), "ADAPTER_ERROR")
    assert t["status"] == "QUEUED" and t["retry_at"] is None


def test_retry_waits_and_scheduler_skips_until_then(svc, monkeypatch):
    """用 test.fail 但換成要等的政策：排隊中、retry_at 在未來 → 排程器不碰；時間到了才排"""
    spec = adapters.ADAPTERS["test.fail"]
    monkeypatch.setitem(adapters.ADAPTERS, "test.fail",
                        adapters.AdapterSpec(**{**spec.__dict__, "retry": RetryPolicy(initial_seconds=60, jitter=0)}))
    t = _fail_once(svc, svc.create("test.fail", {"code": "ADAPTER_ERROR"}, max_attempts=3), "ADAPTER_ERROR")
    assert t["status"] == "QUEUED" and t["retry_at"]
    wait = (dt.datetime.fromisoformat(t["retry_at"].replace("Z", "+00:00")) - dt.datetime.now(dt.timezone.utc)).total_seconds()
    assert 50 < wait <= 60
    assert "60 秒後" in [a for a in svc.repo.audit_tail(5, t["task_id"])][0]["reason"]

    from nexus.tasks.scheduler import Scheduler
    sch = Scheduler.__new__(Scheduler)                   # 只測「挑哪些任務」：沒有節點 → 有挑到也只會記決策
    sch.svc, sch.tr, sch._last_sig = svc, svc.tr, {}
    sch._inputs = lambda: ([], {}, {}, {}, {})
    assert sch.run_once() == []                           # 還沒到時間：連看都不看
    with svc.db.tx() as c:
        cur = svc.tr.get_task(t["task_id"], c)
        svc.tr.update_task(c, t["task_id"], cur["version"], retry_at="2000-01-01T00:00:00Z")
    assert sch.run_once() == [(t["task_id"], None)]       # 時間到了：有拿出來排（沒節點所以沒選到）


def test_migration_adds_retry_at_to_old_db(tmp_path):
    db = Database(str(tmp_path / "old.db"))
    db.migrate()
    cols = [r[1] for r in db.conn().execute("PRAGMA table_info(tasks)")]
    assert "retry_at" in cols
    db.close()


# ---------- 顯示 ----------
def test_task_text():
    future = "2999-01-01T00:00:00Z"
    assert nd.task_text({"status": "QUEUED", "retry_at": future}).startswith("等待重試（")
    assert nd.task_text({"status": "QUEUED", "retry_at": "2000-01-01T00:00:00Z"}) == "排隊中"   # 時間過了就是一般排隊
    assert nd.task_text({"status": "QUEUED"}) == "排隊中"
    assert nd.task_text({"status": "FAILED", "error_code": "TIMED_OUT"}) == "失敗（執行太久）"
    assert nd.task_text({"status": "CANCELLED", "error_code": "QUEUE_EXPIRED"}) == "已取消（排隊太久，沒有節點接）"
    assert nd.task_text({"status": "FAILED", "error_code": "NETWORK"}) == "失敗"

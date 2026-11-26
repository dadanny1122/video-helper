"""APEX（借 Dask）：自己取名字的資源——節點宣告、任務要求、排程扣掉正在用的、舊 Master 相容"""
import pytest

import nexus
from nexus import display as nd
from nexus.core.errors import NexusError
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.tasks import capabilities, manifest
from nexus.tasks.scheduler import REASONS, Scheduler, named_short
from nexus.tasks.service import TaskService
from nexus.tests.unit.test_scheduler import M, cap, dev, ev


# ---------- 名字和數量的規則 ----------
def test_parse_named():
    assert capabilities.parse_named("gpu=1, pysam") == {"gpu": 1, "pysam": 1}
    assert capabilities.parse_named("GPU = 2，sim.pv-1=3") == {"gpu": 2, "sim.pv-1": 3}
    assert capabilities.parse_named("") == {}
    for bad in ("gpu=x", "1gpu", "gpu=0", "gpu=2000", "中文=1"):
        with pytest.raises(ValueError):
            capabilities.parse_named(bad)


def test_named_text():
    assert capabilities.named_text({"pysam": 1, "gpu": 2}) == "gpu×2、pysam×1"
    assert capabilities.named_text(None) == ""


# ---------- Master 收心跳 ----------
def _caps(**res):
    return {"resources": {"cpu_count": 4, **res}, "adapters": {"test.echo": "1.0.0"}}


def test_master_accepts_named():
    assert capabilities.normalize(_caps(named={"gpu": 1}))["resources"]["named"] == {"gpu": 1}


@pytest.mark.parametrize("bad", [{"GPU": 1}, {"gpu": 0}, {"gpu": True}, {f"r{i}": 1 for i in range(17)}, ["gpu"]])
def test_master_rejects_bad_named(bad):
    with pytest.raises(NexusError):
        capabilities.normalize(_caps(named=bad))


# ---------- 任務的需求 ----------
def test_manifest_resources_optional_and_checked():
    assert "resources" not in manifest.build("test.echo", {"text": "x"})["requirements"]     # 沒要求就不放（舊節點照樣收）
    m = manifest.build("test.echo", {"text": "x"}, resources={"gpu": 1})
    assert manifest.validate(m) == [] and m["requirements"]["resources"] == {"gpu": 1}
    m["requirements"]["resources"] = {"gpu": 0}
    assert any("requirements.resources" in p for p in manifest.validate(m))


# ---------- 排程 ----------
NEED = manifest.build("test.echo", {"text": "x"}, resources={"gpu": 1})


def test_named_short():
    assert named_short({"gpu": 1}, {"gpu": 1}, {}) == {}
    assert named_short({"gpu": 1}, {"gpu": 1}, {"gpu": 1}) == {"gpu": 1}
    assert named_short({"gpu": 2, "pysam": 1}, {"gpu": 1}, None) == {"gpu": 1, "pysam": 1}


def test_scheduler_only_picks_node_with_resource():
    devices = [dev("a"), dev("b")]
    caps = {"a": cap(), "b": cap(resources={"named": {"gpu": 1}})}
    rows = {r["device_id"]: r for r in ev(devices, caps, m=NEED)}
    assert rows["a"]["reasons"] == ["resource"] and rows["b"]["eligible"]
    assert all(r["eligible"] for r in ev(devices, caps, m=M))               # 沒要求的任務：兩台都可以
    assert "資源" in REASONS["resource"]


def test_scheduler_counts_resources_in_use():
    from nexus.tasks.scheduler import evaluate
    from nexus.tests.unit.test_scheduler import NOW, TASK
    caps = {"b": cap(resources={"named": {"gpu": 1}}, max_concurrency=4)}
    rows = evaluate(TASK, NEED, [dev("b")], caps, {"b": 1}, {}, set(), now=NOW, used={"b": {"gpu": 1}})
    assert rows[0]["reasons"] == ["resource"]                              # 唯一一張 GPU 正被別的任務用


def test_used_named_from_running_tasks(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    svc = TaskService(db, Repo(db))
    t = svc.create("test.echo", {"text": "x"}, resources={"gpu": 1})
    svc.transition(t["task_id"], nexus.tasks.states.TaskStatus.SCHEDULED, "test", assigned_device_id="dev-1")
    svc.create("test.echo", {"text": "y"}, resources={"gpu": 1})          # 還在排隊：不算佔用
    sch = Scheduler.__new__(Scheduler)
    sch.tr = svc.tr
    assert sch._used_named() == {"dev-1": {"gpu": 1}}
    db.close()


# ---------- 節點：只送給看得懂的 Master ----------
class _C:
    def __init__(self, peer):
        self.peer_protocol = peer


def _named(peer, named):
    from nexus.worker.agent import WorkerAgent
    a = WorkerAgent.__new__(WorkerAgent)
    a._client, a.cfg = _C(peer), {"named_resources": named}
    return a._named_fields()


def test_worker_sends_named_only_to_new_master():
    assert nexus.version_tuple(nexus.PROTOCOL_VERSION) >= nexus.version_tuple(nexus.NAMED_MIN_PROTOCOL) == (1, 5)
    assert _named("1.5", {"gpu": 1}) == {"named": {"gpu": 1}}
    assert _named("1.4", {"gpu": 1}) == {}              # 舊 Master 會把整份能力丟掉 → 不送
    assert _named("1.5", {}) == {}
    assert _named("1.5", {"GPU": 1}) == {}              # 設定壞掉不送（不能害整份能力被丟）


# ---------- 顯示 ----------
def test_display_shows_named():
    assert nd.isolation_text({"isolation": "basic", "named": {"gpu": 1}}) == "基本（可下載）；資源：gpu×1"

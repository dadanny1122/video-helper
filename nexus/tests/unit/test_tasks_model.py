"""V2 PR1：狀態機、Adapter 清單、Manifest 驗證、migration 0002、TaskRepo"""
import json
import sqlite3
import uuid

import pytest

from nexus.core.models import utcnow
from nexus.storage import migrations
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.storage.task_repo import TaskRepo
from nexus.tasks import adapters, manifest
from nexus.tasks.states import CANCELLABLE, ON_WORKER, TERMINAL, TRANSITIONS, TaskStatus, can_transition

S = TaskStatus


# ---------- 狀態機 ----------
def test_all_19_states_have_transitions():
    assert len(TaskStatus) == 19 and set(TRANSITIONS) == set(TaskStatus)
    for nxt in TRANSITIONS.values():
        assert nxt <= set(TaskStatus)


def test_terminal_states():
    assert TERMINAL == {S.SUCCEEDED, S.REJECTED, S.RESULT_REJECTED, S.CANCELLED}


@pytest.mark.parametrize("cur,new", [
    (S.CREATED, S.VALIDATING), (S.VALIDATING, S.QUEUED), (S.VALIDATING, S.REJECTED), (S.QUEUED, S.SCHEDULED),
    (S.SCHEDULED, S.DISPATCHED), (S.DISPATCHED, S.ACCEPTED), (S.DISPATCHED, S.QUEUED), (S.ACCEPTED, S.PREPARING),
    (S.PREPARING, S.RUNNING), (S.RUNNING, S.VERIFYING), (S.VERIFYING, S.SUCCEEDED), (S.RUNNING, S.CANCELLING),
    (S.CANCELLING, S.CANCELLED), (S.RUNNING, S.TIMED_OUT), (S.TIMED_OUT, S.FAILED), (S.FAILED, S.QUEUED),
    (S.RUNNING, S.UNKNOWN), (S.UNKNOWN, S.FAILED)])
def test_legal(cur, new):
    assert can_transition(cur, new)


@pytest.mark.parametrize("cur,new", [
    (S.SUCCEEDED, S.RUNNING), (S.CANCELLED, S.QUEUED), (S.FAILED, S.RUNNING), (S.QUEUED, S.RUNNING),
    (S.DISPATCHED, S.RUNNING), (S.VERIFYING, S.RUNNING), (S.RUNNING, S.QUEUED), (S.UNKNOWN, S.QUEUED),
    (S.REJECTED, S.QUEUED), (S.TIMED_OUT, S.QUEUED)])
def test_illegal(cur, new):
    """終態不能回 RUNNING；執行中不能直接回佇列（要經 UNKNOWN／FAILED）"""
    assert not can_transition(cur, new)


def test_on_worker_states_can_go_unknown_and_cancellable_ok():
    for s in ON_WORKER:
        assert can_transition(s, S.UNKNOWN)
    for s in CANCELLABLE:
        assert can_transition(s, S.CANCELLED) or can_transition(s, S.CANCELLING)


# ---------- Adapter ----------
def test_only_known_adapters_and_no_shell():
    # V3 起多一個 video.download（有副作用：連網＋寫檔，只在沙箱跑）；永遠不會有 shell／python 類
    assert set(adapters.ADAPTERS) == {"test.echo", "test.sleep", "test.fail", "test.file", "video.download"}
    for name, spec in adapters.ADAPTERS.items():
        want = {"video.download": "network,files", "test.file": "test-file"}.get(name, "none")   # V4 test.file：只寫測試檔
        assert spec.side_effects == want
        assert not any(w in name for w in ("shell", "python", "powershell", "cmd", "exec"))
    assert adapters.needs_sandbox("video.download") and not adapters.needs_sandbox("test.file")


def test_video_download_params_checked_on_master():
    spec = adapters.get("video.download")
    assert adapters.check_params(spec, {"url": "https://www.xvideos.com/video.abc/x"}) == []
    assert adapters.check_params(spec, {"url": "https://www.xvideos.com/video.abc/x", "quality": "720"}) == []
    for bad in ("http://www.xvideos.com/x", "https://jable.tv/videos/x", "https://127.0.0.1/x",
                "https://www.xvideos.com:8080/x", "https://u:p@www.pornhub.com/x"):
        assert adapters.check_params(spec, {"url": bad}), bad
    assert adapters.check_params(spec, {"url": "https://www.xvideos.com/x", "quality": "4k"})
    assert adapters.check_params(spec, {"url": "https://www.xvideos.com/x", "cmd": "dir"})


def test_video_manifest_needs_output_limit_and_allows_two_hours():
    m = manifest.build("video.download", {"url": "https://www.pornhub.com/view_video.php?viewkey=1"})
    assert manifest.validate(m) == []
    assert m["limits"]["max_runtime_seconds"] == 7200 and m["limits"]["max_output_bytes"] == 8 << 30
    del m["limits"]["max_output_bytes"]
    assert manifest.validate(m)
    t = manifest.build("test.sleep", {"seconds": 1})
    assert "max_output_bytes" not in t["limits"] and manifest.validate(t) == []   # 舊節點收到的測試任務不變
    t["limits"]["max_runtime_seconds"] = 1000
    assert manifest.validate(t)                                                  # 測試任務還是最多 900 秒


def test_adapter_params():
    echo = adapters.get("test.echo")
    assert adapters.check_params(echo, {"text": "hi"}) == []
    assert adapters.check_params(echo, {"text": "x" * 1001})
    assert adapters.check_params(echo, {"text": "hi", "cmd": "rm"})           # 不認得的欄位
    assert adapters.check_params(echo, {})
    sl = adapters.get("test.sleep")
    assert adapters.check_params(sl, {"seconds": True})                        # bool 不是 int
    assert adapters.check_params(sl, {"seconds": 0}) and adapters.check_params(sl, {"seconds": 601})
    assert adapters.check_params(adapters.get("test.fail"), {"code": "OOPS"})
    assert adapters.compatible(echo, "1.7") and not adapters.compatible(echo, "2.0")


# ---------- Manifest ----------
def test_manifest_build_validate_and_hash_stable():
    m = manifest.build("test.echo", {"text": "hello"}, idempotency_key="k1")
    assert manifest.validate(m) == []
    h = manifest.sha256(m)
    m2 = json.loads(json.dumps(m))                     # 走過一次 JSON 也一樣
    assert manifest.sha256(m2) == h
    m2["params"]["text"] = "hellO"
    assert manifest.sha256(m2) != h


@pytest.mark.parametrize("mutate,word", [
    (lambda m: m.update(cmd="calc.exe"), "不認得的欄位"),
    (lambda m: m["limits"].update(shell=True), "不認得的欄位"),
    (lambda m: m.update(type="shell.run"), "不支援的任務類型"),
    (lambda m: m.update(adapter_version="2.0"), "不相容"),
    (lambda m: m["limits"].update(max_runtime_seconds=5000), "max_runtime_seconds"),
    (lambda m: m["retry_policy"].update(max_attempts=99), "max_attempts"),
    (lambda m: m["requirements"].update(platform="linux"), "platform"),
    (lambda m: m.update(priority=True), "priority"),
    (lambda m: m.update(idempotency_key=""), "idempotency_key"),
    (lambda m: m.update(task_id="not-a-uuid"), "task_id"),
    (lambda m: m["params"].update(text="x" * 20000), "16KB"),
])
def test_manifest_rejects(mutate, word):
    """AT-005：非法欄位、超大、超範圍都在排程前擋下"""
    m = manifest.build("test.echo", {"text": "hello"})
    mutate(m)
    problems = manifest.validate(m)
    assert problems and any(word in p for p in problems), problems


# ---------- migration 0002 ----------
def test_migration_v1_to_v2_keeps_v1_data(tmp_path, monkeypatch):
    p = str(tmp_path / "n.db")
    from nexus.storage import db as dbmod
    only_v1 = {1: migrations.MIGRATIONS[1]}
    monkeypatch.setattr(dbmod, "MIGRATIONS", only_v1)
    monkeypatch.setattr(dbmod, "LATEST", 1)
    db = Database(p)
    db.migrate()
    with db.tx() as c:
        Repo(db).insert_device(c, dict(device_id="d1", name="pc", platform="windows", agent_version="0.1.0", protocol_version="1.0",
                                       status="ACTIVE", public_key_fingerprint="fp", created_at=utcnow()))
    db.close()
    monkeypatch.setattr(dbmod, "MIGRATIONS", migrations.MIGRATIONS)
    monkeypatch.setattr(dbmod, "LATEST", migrations.LATEST)
    db = Database(p)
    r = db.migrate()
    assert r["from"] == 1 and r["to"] == migrations.LATEST and r["backup"]                  # 有備份
    assert Repo(db).get_device("d1")["name"] == "pc"
    tables = {x[0] for x in db.conn().execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"tasks", "task_attempts", "task_events", "schedule_decisions", "device_capabilities", "uploads"} <= tables


# ---------- TaskRepo ----------
def _task(m, **kw):
    t = dict(task_id=m["task_id"], type=m["type"], adapter_version=m["adapter_version"], manifest_json=json.dumps(m),
             manifest_sha256=manifest.sha256(m), idempotency_key=m["idempotency_key"], status="CREATED", priority=0,
             max_attempts=2, created_by="owner", created_at=utcnow(), updated_at=utcnow(), expires_at=m["expires_at"])
    t.update(kw)
    return t


def test_repo_idempotency_unique_and_optimistic_lock(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    tr = TaskRepo(db)
    m = manifest.build("test.echo", {"text": "a"}, idempotency_key="same")
    with db.tx() as c:
        tr.insert_task(c, _task(m))
    m2 = manifest.build("test.echo", {"text": "a"}, idempotency_key="same")
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        tr.insert_task(c, _task(m2))
    assert tr.task_by_idempotency_key("same")["task_id"] == m["task_id"]
    with db.tx() as c:
        assert tr.update_task(c, m["task_id"], 1, status="VALIDATING")
        assert not tr.update_task(c, m["task_id"], 1, status="QUEUED")      # 舊版本號 → 失敗
    assert tr.get_task(m["task_id"])["status"] == "VALIDATING"
    with pytest.raises(ValueError):
        tr.insert_task(db.conn(), {**_task(manifest.build("test.echo", {"text": "b"})), "evil": 1})


def test_repo_event_dedupe_and_seq_unique(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    tr = TaskRepo(db)
    ev = dict(event_id=str(uuid.uuid4()), task_id="t1", attempt_id="a1", device_id="d1", seq=1, type="RUNNING", data={})
    with db.tx() as c:
        assert tr.insert_event(c, ev)
        assert not tr.insert_event(c, ev)                                   # 同 event_id → 忽略
    with pytest.raises(sqlite3.IntegrityError), db.tx() as c:
        tr.insert_event(c, {**ev, "event_id": str(uuid.uuid4())})           # 同 attempt 同 seq → 拒絕
    assert len(tr.events_of("t1")) == 1


def test_repo_capabilities_roundtrip(tmp_path):
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    tr = TaskRepo(db)
    tr.upsert_capabilities("d1", {"cpu_count": 8}, {"test.echo": "1.0"}, 2, True)
    tr.upsert_capabilities("d1", {"cpu_count": 4}, {"test.echo": "1.0"}, 1, False)
    cap = tr.capabilities("d1")
    assert cap["resources"]["cpu_count"] == 4 and cap["max_concurrency"] == 1 and cap["accepting"] is False


def test_capabilities_isolation_field_validated():
    from nexus.core.errors import NexusError
    from nexus.tasks import capabilities
    ok = capabilities.normalize({"resources": {"cpu_count": 2, "isolation": "basic"}, "adapters": {}})
    assert ok["resources"]["isolation"] == "basic"
    for bad in ({"isolation": "strong"}, {"isolation": ""}, {"isolation_reason": "x" * 300}):
        with pytest.raises(NexusError):
            capabilities.normalize({"resources": {"cpu_count": 2, **bad}, "adapters": {}})

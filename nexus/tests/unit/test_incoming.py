"""V4 PR1：Master 收檔（nexus.master.incoming）——亂序／重複段、雜湊錯、大小錯、檔名攻擊、空間不夠、整檔雜湊錯"""
import hashlib
import io
import os

import pytest

from nexus.core.errors import NexusError
from nexus.master import incoming as inc
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.tasks.service import TaskService

CS = 1024                     # 測試用小段（正式 8 MB）


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(inc, "CHUNK_SIZE", CS)
    monkeypatch.setattr(inc, "SPACE_MARGIN", 0)
    db = Database(str(tmp_path / "n.db"))
    db.migrate()
    ts = TaskService(db, Repo(db))
    dest = tmp_path / "下載"
    i = inc.Incoming(db, str(tmp_path / "incoming"), dest_dir=lambda: str(dest))
    yield db, ts, i, dest
    db.close()


def _prep(db, ts, i, data: bytes, name="影片.mp4", sha=None):
    t = ts.create("test.file", {"size_mb": 1, "return_to_master": True})
    with db.tx() as c:
        i.prepare(c, t["task_id"], "dev-1", [{"name": name, "size": len(data), "sha256": sha or hashlib.sha256(data).hexdigest(),
                                              "path": "C:/node/x.mp4"}])
    return i.of_task(t["task_id"])[0]


def _put(i, row, n, data, sha=None):
    part = data[n * CS:(n + 1) * CS]
    return i.write_chunk(i.get(row["upload_id"]), n, len(part), sha or hashlib.sha256(part).hexdigest(), io.BytesIO(part))


def test_out_of_order_and_duplicate_chunks_then_complete(env):
    db, ts, i, dest = env
    data = os.urandom(CS * 3 + 100)
    row = _prep(db, ts, i, data)
    assert row["chunks_total"] == 4
    for n in (3, 0, 2, 0, 1):                                 # 亂序＋重複
        _put(i, row, n, data)
    assert inc.have_list(i.get(row["upload_id"])) == [0, 1, 2, 3]
    out = i.complete(i.get(row["upload_id"]))
    assert out["ok"] and open(out["master_path"], "rb").read() == data
    assert os.path.dirname(out["master_path"]) == os.path.realpath(dest)
    assert not os.path.exists(i.part_path(row["upload_id"]))           # 半成品搬走了
    assert i.complete(i.get(row["upload_id"]))["master_path"] == out["master_path"]   # 重送 complete：冪等


def test_bad_chunk_hash_not_counted(env):
    db, ts, i, _ = env
    data = os.urandom(CS * 2)
    row = _prep(db, ts, i, data)
    with pytest.raises(NexusError) as e:
        _put(i, row, 0, data, sha="0" * 64)
    assert e.value.code == "CHUNK_MISMATCH"
    assert inc.have_list(i.get(row["upload_id"])) == []


def test_wrong_length_and_bad_index(env):
    db, ts, i, _ = env
    data = os.urandom(CS + 10)
    row = _prep(db, ts, i, data)
    with pytest.raises(NexusError) as e:                     # 最後一段只能 10 bytes
        i.write_chunk(row, 1, CS, "0" * 64, io.BytesIO(b"x" * CS))
    assert e.value.code == "SIZE_MISMATCH"
    with pytest.raises(NexusError) as e:
        i.write_chunk(row, 9, 10, "0" * 64, io.BytesIO(b"x" * 10))
    assert e.value.code == "CHUNK_MISMATCH"
    with pytest.raises(NexusError) as e:                     # 宣告了長度卻沒送完
        i.write_chunk(row, 1, 10, hashlib.sha256(b"x" * 10).hexdigest(), io.BytesIO(b"x" * 3))
    assert e.value.code == "SIZE_MISMATCH"


def test_complete_with_missing_chunks(env):
    db, ts, i, _ = env
    data = os.urandom(CS * 2)
    row = _prep(db, ts, i, data)
    _put(i, row, 0, data)
    with pytest.raises(NexusError) as e:
        i.complete(i.get(row["upload_id"]))
    assert e.value.code == "UPLOAD_INCOMPLETE" and e.value.details["missing"] == [1]


def test_whole_file_hash_mismatch_retry_then_give_up(env):
    db, ts, i, dest = env
    data = os.urandom(CS * 2)
    row = _prep(db, ts, i, data, sha="ab" * 32)              # 節點宣告的雜湊跟內容不合
    for n in (0, 1):
        _put(i, row, n, data)
    with pytest.raises(NexusError) as e:
        i.complete(i.get(row["upload_id"]))
    assert e.value.code == "HASH_MISMATCH" and e.value.details["final"] is False
    assert inc.have_list(i.get(row["upload_id"])) == []        # 清掉、全部重傳
    for n in (0, 1):
        _put(i, row, n, data)
    with pytest.raises(NexusError) as e:
        i.complete(i.get(row["upload_id"]))
    assert e.value.details["final"] is True and i.get(row["upload_id"])["status"] == "failed"
    assert not os.path.exists(dest) or os.listdir(dest) == []   # 下載資料夾裡什麼都沒有


@pytest.mark.parametrize("name", ["..\\..\\evil.mp4", "../../evil.mp4", "CON.mp4", "C:evil.mp4", "a/b\\c.mp4", "x:y.mp4"])
def test_malicious_names_stay_inside(env, name):
    db, ts, i, dest = env
    data = b"v" * 50
    row = _prep(db, ts, i, data, name=name)
    _put(i, row, 0, data)
    out = i.complete(i.get(row["upload_id"]))
    real = os.path.realpath(out["master_path"])
    assert os.path.dirname(real) == os.path.realpath(dest)
    assert os.path.basename(real).upper() != "CON.MP4" and ":" not in os.path.basename(real)


def test_extension_not_allowed(env):
    db, ts, i, dest = env
    data = b"MZ" * 10
    row = _prep(db, ts, i, data, name="木馬.exe")
    _put(i, row, 0, data)
    with pytest.raises(NexusError) as e:
        i.complete(i.get(row["upload_id"]))
    assert e.value.code == "OUTPUT_REJECTED"


def test_no_overwrite(env):
    db, ts, i, dest = env
    os.makedirs(dest)
    (dest / "a.mp4").write_bytes(b"old")
    data = b"new" * 10
    row = _prep(db, ts, i, data, name="a.mp4")
    _put(i, row, 0, data)
    out = i.complete(i.get(row["upload_id"]))
    assert (dest / "a.mp4").read_bytes() == b"old" and out["master_path"].endswith("a (2).mp4")


def test_disk_full_then_ok(env):
    db, ts, i, _ = env
    data = os.urandom(CS)
    row = _prep(db, ts, i, data)
    free = {"n": 0}
    i.free_bytes = lambda p: free["n"]
    with pytest.raises(NexusError) as e:
        i.open_for(i.get(row["upload_id"]))
    assert e.value.code == "MASTER_DISK_FULL" and e.value.retry_after == inc.DISK_WAIT_SECONDS
    assert i.progress(row["task_id"])["disk_full"]
    free["n"] = 10 << 30
    assert i.open_for(i.get(row["upload_id"]))["have"] == []
    assert not i.progress(row["task_id"])["disk_full"]


def test_discard_task_removes_partials(env):
    db, ts, i, _ = env
    data = os.urandom(CS * 2)
    row = _prep(db, ts, i, data)
    _put(i, row, 0, data)
    assert os.path.exists(i.part_path(row["upload_id"]))
    assert i.discard_task(row["task_id"])
    assert not os.path.exists(i.part_path(row["upload_id"]))
    with pytest.raises(NexusError):
        _put(i, row, 1, data)                                  # 放棄的不再收

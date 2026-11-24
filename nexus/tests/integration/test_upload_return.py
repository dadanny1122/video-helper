"""
V4：真的 Worker（test.file）→ 真的 Master，把檔案傳回來
AT-030 正常傳回、AT-031 節點關掉再開接著傳、AT-032 Master 重開接著收、AT-034 別台冒充、AT-036 空間不夠、
AT-037 傳回中取消、D7 24 小時逾時＋重新傳回、D3 保留／刪除、舊協定節點不派、PUT 大小上限與防重放
"""
import hashlib
import http.client
import json
import os
import ssl
import time

import pytest

from nexus.core.config import Config
from nexus.core.errors import NexusError
from nexus.master import incoming as inc
from nexus.tasks import dispatch
from nexus.tests.integration.conftest import Master
from nexus.worker.agent import WorkerAgent
from nexus.worker.credstore import CredStore


def wait_for(pred, timeout=20):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


@pytest.fixture(autouse=True)
def fast(monkeypatch):
    monkeypatch.setattr(inc, "CHUNK_SIZE", 1 << 20)           # 1 MB 一段，測試比較快有好幾段
    monkeypatch.setattr(inc, "SPACE_MARGIN", 0)
    monkeypatch.setattr(inc, "DISK_WAIT_SECONDS", 1)
    monkeypatch.setattr(dispatch, "UPLOAD_NUDGE_SECONDS", 1)


@pytest.fixture
def m(tmp_path):
    mm = Master(tmp_path / "m", auto_dispatch=True, tick=0.2)
    mm.dest = tmp_path / "Master下載"
    mm.dispatcher.incoming.dest_dir = lambda: str(mm.dest)
    mm.returned = []
    mm.dispatcher.on_returned = lambda t, r: mm.returned.append((t, r))
    yield mm
    mm.stop()


@pytest.fixture
def agents(m, tmp_path):
    made = []

    def make(name, fresh=True, **cfg):
        os.environ["VH_NEXUS_ROOT"] = str(tmp_path / name)
        c = Config("worker")
        c["download_dir"] = str(tmp_path / name / "dl")
        for k, v in cfg.items():
            c[k] = v
        c.save()
        a = WorkerAgent(c, CredStore(str(tmp_path / name / "identity")), heartbeat_seconds=1, poll_seconds=0.3)
        a.executor.poll_wait = 1
        a.start()
        if fresh:
            a.pair(m.url, m.registry.pair.new(), name)
            m.registry.approve(a.store.state["device_id"], "owner", "ok")
        assert wait_for(lambda: a.state == "ACTIVE"), a.status()
        made.append(a)
        return a
    yield make
    for a in made:
        a.stop(disconnect=False)


def st(m, tid):
    return m.tasks.get(tid)["status"]


def _sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def test_at030_return_ok_and_node_copy_deleted(m, agents, tmp_path):
    a = agents("pc-a")
    t = m.tasks.create("test.file", {"size_mb": 3, "return_to_master": True})
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30), m.tasks.get(t["task_id"])
    r = json.loads(m.tasks.get(t["task_id"])["result_json"])
    f = r["files"][0]
    assert r["returned"] and os.path.dirname(f["path"]) == os.path.realpath(m.dest)
    assert f["size"] == 3 << 20 and _sha(f["path"]) == f["sha256"]
    assert m.returned and m.returned[0][1]["files"][0]["path"] == f["path"]          # 電腦版會收到通知（寫 ② 下載紀錄）
    assert wait_for(lambda: not os.path.exists(f["node_path"]), 5)                  # D3：驗證後節點那份刪了
    assert not os.listdir(m.dispatcher.incoming.root)                                # 沒有半成品
    states = [x["action"] for x in reversed(m.repo.audit_tail(200, t["task_id"]))]
    assert "task.uploading" in states and states[-1] == "task.succeeded"
    assert a.executor.summary() == []


def test_keep_after_return_and_no_return(m, agents):
    agents("pc-k", keep_after_return=True)
    t = m.tasks.create("test.file", {"size_mb": 1, "return_to_master": True})
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30)
    f = json.loads(m.tasks.get(t["task_id"])["result_json"])["files"][0]
    time.sleep(0.5)
    assert os.path.exists(f["node_path"])                                            # 勾了「留一份」
    t2 = m.tasks.create("test.file", {"size_mb": 1})                                 # 不勾傳回 → 跟 V3 一樣
    assert wait_for(lambda: st(m, t2["task_id"]) == "SUCCEEDED", 30)
    r2 = json.loads(m.tasks.get(t2["task_id"])["result_json"])
    assert not r2.get("returned") and os.path.exists(r2["files"][0]["path"]) and not m.dispatcher.incoming.of_task(t2["task_id"])


def test_at031_worker_restart_resumes(m, agents):
    m.dispatcher.rate_limit_mbps = lambda: 16                  # 2 MB/s → 8 MB 要 4 秒，中途關掉
    writes = []
    orig = m.dispatcher.incoming.write_chunk
    m.dispatcher.incoming.write_chunk = lambda row, n, *a: (writes.append(n), orig(row, n, *a))[1]
    a = agents("pc-r")
    t = m.tasks.create("test.file", {"size_mb": 8, "return_to_master": True})
    assert wait_for(lambda: len(writes) >= 2, 30)
    a.stop(disconnect=False)                                   # 節點關掉（不是撤銷）
    time.sleep(1)
    assert st(m, t["task_id"]) == "UPLOADING"
    m.dispatcher.sweep()
    assert st(m, t["task_id"]) == "UPLOADING"                  # 不會變「狀態不明」
    m.dispatcher.rate_limit_mbps = lambda: 0
    agents("pc-r", fresh=False)                                # 同一個身分再開
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30), m.tasks.get(t["task_id"])
    acts = [(x["action"], x["reason"]) for x in reversed(m.repo.audit_tail(200, t["task_id"]))]
    assert writes.count(0) == 1, (str(writes), str(acts))      # 第 0 段沒有重傳
    f = json.loads(m.tasks.get(t["task_id"])["result_json"])["files"][0]
    assert _sha(f["path"]) == f["sha256"]


def test_at032_master_restart_resumes(m, agents):
    m.dispatcher.rate_limit_mbps = lambda: 16
    agents("pc-m")
    t = m.tasks.create("test.file", {"size_mb": 8, "return_to_master": True})
    assert wait_for(lambda: (m.dispatcher.upload_progress(t["task_id"]) or {}).get("received", 0) >= 2 << 20, 30)
    m.restart()                                                # 記憶體全丟，從磁碟重新載入
    m.dispatcher.incoming.dest_dir = lambda: str(m.dest)
    m.dispatcher.start()
    assert st(m, t["task_id"]) == "UPLOADING"
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 40), m.tasks.get(t["task_id"])
    f = json.loads(m.tasks.get(t["task_id"])["result_json"])["files"][0]
    assert _sha(f["path"]) == f["sha256"]


def test_at034_other_device_cannot_upload(m, agents):
    m.dispatcher.rate_limit_mbps = lambda: 8
    a = agents("pc-1")
    b = agents("pc-2", accept_tasks=False)
    t = m.tasks.create("test.file", {"size_mb": 4, "return_to_master": True})
    assert wait_for(lambda: st(m, t["task_id"]) == "UPLOADING", 30)
    row = m.dispatcher.incoming.of_task(t["task_id"])[0]
    cb = b.executor._new_client()
    with pytest.raises(NexusError) as e:
        cb.upload_open(t["task_id"], m.tasks.get(t["task_id"])["current_attempt_id"], 0)
    assert e.value.code == "UPLOAD_NOT_ALLOWED"
    with pytest.raises(NexusError) as e:
        cb.upload_chunk(row["upload_id"], 0, b"x" * 100)
    assert e.value.code == "UPLOAD_NOT_ALLOWED"
    m.dispatcher.rate_limit_mbps = lambda: 0
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30)
    assert a.store.state["device_id"] == m.tasks.get(t["task_id"])["assigned_device_id"]


def test_at036_master_disk_full_waits(m, agents):
    free = {"n": 0}
    m.dispatcher.incoming.free_bytes = lambda p: free["n"]
    agents("pc-d")
    t = m.tasks.create("test.file", {"size_mb": 1, "return_to_master": True})
    assert wait_for(lambda: (m.dispatcher.upload_progress(t["task_id"]) or {}).get("disk_full"), 30)
    assert st(m, t["task_id"]) == "UPLOADING"
    free["n"] = 100 << 30
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30)


def test_at037_cancel_while_uploading(m, agents):
    m.dispatcher.rate_limit_mbps = lambda: 8
    a = agents("pc-c")
    t = m.tasks.create("test.file", {"size_mb": 6, "return_to_master": True})
    assert wait_for(lambda: (m.dispatcher.upload_progress(t["task_id"]) or {}).get("received", 0) >= 1 << 20, 30)
    m.tasks.cancel(t["task_id"], "owner", "測試取消傳回")
    assert wait_for(lambda: st(m, t["task_id"]) == "CANCELLED", 15), m.tasks.get(t["task_id"])
    assert wait_for(lambda: a.executor.summary() == [], 5)
    assert not [x for x in os.listdir(m.dispatcher.incoming.root) if x.endswith(".part")]
    assert not os.path.exists(m.dest) or not os.listdir(m.dest)


def test_d7_expire_then_retry_return(m, agents):
    import datetime as dt
    m.dispatcher.rate_limit_mbps = lambda: 16
    a = agents("pc-e")
    t = m.tasks.create("test.file", {"size_mb": 6, "return_to_master": True})
    assert wait_for(lambda: st(m, t["task_id"]) == "UPLOADING", 30)
    a.stop(disconnect=False)
    later = dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=25)
    m.dispatcher.sweep(now=later)
    tt = m.tasks.get(t["task_id"])
    assert tt["status"] == "FAILED" and tt["error_code"] == "TRANSFER_EXPIRED"
    with pytest.raises(NexusError):                            # 不是傳回失敗的任務不能「重新傳回」
        m.dispatcher.retry_return(m.tasks.create("test.echo", {"text": "x"})["task_id"])
    m.dispatcher.retry_return(t["task_id"])
    assert st(m, t["task_id"]) == "UPLOADING"
    m.dispatcher.rate_limit_mbps = lambda: 0
    agents("pc-e", fresh=False)
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30), m.tasks.get(t["task_id"])


def test_old_protocol_not_scheduled_for_return(m):
    import datetime as dt

    from nexus.tasks.scheduler import evaluate
    now = dt.datetime.now(dt.timezone.utc)
    dev = {"device_id": "d1", "name": "old", "status": "ACTIVE", "connection_state": "CONNECTED",
           "last_seen_at": now.isoformat().replace("+00:00", "Z"), "protocol_version": "1.2"}
    cap = {"resources": {"isolation": "basic"}, "adapters": {"test.file": "1.0"}, "accepting": True, "max_concurrency": 1}
    task = {"type": "test.file"}
    out = evaluate(task, {"params": {"size_mb": 1, "return_to_master": True}}, [dev], {"d1": cap}, {}, {}, set(), now)
    assert "no_return" in out[0]["reasons"]
    out = evaluate(task, {"params": {"size_mb": 1}}, [dev], {"d1": cap}, {}, {}, set(), now)
    assert "no_return" not in out[0]["reasons"]


def _raw_put(m, a, path, body: bytes, env=None, sha="0" * 64):
    from nexus.protocol.messages import envelope
    certfile, keyfile, pw = a.store.tls_material()
    ctx = ssl.create_default_context(cadata=a.store.ca_pem())
    ctx.load_cert_chain(certfile, keyfile, password=pw)
    c = http.client.HTTPSConnection(m.host, m.port, context=ctx, timeout=10)
    env = env or envelope(f"worker:{a.store.state['device_id']}")
    c.request("PUT", "/api/v1" + path, body=body, headers={"X-VH-Envelope": json.dumps(env), "X-VH-Chunk-SHA256": sha,
                                                         "Content-Type": "application/octet-stream"})
    r = c.getresponse()
    out = r.status, json.loads(r.read() or b"{}"), r.getheader("Connection")
    c.close()
    return out, env


def test_put_limits_and_replay(m, agents):
    m.dispatcher.rate_limit_mbps = lambda: 4
    a = agents("pc-p")
    t = m.tasks.create("test.file", {"size_mb": 3, "return_to_master": True})
    assert wait_for(lambda: st(m, t["task_id"]) == "UPLOADING", 30)
    uid = m.dispatcher.incoming.of_task(t["task_id"])[0]["upload_id"]
    # 超過一段的大小：Master 不讀內容就回 413 並斷線；Windows 上客戶端還在送就被切斷（SSLEOFError／連線重設）也算擋下
    try:
        (s, body, conn), _ = _raw_put(m, a, f"/uploads/{uid}/chunks/0", b"x" * ((1 << 20) + 1))
        assert s == 413 and conn == "close"
    except (ssl.SSLError, ConnectionError):
        pass
    data = b"y" * 100
    (s, body, _), env = _raw_put(m, a, f"/uploads/{uid}/chunks/2", data, sha=hashlib.sha256(data).hexdigest())
    assert body["error"]["code"] == "SIZE_MISMATCH"                                           # 長度跟宣告的對不上
    (s, body, _), _ = _raw_put(m, a, f"/uploads/{uid}/chunks/2", data, env=env)
    assert body["error"]["code"] == "REPLAY_DETECTED"                                         # 同一個外框再送一次
    m.dispatcher.rate_limit_mbps = lambda: 0
    assert wait_for(lambda: st(m, t["task_id"]) == "SUCCEEDED", 30)

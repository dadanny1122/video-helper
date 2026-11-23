"""V2 PR3：心跳 1.1 帶節點能力、舊版 1.0 照樣能心跳、不合格能力丟掉＋稽核、`--profile` 同機多節點"""
import os
import subprocess
import sys

import pytest

from nexus.core import config as nconfig
from nexus.core.config import Config
from nexus.worker.agent import WorkerAgent
from nexus.worker.credstore import CredStore

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))


def wait_for(pred, timeout=10):
    import time
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_agent_heartbeat_reports_capabilities(master, tmp_path):
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path)
    cfg = Config("worker")
    cfg["max_concurrency"] = 2
    a = WorkerAgent(cfg, CredStore(str(tmp_path / "w" / "identity")), heartbeat_seconds=1, poll_seconds=0.3)
    try:
        a.start()
        a.pair(master.url, master.registry.pair.new(), "pc-caps")
        did = a.store.state["device_id"]
        master.registry.approve(did, "owner", "ok")
        assert wait_for(lambda: master.registry.tr.capabilities(did) is not None, 10)
        cap = master.registry.tr.capabilities(did)
        assert cap["max_concurrency"] == 2 and cap["accepting"] is True
        # V3：有沙箱（Windows）的節點多宣稱 video.download；沒有的不宣稱
        from nexus.worker.executor import sandbox_status
        want = {"test.echo", "test.sleep", "test.fail"} | ({"video.download"} if sandbox_status()["isolation"] == "basic" else set())
        assert set(cap["adapters"]) == want
        assert cap["resources"]["cpu_count"] >= 1 and cap["resources"]["memory_total_mb"] > 0
        assert master.repo.get_device(did)["protocol_version"] == "1.2"
        # V3：沙箱等級也回報（Windows＝basic；其他平台＝none＋原因）
        iso = cap["resources"]["isolation"]
        assert iso == sandbox_status()["isolation"] and (iso == "basic" or cap["resources"]["isolation_reason"])
        # 關掉「接收任務」→ 下一次心跳就反映
        cfg["accept_tasks"] = False
        assert wait_for(lambda: master.registry.tr.capabilities(did)["accepting"] is False, 5)
    finally:
        a.stop(disconnect=False)


def test_old_protocol_worker_still_heartbeats_without_capabilities(master, worker_factory):
    """T-04：1.0 的 Worker（V1）照樣心跳，只是沒有能力資料（之後排程會排除它）"""
    w = worker_factory("old").onboard()
    r = w.beat()
    assert r["status"] == "ACTIVE"
    assert master.registry.tr.capabilities(w.device_id) is None


@pytest.mark.parametrize("bad", [
    {"resources": {"cpu_count": 8, "evil": 1}, "adapters": {}},
    {"resources": {"cpu_load": 5}, "adapters": {}},
    {"resources": {}, "adapters": {"rm -rf": "1.0"}},
    {"resources": {}, "adapters": {}, "max_concurrency": 99},
    {"resources": {}, "adapters": {}, "running": ["../../etc"]},
])
def test_malformed_capabilities_are_dropped_but_heartbeat_ok(master, worker_factory, bad):
    w = worker_factory("bad").onboard()
    good = {"resources": {"cpu_count": 4}, "adapters": {"test.echo": "1.0"}}
    w.beat(**good)
    assert master.registry.tr.capabilities(w.device_id) is not None
    r = w.beat(**bad)
    assert r["status"] == "ACTIVE"                                          # 心跳照算
    assert master.registry.tr.capabilities(w.device_id) is None             # 能力資料丟掉 → 不會被派任務
    assert "capabilities.rejected" in master.audit_actions(w.device_id)


def test_profile_dirs_and_lock(tmp_path, monkeypatch):
    from nexus.worker.tray import ProfileBusy, lock_profile
    monkeypatch.setenv("VH_NEXUS_ROOT", str(tmp_path))
    try:
        nconfig.set_worker_profile(None)
        assert nconfig.mode_dir("worker").endswith(os.sep + "worker")
        nconfig.set_worker_profile("p1")
        d1 = nconfig.mode_dir("worker")
        assert d1.endswith("worker-p1") and Config("worker").path.startswith(d1)
        assert CredStore().dir.startswith(d1)
        h = lock_profile()
        with pytest.raises(ProfileBusy):
            lock_profile()                                                  # 同一個 profile 開第二個 → 拒絕
        nconfig.set_worker_profile("p2")
        h2 = lock_profile()                                                 # 不同 profile → 可以
        h.close()
        h2.close()
        for bad in ("../x", "a b", "", "x" * 30):
            with pytest.raises(ValueError):
                nconfig.set_worker_profile(bad)
    finally:
        nconfig.set_worker_profile(None)


def test_two_profiles_register_as_two_independent_devices(master, tmp_path):
    """D3：同一台電腦用 --profile 開兩個節點 → 兩個獨立身分（不同金鑰、不同資料夾）"""
    root = tmp_path / "wroot"
    env = dict(os.environ, VH_NEXUS_ROOT=str(root))
    for prof in ("a", "b"):
        code = master.registry.pair.new()
        script = ("import sys, nexus.worker.tray as t; "
                  f"sys.exit(t.main(['--profile','{prof}','--headless','--master',{master.url!r},'--pair','{code}','--name','pc-{prof}','--once']))")
        p = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=60, env=env, cwd=ROOT)
        assert p.returncode == 0, p.stderr[-1500:]
        assert os.path.isfile(root / f"worker-{prof}" / "worker.log")
    devs = {d["name"]: d for d in master.repo.list_devices()}
    assert {"pc-a", "pc-b"} <= set(devs)
    assert devs["pc-a"]["public_key_fingerprint"] != devs["pc-b"]["public_key_fingerprint"]
    assert not os.path.exists(root / "worker")                              # 沒有碰到預設節點的資料夾

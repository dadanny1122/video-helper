"""Worker Agent 對真 Master：配對→等核准→心跳→更新→撤銷停止；Master 重啟後重連；CA 換掉就拒絕（N-03／N-06／N-09／N-10）"""
import os
import subprocess
import sys
import time

import pytest

from nexus.core.config import Config
from nexus.worker.agent import WorkerAgent
from nexus.worker.credstore import CredStore


def wait_for(pred, timeout=10, step=0.1):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(step)
    return False


@pytest.fixture
def agent(master, tmp_path):
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path)
    cfg = Config("worker")
    a = WorkerAgent(cfg, CredStore(str(tmp_path / "worker" / "identity")), heartbeat_seconds=1, poll_seconds=0.3)
    yield a
    a.stop(disconnect=False)


def test_full_lifecycle_pair_approve_heartbeat_revoke(master, agent):
    agent.start()
    assert wait_for(lambda: agent.state == "UNCONFIGURED")
    from nexus.core.errors import NexusError
    with pytest.raises(NexusError):                        # 錯的配對碼
        agent.pair(master.url, "000000", "pc-a")
    code = master.registry.pair.new()
    r = agent.pair(master.url, code, "pc-a")
    assert agent.state == "PENDING" and agent.master_fingerprint_seen
    assert agent.status()["master_ca_fingerprint"] == r["master_ca_fingerprint"][:16]
    did = r["device_id"]
    time.sleep(0.8)                                        # 輪詢中、還是 PENDING
    assert agent.state == "PENDING" and master.repo.get_device(did)["status"] == "PENDING"
    master.registry.approve(did, "owner", "測試")
    assert wait_for(lambda: agent.state == "ACTIVE" and agent.detail == "心跳正常", 10), agent.status()
    assert agent.store.has_credential() and agent.store.state["registration_token"] is None
    assert wait_for(lambda: master.repo.get_device(did)["last_sequence"] >= 2, 10)
    # 撤銷 → Agent 停止、憑證刪掉、不再心跳
    master.registry.revoke(did, "owner", "測試撤銷")
    assert wait_for(lambda: agent.state == "REVOKED", 10), agent.status()
    assert not os.path.isfile(agent.store.cert_path) and agent.store.state["status"] == "REVOKED"
    seq = master.repo.get_device(did)["last_sequence"]
    time.sleep(2.5)
    assert master.repo.get_device(did)["last_sequence"] == seq
    # 重新配對＝新身分
    code = master.registry.pair.new()
    r2 = agent.pair(master.url, code, "pc-a")
    assert r2["device_id"] != did and agent.state == "PENDING"


def test_reconnect_after_master_restart_and_refuse_changed_ca(master, agent):
    agent.start()
    agent.pair(master.url, master.registry.pair.new(), "pc-b")
    did = agent.store.state["device_id"]
    master.registry.approve(did, "owner", "ok")
    assert wait_for(lambda: agent.state == "ACTIVE", 10)
    master.restart()
    assert wait_for(lambda: agent.state in ("ACTIVE", "RECONNECTING"), 5)
    assert wait_for(lambda: agent.state == "ACTIVE" and master.repo.get_device(did)["connection_state"] == "CONNECTED", 20), agent.status()
    # Master 的 CA 被換掉（例如有人冒充）→ Worker 停止、不自動信任
    master.server.stop()
    import shutil

    from nexus.identity.ca import MasterCA
    shutil.rmtree(master.ca.dir)
    master.ca = MasterCA(master.ca.dir)
    master.ca.ensure(["127.0.0.1"])
    master.registry.ca = master.ca
    master.server.start()
    assert wait_for(lambda: agent.state == "MASTER_CHANGED", 20), agent.status()


def test_renew_when_recommended(master, agent):
    import datetime as dt
    agent.start()
    agent.pair(master.url, master.registry.pair.new(), "pc-c")
    did = agent.store.state["device_id"]
    master.registry.approve(did, "owner", "ok")
    assert wait_for(lambda: agent.state == "ACTIVE", 10)
    from nexus.identity import certs
    old_fp = certs.cert_fingerprint(agent.store.cert())
    soon = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=3)).isoformat(timespec="seconds").replace("+00:00", "Z")
    master.repo.db.conn().execute("UPDATE credentials SET expires_at=? WHERE device_id=?", (soon, did))
    assert wait_for(lambda: [x["status"] for x in master.repo.credentials_of(did)] == ["SUPERSEDED", "ACTIVE"], 10)
    assert wait_for(lambda: certs.cert_fingerprint(agent.store.cert()) != old_fp, 20)  # Windows CI 偶爾慢，5 秒會誤報
    assert wait_for(lambda: master.repo.get_device(did)["last_sequence"] >= 3, 10)   # 新憑證繼續心跳


def test_worker_mode_does_not_touch_downloader_state(master, tmp_path):
    """N-09／N-10：Worker 程序不載入下載器、不讀寫 settings.json／history.json；headless 子程序真的能註冊"""
    root = tmp_path / "wroot"
    env = dict(os.environ, VH_NEXUS_ROOT=str(root), APPDATA=str(tmp_path / "appdata"))
    code = master.registry.pair.new()
    script = ("import sys, runpy; sys.argv=['w','--headless','--master',%r,'--pair',%r,'--name','sub','--once']; "
              "runpy.run_module('nexus.worker', run_name='__main__'); " % (master.url, code))
    script2 = "import sys; assert 'video_helper_app' not in sys.modules, 'worker imported the downloader'; print('clean')"
    p = subprocess.run([sys.executable, "-c", script.replace("runpy.run_module('nexus.worker', run_name='__main__'); ",
                                                              "import nexus.worker.tray as t; t.main(sys.argv[1:]); " + script2)],
                       capture_output=True, text=True, timeout=60, env=env,
                       cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))))
    assert p.returncode == 0 and "clean" in p.stdout, p.stderr[-2000:]
    devs = master.repo.list_devices()
    assert any(d["name"] == "sub" and d["status"] == "PENDING" for d in devs)
    assert os.path.isfile(root / "worker" / "worker.log") and os.path.isfile(root / "worker" / "config.json")
    assert not os.path.exists(tmp_path / "appdata" / "VideoHelper" / "settings.json")
    assert not os.path.exists(root / "master")
    logtxt = open(root / "worker" / "worker.log", encoding="utf-8").read()
    assert code not in logtxt and "PRIVATE KEY" not in logtxt


def test_repair_while_active_uses_new_identity_and_never_mixes_certs(master, agent):
    """使用者實測踩到的：已核准後又配對一次 → 舊憑證留著、新身分對不上 → CREDENTIAL_MISMATCH 一直重連；
    名單出現多筆同指紋。修好後：重配＝新金鑰、舊憑證刪掉；新的那筆被撤銷時 Agent 停下，不會拿舊憑證亂連。"""
    agent.start()
    agent.pair(master.url, master.registry.pair.new(), "pc-r")
    a_id, a_fp = agent.store.state["device_id"], agent.store.public_fingerprint()
    master.registry.approve(a_id, "owner", "ok")
    assert wait_for(lambda: agent.state == "ACTIVE" and agent.detail == "心跳正常", 10)
    # 已連線時又配對一次
    agent.pair(master.url, master.registry.pair.new(), "pc-r")
    b_id = agent.store.state["device_id"]
    assert b_id != a_id and agent.store.public_fingerprint() != a_fp     # 新身分
    assert not os.path.isfile(agent.store.cert_path)                      # 舊憑證刪掉了
    fps = [d["public_key_fingerprint"] for d in master.repo.list_devices()]
    assert len(fps) == len(set(fps))                                     # 名單沒有重複指紋
    # 在等核准時被拒絕 → Agent 回到「尚未配對」，不會拿舊憑證出現 CREDENTIAL_MISMATCH 重連迴圈
    master.registry.reject(b_id, "owner", "測試")
    assert wait_for(lambda: agent.state == "UNCONFIGURED", 10), agent.status()
    # 舊的 A 也撤銷（使用者名單上那幾筆舊的）
    master.registry.revoke(a_id, "owner", "清掉舊的")
    time.sleep(1.5)
    assert agent.last_error != "CREDENTIAL_MISMATCH" and agent.state != "RECONNECTING"
    # 再配一次、核准、撤銷 → 正常停下
    agent.pair(master.url, master.registry.pair.new(), "pc-r")
    c_id = agent.store.state["device_id"]
    master.registry.approve(c_id, "owner", "ok")
    assert wait_for(lambda: agent.state == "ACTIVE" and agent.detail == "心跳正常", 10), agent.status()
    master.registry.revoke(c_id, "owner", "測試")
    assert wait_for(lambda: agent.state == "REVOKED", 10), agent.status()


def test_stale_cert_is_not_a_credential(master, agent):
    agent.start()
    agent.pair(master.url, master.registry.pair.new(), "pc-s")
    master.registry.approve(agent.store.state["device_id"], "owner", "ok")
    assert wait_for(lambda: agent.store.has_credential(), 10)
    agent.store.save(device_id="00000000-0000-0000-0000-000000000000")  # 身分跟憑證不一致
    assert not agent.store.has_credential()


def test_bad_input_does_not_wipe_existing_identity(master, agent):
    """v1.30.1：已配對的節點，配對時位址空白或配對碼格式錯 → 不能先把現在的身分清掉（會在 Master 留下孤兒紀錄）"""
    agent.start()
    r = agent.pair(master.url, master.registry.pair.new(), "pc-keep")
    master.registry.approve(r["device_id"], "owner", "測試")
    assert wait_for(lambda: agent.state == "ACTIVE", 10), agent.status()
    for url, code in (("", master.registry.pair.new()), ("http://127.0.0.1:1", "123456"), (master.url, "12a456")):
        with pytest.raises(ValueError):
            agent.pair(url, code, "pc-keep")
    assert agent.store.has_credential() and agent.store.state["device_id"] == r["device_id"]
    assert wait_for(lambda: agent.state == "ACTIVE", 10), agent.status()
    # 照抄畫面上的「945 083」（中間有空格）也要能配對
    code = master.registry.pair.new()
    r2 = agent.pair(master.url, f"{code[:3]} {code[3:]}", "pc-keep")
    assert r2["device_id"] != r["device_id"]

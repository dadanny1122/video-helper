"""
PR7：MasterService 生命週期 ＋ 🖧 節點分頁（AT-022）。
GUI 的部分需要顯示器（Linux 用 xvfb-run；沒有就跳過），其他照常。
"""
import os
import socket

import pytest

from nexus.core.config import Config
from nexus.master.service import MasterService
from nexus.worker.agent import WorkerAgent
from nexus.worker.credstore import CredStore

RULES = {"register": (5, 60), "registration_poll": (12, 60), "heartbeat": (200, 1), "renew": (20, 60), "disconnect": (6, 60),
         "info": (30, 60)}


def _port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


@pytest.fixture
def svc(tmp_path):
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path)
    cfg = Config("master")
    cfg["bind_host"], cfg["port"] = "127.0.0.1", _port()
    cfg.save()
    s = MasterService(cfg, rules=RULES)
    yield s
    s.close()


def test_service_default_off_and_lifecycle(svc):
    assert svc.cfg["enabled"] is False            # 預設不開埠
    assert not svc.running
    assert svc.devices() == []                    # 沒啟動也能看名單（空）
    fp = svc.ca_fingerprint()                     # 沒啟動就能先拿指紋核對
    host, port = svc.start()
    assert svc.running and svc.ca_fingerprint() == fp
    svc.stop()
    assert not svc.running
    s2 = socket.socket()
    s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s2.bind((host, port))                         # 停用後埠要釋放（REUSEADDR：略過 TIME_WAIT，只看有沒有人在聽）
    s2.close()
    svc.start()
    assert svc.running and svc.ca_fingerprint() == fp   # 重啟同一個 CA


def test_service_bad_bind_raises(tmp_path):
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path)
    cfg = Config("master")
    cfg["bind_host"], cfg["port"] = "8.8.8.8", _port()
    s = MasterService(cfg, rules=RULES)
    with pytest.raises(Exception):
        s.start()
    assert not s.running and "8.8.8.8" in s.error
    s.close()


def _display_ok():
    import tkinter as tk
    try:
        r = tk.Tk()
        r.destroy()
        return True
    except tk.TclError:
        return False


@pytest.mark.skipif(not _display_ok(), reason="需要顯示器")
def test_devices_tab_approve_revoke(svc, tmp_path, monkeypatch):
    """AT-022：分頁啟用 → 產生配對碼 → 真 Worker 配對 → 核准 → 心跳 → 撤銷 → Worker 停下"""
    import tkinter as tk
    from tkinter import ttk

    from nexus.gui import devices_tab as dt

    root = tk.Tk()
    root.withdraw()
    page = ttk.Frame(root)
    page.pack()
    C = {"muted": "#888", "ok": "#2a2", "bad": "#c33", "accent": "#57C8FF", "bg": "#fff"}
    monkeypatch.setattr(dt.messagebox, "askyesno", lambda *a, **k: True)
    monkeypatch.setattr(dt.simpledialog, "askstring", lambda *a, **k: "測試理由")
    tab = dt.DevicesTab(page, svc, {}, C)
    assert not svc.running and tab.b["approve"].instate(["disabled"])

    tab.enabled.set(True)
    tab.toggle()
    assert svc.running and svc.cfg["enabled"] is True
    tab.new_pair()
    code = tab.pair_lbl.cget("text").replace(" ", "")
    assert len(code) == 6

    # 真的 Worker（獨立資料夾）
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path / "w")
    wcfg = Config("worker")
    wcfg["heartbeat_seconds"] = 1
    agent = WorkerAgent(wcfg, CredStore(), poll_seconds=0.3)
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path)
    agent.pair(f"https://127.0.0.1:{svc.port}", code, "pc-gui")
    agent.start()
    tab.refresh()
    assert [d["status"] for d in svc.devices()] == ["PENDING"]
    tab.tree.selection_set(tab.tree.get_children()[0])
    tab._update_buttons()
    assert not tab.b["approve"].instate(["disabled"])
    tab.act("approve")
    _wait(lambda: agent.state == "ACTIVE" and agent.detail == "心跳正常", 8)
    tab.refresh()
    assert tab.tree.item(tab.tree.get_children()[0])["values"][1] == "已核准"

    tab.tree.selection_set(tab.tree.get_children()[0])
    tab._update_buttons()
    tab.act("revoke")
    _wait(lambda: agent.state == "REVOKED", 8)
    actions = [e["action"] for e in svc.audit(50)]
    assert "device.approved" in actions and "device.revoked" in actions

    tab.enabled.set(False)
    tab.toggle()
    assert not svc.running and svc.cfg["enabled"] is False
    agent.stop(disconnect=False)
    tab._stop_timer()
    root.destroy()


def _wait(cond, timeout):
    import time
    t0 = time.time()
    while time.time() - t0 < timeout:
        if cond():
            return
        time.sleep(0.1)
    raise AssertionError("等太久")

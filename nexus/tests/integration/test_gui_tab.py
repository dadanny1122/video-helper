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
    with pytest.raises(RuntimeError):
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


@pytest.mark.skipif(not _display_ok(), reason="需要顯示器")
def test_tasks_tab_create_run_detail_cancel(svc, tmp_path, monkeypatch):
    """V2 PR7：📋 節點任務——新增測試任務 → 真 Worker 執行 → 成功；詳情看得到「選中」與時間軸；取消執行中的任務"""
    import tkinter as tk
    from tkinter import ttk

    from nexus.gui import tasks_tab as tt

    root = tk.Tk()
    root.withdraw()
    page = ttk.Frame(root)
    page.pack()
    C = {"muted": "#888", "ok": "#2a2", "bad": "#c33", "accent": "#57C8FF", "bg": "#fff"}
    tab = tt.TasksTab(page, svc, C)
    assert tab.new_btn.instate(["disabled"])                          # Master 沒開 → 不能派
    svc.start()
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path / "w")
    wcfg = Config("worker")
    agent = WorkerAgent(wcfg, CredStore(), heartbeat_seconds=1, poll_seconds=0.3)
    agent.executor.poll_wait = 2
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path)
    try:
        agent.start()
        agent.pair(f"https://127.0.0.1:{svc.port}", svc.registry.pair.new(), "pc-tasks")
        svc.registry.approve(agent.store.state["device_id"], "owner", "ok")
        _wait(lambda: svc.registry.tr.capabilities(agent.store.state["device_id"]) is not None, 10)
        tab.refresh()
        assert not tab.new_btn.instate(["disabled"])
        # 用對話框新增
        dlg = tt.NewTaskDialog(page, tab, lambda n: n)
        assert dlg.types[dlg.type.get()] == "video.download"              # V3：下載影片排第一個
        dlg.type.set(next(k for k, v in dlg.types.items() if v == "test.echo"))
        dlg._switch()
        dlg.pv.delete(0, "end")
        dlg.pv.insert(0, "GUI 測試")
        key = dlg.key.get()
        dlg.ok()
        t = svc.tasks.list()[0]
        _wait(lambda: svc.tasks.get(t["task_id"])["status"] == "SUCCEEDED", 15)
        tab.refresh()
        assert tab.tree.item(tab.tree.get_children()[0])["values"][2] == "成功"
        tab.tree.selection_set(tab.tree.get_children()[0])
        win = tab.show_detail()
        rows = [win.why.winfo_children()[1].item(i)["values"] for i in win.why.winfo_children()[1].get_children()]
        assert rows and rows[0][1] == "✔ 選中"
        win.win.destroy()
        # 同一個檢查碼再送一次 → 不會多一個
        tab.submit("test.echo", {"text": "GUI 測試"}, idempotency_key=key, max_runtime_seconds=30, max_attempts=2)
        assert len(svc.tasks.list()) == 1
        # 取消執行中的等待任務
        t2 = tab.submit("test.sleep", {"seconds": 60})
        _wait(lambda: svc.tasks.get(t2["task_id"])["status"] == "RUNNING", 10)
        tab.refresh()
        iid = next(i for i, r in tab._rows.items() if r["task_id"] == t2["task_id"])
        tab.tree.selection_set(iid)
        tab._update_buttons()
        monkeypatch.setattr(tt.simpledialog, "askstring", lambda *a, **k: "測試取消")
        monkeypatch.setattr(tt.messagebox, "showinfo", lambda *a, **k: None)
        tab.cancel()
        _wait(lambda: svc.tasks.get(t2["task_id"])["status"] == "CANCELLED", 10)
    finally:
        agent.stop(disconnect=False)
        tab._stop_timer()
        root.destroy()


def test_new_download_task_dialog_and_result_text(svc, monkeypatch):
    """V3 PR7：新增「下載影片（節點）」：貼網址＋選畫質 → 建出 video.download；網址不合格就被拒；結果顯示檔名、大小、存在哪"""
    import json
    import tkinter as tk
    from tkinter import messagebox, ttk

    from nexus.gui import tasks_tab as tt

    root = tk.Tk()
    root.withdraw()
    page = ttk.Frame(root)
    page.pack()
    C = {"muted": "#888", "ok": "#2a2", "bad": "#c33", "accent": "#57C8FF", "bg": "#fff"}
    svc.start()
    warned = []
    monkeypatch.setattr(messagebox, "showwarning", lambda *a, **k: warned.append(a))
    try:
        tab = tt.TasksTab(page, svc, C)
        dlg = tt.NewTaskDialog(page, tab, lambda n: n)
        assert dlg.types[dlg.type.get()] == "video.download" and int(dlg.runtime.get()) == 7200
        dlg.pv.insert(0, "https://www.xvideos.com/video.abc123/some_title")
        dlg.quality.set("720p")
        dlg.ok()
        t = svc.tasks.list()[0]
        m = json.loads(t["manifest_json"])
        assert t["type"] == "video.download" and m["params"] == {"url": "https://www.xvideos.com/video.abc123/some_title",
                                                                    "quality": "720"}
        assert m["limits"]["max_output_bytes"] == 8 << 30
        assert tt.summary_of(t).startswith("xvideos.com／some_title") and "720p" in tt.summary_of(t)
        # 只擋廣告的網站 → 建立就被拒（REJECTED），不會派出去
        dlg = tt.NewTaskDialog(page, tab, lambda n: n)
        dlg.pv.insert(0, "https://jable.tv/videos/abc/")
        dlg.ok()
        assert warned and svc.tasks.list()[0]["status"] == "REJECTED"
        path = "C:\\Users\\me\\Downloads\\VideoHelper 節點\\Clip [abc].mp4"
        files = [{"name": "Clip [abc].mp4", "size": 3 << 20, "sha256": "0" * 64, "path": path}]
        txt = tt.DetailWindow._download_result(json.dumps({"site": "XVideos", "title": "Clip", "files": files}), "pc-a")
        assert "Clip [abc].mp4" in txt and "3.0 MB" in txt and "pc-a" in txt and "VideoHelper 節點" in txt
    finally:
        root.destroy()


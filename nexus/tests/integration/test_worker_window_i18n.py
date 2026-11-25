"""
英文版（v1.45.0）：節點小視窗（--worker）在英文模式下畫面上沒有中文；中文模式照舊是中文。
用假的 WorkerAgent（不連網），開視窗 → 掃所有元件的字 → 關掉。需要顯示器（Linux 用 xvfb-run；沒有就跳過）。
"""
import os
import re
import threading

import pytest

CJK = re.compile(r"[㐀-鿿豈-﫿]")


def _display_ok():
    try:
        import tkinter as tk
        r = tk.Tk()
        r.destroy()
        return True
    except Exception:
        return False


class _Store:
    state = {"status": "ACTIVE", "device_id": "d1", "master_url": "https://127.0.0.1:47331"}


class _Exec:
    def summary(self):
        return [{"type": "video.download", "progress": 0.4, "status": ""}, {"type": "upload", "progress": 0.1, "status": "retrying"}]


class FakeAgent:
    def __init__(self, cfg, store):
        self.store, self.executor, self.on_state = _Store(), _Exec(), None

    def status(self):
        return {"state": "ACTIVE", "detail": "心跳正常", "my_fingerprint": "ab12cd34ef56ab12", "master_ca_fingerprint": "0011223344556677",
                "master_server_fingerprint": "", "credential_expires_at": "2027-02-20T00:00:00Z"}

    def start(self):
        pass

    def stop(self):
        pass


def _texts(w, out):
    try:
        t = w.cget("text")
        if isinstance(t, str) and t:
            out.append(t)
    except Exception:
        pass
    for c in w.winfo_children():
        _texts(c, out)
    return out


def _run(tmp_path, monkeypatch, lang):
    import tkinter as tk

    from nexus.core.config import Config
    from nexus.worker import tray
    os.environ["VH_NEXUS_ROOT"] = str(tmp_path / lang)
    cfg = Config("worker")
    cfg["lang"] = lang
    cfg.save()
    monkeypatch.setattr(tray, "WorkerAgent", FakeAgent)
    seen = {}

    def fake_loop(self):
        for _ in range(20):
            self.update()
        threading.Event().wait(0.8)            # 讓 tk_translate 的 0.7 秒那一輪也跑到
        for _ in range(20):
            self.update()
        seen["title"] = self.title()
        seen["texts"] = _texts(self, [])
        self.destroy()
    monkeypatch.setattr(tk.Tk, "mainloop", fake_loop)
    tray.run_window()
    return seen


@pytest.mark.skipif(not _display_ok(), reason="需要顯示器")
def test_worker_window_english(tmp_path, monkeypatch):
    s = _run(tmp_path, monkeypatch, "en")
    left = [t for t in s["texts"] + [s["title"]] if CJK.search(t.replace("🌐 中文", ""))]
    assert not left, f"英文模式還有中文：{left}"
    joined = "\n".join(s["texts"])
    assert "Master address" in joined and "Status: Connected" in joined and "Download video 40%" in joined
    assert s["title"].startswith("Video Helper node (Worker)")


@pytest.mark.skipif(not _display_ok(), reason="需要顯示器")
def test_worker_window_chinese(tmp_path, monkeypatch):
    s = _run(tmp_path, monkeypatch, "zh")
    joined = "\n".join(s["texts"])
    assert "Master 位址" in joined and "狀態：已連線" in joined and "🌐 English" in joined

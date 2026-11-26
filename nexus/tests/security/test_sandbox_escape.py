"""
APEX Phase 1 PR1：沙箱「目前已知擋不住」的情況（docs/apex/00-Phase0盤點報告.md §3）。

這裡的測試寫的是「應該要擋住」；現在擋不住，所以標 xfail(strict=True)：
  ・現在：測試失敗 → 顯示 xfail（CI 綠燈），等於把已知缺口寫成文件
  ・以後真的擋住了：測試通過 → strict 讓它變紅，提醒把 xfail 拿掉、更新威脅模型
前提都是「子程序裡已經有惡意程式碼在跑」（例如 ffmpeg 解析惡意影片被打進去）。正常的 yt-dlp 不會這樣做。
"""
import os
import socket
import subprocess
import sys
import threading

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
GAP = "已知缺口（Phase 0 §3）：沙箱子程序跟節點同一個 Windows 身分，網路檢查只在 Python 層"


class Listener:
    """本機 127.0.0.1 的測試埠：記錄有沒有人連進來"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        self.t = threading.Thread(target=self._run, daemon=True)
        self.t.start()

    def _run(self):
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
                self.hits += 1
                c.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self.t.join(2)
        self.sock.close()


@pytest.fixture
def listener():
    lst = Listener()
    yield lst
    lst.close()


# 在全新的子程序裡裝上 install_net_guard，再用各種方式連本機測試埠（不污染測試程序自己的 socket）
GUARD_THEN = (
    "import sys;sys.path.insert(0,{root!r});"
    "from nexus.sandbox import video;video.install_net_guard(resolve_override=lambda *_:['93.184.216.34']);"
    "import socket,_socket;s=socket.socket();s.settimeout(3);{call}"
)


def _run_guarded(call: str, port: int):
    code = GUARD_THEN.format(root=ROOT, call=call.format(port=port))
    return subprocess.run([sys.executable, "-I", "-c", code], capture_output=True, text=True, timeout=30)


def test_guard_blocks_plain_connect(listener):
    """對照組：一般的 connect 擋得住（現有功能，應該一直通過）"""
    _run_guarded("s.connect(('127.0.0.1',{port}))", listener.port)
    assert listener.hits == 0


@pytest.mark.xfail(strict=True, reason=GAP + "：connect_ex 沒被換掉")
def test_guard_blocks_connect_ex(listener):
    _run_guarded("s.connect_ex(('127.0.0.1',{port}))", listener.port)
    assert listener.hits == 0


@pytest.mark.xfail(strict=True, reason=GAP + "：直接呼叫 _socket 的 connect 繞過")
def test_guard_blocks_low_level_socket(listener):
    _run_guarded("_socket.socket.connect(s,('127.0.0.1',{port}))", listener.port)
    assert listener.hits == 0


# ---------- 以下只在 Windows：跟沙箱一模一樣的方式開子程序（同一個 Job 設定、同一套環境變數白名單） ----------
win_only = pytest.mark.skipif(sys.platform != "win32", reason="沙箱只支援 Windows")


def _spawn_like_sandbox(tmp_path, code: str) -> int:
    from nexus.sandbox import jobs
    from nexus.sandbox.workspace import Workspace, safe_env
    ws = Workspace(str(tmp_path / "sandbox"))
    with jobs.Job(jobs.JobLimits()) as job:
        p = job.spawn([sys.executable, "-I", "-c", code], cwd=ws.tmp, env=safe_env(ws),
                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        rc = p.wait(30)
        job.wait_empty(5)
    ws.cleanup()
    return rc


@win_only
@pytest.mark.xfail(strict=True, reason=GAP + "：子程序讀得到節點資料夾（私鑰就在這種位置）")
def test_child_cannot_read_node_identity_dir(tmp_path):
    # 模擬 <節點資料夾>/identity/device.key：放在沙箱根目錄旁邊（真實佈局：<base>/sandbox 與 <base>/identity）
    ident = tmp_path / "identity"
    ident.mkdir()
    secret = ident / "device.key"
    secret.write_text("not-a-real-key")
    rc = _spawn_like_sandbox(tmp_path, f"open({str(secret)!r}).read()")
    assert rc != 0, "子程序讀到了節點資料夾裡的檔案"


@win_only
@pytest.mark.xfail(strict=True, reason=GAP + "：子程序在 OS 層連得到本機（Master 常在同一台）")
def test_child_cannot_reach_loopback_at_os_level(tmp_path, listener):
    code = f"import socket;s=socket.socket();s.settimeout(3);s.connect_ex(('127.0.0.1',{listener.port}))"
    _spawn_like_sandbox(tmp_path, code)
    assert listener.hits == 0, "子程序連到了 127.0.0.1"

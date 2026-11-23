"""V3 PR4：SandboxRunner 實測（Job Object＋工作資料夾＋子程序）。只在 Windows。"""
import os
import sys
import threading
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="沙箱只支援 Windows")

if sys.platform == "win32":
    from nexus.sandbox.runner import SandboxRunner


@pytest.fixture
def runner(tmp_path, monkeypatch):
    monkeypatch.setenv("VH_BRIDGE_TOKEN", "secret-token-should-not-leak")
    return SandboxRunner(str(tmp_path / "nexus"), str(tmp_path / "dl"))


def probe(**params):
    return {"type": "sandbox.probe", "params": params}


def no_workdirs(r):
    base = os.path.join(r.root, "work")
    return not os.path.isdir(base) or os.listdir(base) == []


def test_write_succeeds_and_delivers(runner):
    r = runner.run(probe(mode="write", bytes=2048, name="clip.mp4"))
    assert r.ok, r
    assert r.files[0]["name"] == "clip.mp4" and r.files[0]["size"] == 2048 and len(r.files[0]["sha256"]) == 64
    assert os.path.isfile(os.path.join(runner.dest_dir, "clip.mp4"))
    assert r.cleanup_ok and no_workdirs(runner)


def test_progress_events_and_cancel_stops_everything(runner):
    seen, cancel = [], threading.Event()

    def on_event(ev):
        seen.append(ev)
        if len(seen) >= 3:
            cancel.set()
    r = runner.run(probe(mode="sleep", seconds=60), on_event=on_event, cancel=cancel)
    assert r.status == "cancelled" and r.leftover_processes == 0 and len(seen) >= 3
    assert no_workdirs(runner)


def test_cancel_kills_grandchild_too(runner):
    cancel = threading.Event()
    threading.Timer(3, cancel.set).start()
    t0 = time.time()
    r = runner.run(probe(mode="spawn", seconds=60), cancel=cancel)
    assert r.status == "cancelled" and r.leftover_processes == 0, r
    assert time.time() - t0 < 30
    assert no_workdirs(runner)


def test_timeout(runner):
    r = runner.run(probe(mode="sleep", seconds=60), timeout=2)
    assert r.status == "timed_out" and r.leftover_processes == 0


def test_env_whitelist_inside_child(runner):
    r = runner.run({**probe(mode="env"), "deliver": False})
    assert r.ok, r
    keys = set(r.result["env_keys"])
    assert "VH_BRIDGE_TOKEN" not in keys and "VH_SANDBOX" in keys
    assert not any(k.upper().endswith(("TOKEN", "KEY", "SECRET")) for k in keys), keys


def test_unsupported_adapter_fails_clearly(runner):
    r = runner.run({"type": "shell", "params": {"cmd": "whoami"}})
    assert r.status == "failed" and r.code == "ADAPTER_UNSUPPORTED" and no_workdirs(runner)


def test_symlink_escape_not_delivered(runner, tmp_path):
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"user's own file")
    r = runner.run(probe(mode="escape", target=str(secret)))
    if r.code == "ESCAPE_FAILED":
        pytest.skip("這台 Windows 一般權限不能建符號連結（本身就擋住了）")
    assert r.status == "failed" and r.code == "OUTPUT_REJECTED", r
    assert not os.listdir(runner.dest_dir) if os.path.isdir(runner.dest_dir) else True
    assert secret.read_bytes() == b"user's own file"


def test_output_size_limit(runner):
    r = runner.run(probe(mode="write", bytes=4096), max_output_bytes=1000)
    assert r.status == "failed" and r.code == "TOO_BIG" and no_workdirs(runner)

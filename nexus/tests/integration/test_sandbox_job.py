"""
V3 PR2：Windows Job Object 實測（AT-008 資源上限、AT-015 取消整個程序樹、AT-026 節點當掉子程序跟著結束）。
只在 Windows 跑（nexus.yml 的 windows job）；其他平台整個檔案跳過。
"""
import subprocess
import sys
import time

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="Job Object 只有 Windows")

if sys.platform == "win32":
    from nexus.sandbox.jobs import Job, JobLimits, SandboxError, probe

PY = sys.executable
# 子程序：再開一個「孫程序」，兩個都睡很久（模擬 yt-dlp 開 ffmpeg）
TREE = ("import subprocess,sys,time;"
        "subprocess.Popen([sys.executable,'-c','import time; time.sleep(120)']);"
        "time.sleep(120)")


def pid_alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True).stdout
    return str(pid) in out


def wait_for(pred, timeout=10):
    t = time.time()
    while time.time() - t < timeout:
        if pred():
            return True
        time.sleep(0.1)
    return False


def test_limits_really_applied():
    with Job(JobLimits(memory_bytes=300 << 20, cpu_percent=30, max_processes=3)) as job:
        n = job.limits_now()
        assert n["memory_bytes"] == 300 << 20 and n["max_processes"] == 3 and n["cpu_percent"] == 30
        assert n["flags"] & 0x2000, "KILL_ON_JOB_CLOSE 沒設"


def test_child_is_inside_job_from_first_instruction():
    # 64 位元要先指定型別：GetCurrentProcess() 回的是 -1（偽 handle），沒指定會被截成 32 位元而呼叫失敗
    code = ("import ctypes,sys;from ctypes import wintypes as w;k=ctypes.WinDLL('kernel32');"
            "k.GetCurrentProcess.restype=w.HANDLE;"
            "k.IsProcessInJob.argtypes=[w.HANDLE,w.HANDLE,ctypes.POINTER(w.BOOL)];"
            "b=w.BOOL(0);ok=k.IsProcessInJob(k.GetCurrentProcess(),None,ctypes.byref(b));"
            "sys.exit(7 if ok and b.value else 3)")
    with Job() as job:
        p = job.spawn([PY, "-c", code])
        assert p.wait(30) == 7


def test_terminate_kills_whole_tree_no_orphans():
    with Job() as job:
        p = job.spawn([PY, "-c", TREE])
        assert wait_for(lambda: len(job.process_ids()) >= 2, 20), job.process_ids()
        pids = job.process_ids()
        job.terminate()
        assert job.wait_empty(10), f"還有殘留：{job.process_ids()}"
        assert p.wait(5) is not None
        for pid in pids:
            assert wait_for(lambda pid=pid: not pid_alive(pid), 10), f"{pid} 還活著"


def test_close_handle_kills_children_like_node_crash():
    job = Job()
    job.spawn([PY, "-c", TREE])
    assert wait_for(lambda: len(job.process_ids()) >= 2, 20)
    pids = job.process_ids()
    job.close()                                     # 模擬節點程式當掉：handle 被系統關掉
    for pid in pids:
        assert wait_for(lambda pid=pid: not pid_alive(pid), 10), f"{pid} 在節點關掉後還活著"


def test_memory_limit_stops_big_allocation():
    code = "b = bytearray(600 * 1024 * 1024); print(len(b))"
    with Job(JobLimits(memory_bytes=200 << 20)) as job:
        p = job.spawn([PY, "-c", code], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = p.communicate(timeout=60)
        assert p.returncode != 0, "超過記憶體上限卻成功了"
        assert b"MemoryError" in err or job.memory_limit_hit(), err[-300:]


def test_process_count_limit():
    code = ("import subprocess,sys\n"
            "ok=0\n"
            "for i in range(4):\n"
            "    try:\n"
            "        subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']); ok+=1\n"
            "    except OSError:\n"
            "        pass\n"
            "print(ok)\n")
    with Job(JobLimits(max_processes=2)) as job:
        p = job.spawn([PY, "-c", code], stdout=subprocess.PIPE, text=True)
        out, _ = p.communicate(timeout=60)
        assert int(out.strip()) == 1, f"最多 2 個程序（自己＋1 個），結果開了 {out.strip()} 個"
        job.terminate()
        assert job.wait_empty(10)


def test_shell_true_refused():
    with Job() as job, pytest.raises(SandboxError):
        job.spawn(["cmd", "/c", "echo hi"], shell=True)


def test_probe_reports_basic_on_windows():
    r = probe()
    assert r["isolation"] == "basic", r
    assert not r["reasons"]

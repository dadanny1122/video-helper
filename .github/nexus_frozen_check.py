"""
V3 PR4：打包成 EXE（PyInstaller --onefile，跟 VideoHelper.exe 一樣）之後，沙箱還正不正常？
nexus.yml 的 frozen job：pyinstaller 打包這個檔 → 執行 → 印結果，有一項不對就失敗。

onefile 的 EXE 會先開「解壓用的外殼程序」再開真正的 Python 程序，所以沙箱裡的程序數、取消時
能不能整組結束，都要在打包後實測（規格 07 第 8 節最大的風險）。
入口跟 video_helper_app.py 最上面一樣：--nexus-run／--probe-sleeper 交給 nexus.sandbox.child。
"""
import sys

if len(sys.argv) > 1 and sys.argv[1] in ("--nexus-run", "--probe-sleeper"):
    from nexus.sandbox.child import entry
    sys.exit(entry())
if len(sys.argv) > 2 and sys.argv[1] == "--nexus-probe-strong":      # APEX PR2：跟 video_helper_app.py 一樣
    from nexus.sandbox.appcontainer import entry as _strong_entry
    sys.exit(_strong_entry())

import json  # noqa: E402
import os  # noqa: E402
import subprocess  # noqa: E402
import tempfile  # noqa: E402
import threading  # noqa: E402
import time  # noqa: E402

from nexus.sandbox import appcontainer, jobs  # noqa: E402
from nexus.sandbox.runner import SandboxRunner  # noqa: E402


def count_my_exes() -> int:
    name = os.path.basename(sys.executable)
    out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {name}", "/NH"], capture_output=True, text=True).stdout
    return sum(1 for line in out.splitlines() if line.strip().lower().startswith(name.lower()))


def main() -> int:
    res, ok = {"frozen": bool(getattr(sys, "frozen", False)), "exe": sys.executable}, True
    res["probe"] = jobs.probe()
    ok &= res["probe"]["isolation"] == "basic"
    before = count_my_exes()                          # 自己（外殼＋Python＝2 個）
    with tempfile.TemporaryDirectory() as d:
        r = SandboxRunner(os.path.join(d, "nx"), os.path.join(d, "dl"))
        a = r.run({"type": "sandbox.probe", "params": {"mode": "write", "bytes": 1000, "name": "a.mp4"}})
        res["write"] = {"status": a.status, "code": a.code, "files": [f["name"] for f in a.files], "cleanup": a.cleanup_ok}
        ok &= a.ok and res["write"]["files"] == ["a.mp4"] and a.cleanup_ok
        c = threading.Event()
        threading.Timer(5, c.set).start()
        b = r.run({"type": "sandbox.probe", "params": {"mode": "spawn", "seconds": 60}}, cancel=c)
        time.sleep(1)
        after = count_my_exes()
        res["spawn_cancel"] = {"status": b.status, "leftover": b.leftover_processes, "exe_before": before,
                               "exe_after": after, "cleanup": b.cleanup_ok}
        ok &= b.status == "cancelled" and b.leftover_processes == 0 and after == before and b.cleanup_ok
        e = r.run({"type": "sandbox.probe", "params": {"mode": "env"}, "deliver": False})
        res["env_keys"] = e.result.get("env_keys")
        ok &= e.ok and "VH_SANDBOX" in (res["env_keys"] or [])
        # APEX PR2：打包後強隔離偵測能跑完、格式對（等級只記錄，不當成失敗：要看實際機器）
        res["strong"] = appcontainer.probe_strong(os.path.join(d, "node"))
        ok &= res["strong"]["hardening"] in appcontainer.LEVELS and "偵測程式沒有回報" not in "".join(res["strong"]["reasons"])
    res["ok"] = bool(ok)
    print(json.dumps(res, ensure_ascii=False, indent=1))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())

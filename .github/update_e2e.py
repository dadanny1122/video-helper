"""一鍵更新：真的 EXE 走一遍（v1.47.0，Windows CI；build.yml 打包後跑）
  A. 正常：舊版當小幫手 → 開新版 → 新版介面打開後回報成功 → 小幫手結束、新版保留
  B. 新版打不開（用一個會馬上結束的程式冒充新版）→ 小幫手換回舊版、開舊版並說「更新失敗」
用法：python -I .github/update_e2e.py dist/VideoHelper.exe
"""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
src = os.path.abspath(sys.argv[1] if len(sys.argv) > 1 else "dist/VideoHelper.exe")
fails = 0


def ok(c, m):
    global fails
    print(("::notice title=一鍵更新::✅ " if c else "::error title=一鍵更新::❌ ") + m, flush=True)
    if not c:
        fails += 1


def sha(p):
    return hashlib.sha256(open(p, "rb").read()).hexdigest()


def kill_all():
    subprocess.run(["taskkill", "/F", "/T", "/IM", "VideoHelper.exe"], capture_output=True)


def run_case(name, new_exe_src, timeout):
    root = tempfile.mkdtemp(prefix="vh-upd-")
    app, data = os.path.join(root, "app"), os.path.join(root, "data")
    os.makedirs(app)
    os.makedirs(data)
    exe = os.path.join(app, "VideoHelper.exe")
    old = exe + ".old"
    shutil.copy2(src, old)                    # 舊版（已改名 .old，小幫手跑在它裡面）
    shutil.copy2(new_exe_src, exe)            # 新版
    env = dict(os.environ, VIDEO_HELPER_HOME=data)
    arg = json.dumps({"exe": exe, "old": old, "new_ver": "9.9.9" if name == "B" else read_version(), "old_ver": "1.0.0", "data": data})
    t0 = time.time()
    w = subprocess.run([old, "--update-watchdog", arg], env=env, timeout=timeout + 60)
    st = {}
    try:
        st = json.load(open(os.path.join(data, "update_state.json"), encoding="utf-8"))
    except (OSError, ValueError):
        pass
    print(f"{name}: 小幫手結束（代碼 {w.returncode}，{time.time() - t0:.0f} 秒），state={st}", flush=True)
    return exe, old, data, st


def read_version():
    import re
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return re.search(r'^VERSION = "([^"]+)"', open(os.path.join(here, "video_helper_app.py"), encoding="utf-8").read(), re.M).group(1)


kill_all()
# A. 正常更新（新、舊都是這次打包的 EXE；新版用 --after-update 開起來後要回報）
exe, old, data, st = run_case("A", src, 60)
ok(st.get("result") == "ok", f"A 新版正常打開並回報成功（{st.get('result')} {st.get('reason', '')}）")
ok(sha(exe) == sha(src), "A 新版留在 VideoHelper.exe")
time.sleep(20)                                 # 新版會在背景清 .old（小幫手剛結束，檔案可能還被鎖一下）
ok(not os.path.exists(old), "A 新版把 .old 清掉了")
kill_all()

# B. 新版打不開：拿 Windows 內建的 where.exe 冒充（帶不認得的參數會馬上結束）
bad = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "where.exe")
exe, old, data, st = run_case("B", bad, 60)
ok(st.get("result") == "rolled_back", f"B 新版打不開 → 換回舊版（{st.get('reason', '')}）")
ok(os.path.exists(exe) and sha(exe) == sha(src), "B VideoHelper.exe 又是舊版")
ok(os.path.exists(exe + ".failed") and sha(exe + ".failed") == sha(bad), "B 壞掉的新版留在 .failed（下次啟動會清）")
time.sleep(8)
r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq VideoHelper.exe"], capture_output=True, text=True)
ok("VideoHelper.exe" in r.stdout, "B 換回後舊版有重新打開")
kill_all()
print("全部通過" if not fails else f"❌ {fails} 項沒過")
sys.exit(1 if fails else 0)

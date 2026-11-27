"""
sim.pvwatts：在沙箱裡用 PySAM pvwattsv8 算太陽光電發電量。

流程（節點）：
  1. 主程式抓氣象（power.py，有快取）
  2. 開工作資料夾（Workspace），寫 input.json（參數＋氣象）和固定的模擬腳本 CHILD_SCRIPT
  3. 在 Job Object 裡用「節點設定的 PySAM Python」跑腳本：環境變數白名單、子程序先把網路關掉
  4. 讀子程序印的一行 JSON → 檢查格式 → 回傳結果

腳本是寫死在這裡的字串（打包後 .py 原始碼不在 EXE 裡，所以不能用 inspect 讀檔），節點不會收到任何程式碼。
"""
import json
import os
import subprocess
import sys
import threading
import time

SCRIPT_NAME = "vh_pvwatts.py"
INPUT_NAME = "input.json"
MAX_STDOUT = 64 << 10

# ---- 沙箱子程序跑的固定腳本（只用標準庫＋PySAM；不能 import nexus，因為是節點自己的 Python） ----
CHILD_SCRIPT = r'''
import json, socket, sys

def _no_net(*a, **k):
    raise OSError("sim.pvwatts：模擬程序不准連網")
for _n in ("connect", "connect_ex", "sendto", "bind"):
    setattr(socket.socket, _n, _no_net)
socket.create_connection = _no_net
socket.getaddrinfo = _no_net

def out(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=True) + "\n")
    sys.stdout.flush()

def main():
    with open(sys.argv[1], encoding="utf-8") as f:
        job = json.load(f)
    try:
        import PySAM
        import PySAM.Pvwattsv8 as P
    except Exception as e:
        out({"event": "error", "code": "SANDBOX_UNAVAILABLE", "message": "這個 Python 沒有 PySAM：" + type(e).__name__})
        return
    p = job["params"]
    m = P.default("PVWattsNone")
    m.SolarResource.solar_resource_data = job["resource"]
    sd = m.SystemDesign
    sd.system_capacity = float(p["capacity_kw"])
    sd.tilt = float(p.get("tilt", 20))
    sd.azimuth = float(p.get("azimuth", 180))
    sd.losses = float(p.get("losses", 14))
    sd.array_type = int(p.get("array_type", 0))
    sd.module_type = int(p.get("module_type", 0))
    sd.dc_ac_ratio = float(p.get("dc_ac_ratio", 1.2))
    sd.gcr = float(p.get("gcr", 0.4))
    try:
        m.execute(0)
    except Exception as e:
        out({"event": "error", "code": "INVALID_INPUT", "message": "PySAM 算不出來：" + str(e)[:300]})
        return
    o = m.Outputs
    out({"event": "result", "result": {
        "annual_kwh": round(float(o.annual_energy), 3),
        "monthly_kwh": [round(float(x), 3) for x in o.monthly_energy],
        "capacity_factor": round(float(o.capacity_factor), 4),
        "kwh_per_kw": round(float(o.kwh_per_kw), 3),
        "pysam_version": str(getattr(PySAM, "__version__", "?"))[:32]}})

try:
    main()
except Exception as e:
    out({"event": "error", "code": "ADAPTER_ERROR", "message": type(e).__name__ + ": " + str(e)[:300]})
'''


class SimError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


# ---------- 節點設定的 PySAM Python ----------
_PROBE = {}


def probe_python(path: str, timeout: float = 60) -> tuple[bool, str]:
    """這個 Python 能不能 import PySAM.Pvwattsv8？回傳 (能不能, PySAM 版本或原因)。結果記住（設定改了才重測）"""
    if not path:
        return False, "沒設定 PySAM 用的 Python"
    if path in _PROBE:
        return _PROBE[path]
    if not os.path.isfile(path):
        r = (False, "找不到這個 Python")
    else:
        try:
            p = subprocess.run([path, "-I", "-c", "import PySAM, PySAM.Pvwattsv8; print(PySAM.__version__)"],
                               capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = p.stdout.decode("utf-8", "replace").strip().splitlines()
            r = (True, out[-1][:32]) if p.returncode == 0 and out else (False, "這個 Python 沒有裝 PySAM")
        except (OSError, subprocess.TimeoutExpired) as e:
            r = (False, f"開不了這個 Python：{type(e).__name__}")
    _PROBE[path] = r
    return r


# ---------- 結果檢查（節點、Master 都用） ----------
def result_problem(r) -> str | None:
    if not isinstance(r, dict):
        return "結果不是物件"
    a, mon, cf, k = r.get("annual_kwh"), r.get("monthly_kwh"), r.get("capacity_factor"), r.get("kwh_per_kw")
    num = (int, float)
    if not isinstance(a, num) or isinstance(a, bool) or a < 0:
        return "annual_kwh 不對"
    if not (isinstance(mon, list) and len(mon) == 12 and all(isinstance(x, num) and not isinstance(x, bool) and x >= 0 for x in mon)):
        return "monthly_kwh 要是 12 個非負數"
    if abs(sum(mon) - a) > max(1.0, a * 0.01):
        return "各月加起來跟全年對不上"
    if not isinstance(cf, num) or isinstance(cf, bool) or not 0 <= cf <= 100:
        return "capacity_factor 要在 0～100"
    if not isinstance(k, num) or isinstance(k, bool) or not 0 <= k <= 8760:
        return "kwh_per_kw 不合理"
    return None


# ---------- 在沙箱裡跑 ----------
def run(params: dict, resource: dict, python: str, sandbox_root: str, *, cancel: threading.Event | None = None,
        timeout: float = 600, use_job: bool = True) -> dict:
    """回傳結果 dict；失敗丟 SimError。use_job=False 只給 Linux 測試（沒有 Job Object）"""
    from nexus.sandbox.workspace import Workspace, safe_env
    cancel = cancel or threading.Event()
    ws = Workspace(sandbox_root)
    try:
        script = os.path.join(ws.input, SCRIPT_NAME)
        inp = os.path.join(ws.input, INPUT_NAME)
        with open(script, "w", encoding="utf-8") as f:
            f.write(CHILD_SCRIPT)
        with open(inp, "w", encoding="utf-8") as f:
            json.dump({"params": params, "resource": resource}, f)
        cmd = [python, "-I", script, inp]
        home = os.path.dirname(os.path.abspath(python))      # conda 環境沒「啟用」時，DLL 要靠 PATH 找到
        env = safe_env(ws, [home, os.path.join(home, "Library", "bin"), os.path.join(home, "Scripts")])
        if use_job:
            from nexus.sandbox import jobs
            with jobs.Job(jobs.JobLimits(memory_bytes=1 << 30, cpu_percent=50, max_processes=4)) as job:
                p = job.spawn(cmd, cwd=ws.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              stdin=subprocess.DEVNULL)
                out = _wait(p, cancel, timeout, job.terminate)
                if not job.wait_empty(10):
                    raise SimError("LEFTOVER_PROCESSES", "模擬結束後還有程序沒停")
        else:
            p = subprocess.Popen(cmd, cwd=ws.tmp, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                 stdin=subprocess.DEVNULL)
            out = _wait(p, cancel, timeout, p.kill)
    finally:
        ws.cleanup()
    ev = None
    for line in out.decode("utf-8", "replace").splitlines():
        try:
            x = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(x, dict) and x.get("event") in ("result", "error"):
            ev = x
    if ev is None:
        raise SimError("ADAPTER_ERROR", "模擬程序沒有回報結果")
    if ev["event"] == "error":
        code = ev.get("code") if ev.get("code") in ("SANDBOX_UNAVAILABLE", "INVALID_INPUT", "ADAPTER_ERROR") else "ADAPTER_ERROR"
        raise SimError(code, str(ev.get("message") or "模擬失敗")[:300])
    r = ev.get("result")
    bad = result_problem(r)
    if bad:
        raise SimError("OUTPUT_REJECTED", f"模擬結果格式不對：{bad}")
    return r


def _wait(p, cancel, timeout, kill) -> bytes:
    chunks, size = [], [0]

    def reader():
        for raw in iter(lambda: p.stdout.read(4096), b""):
            if size[0] < MAX_STDOUT:
                chunks.append(raw)
            size[0] += len(raw)
    t = threading.Thread(target=reader, daemon=True)
    t.start()
    deadline = time.monotonic() + timeout
    while p.poll() is None:
        if cancel.is_set():
            kill()
            p.wait(10)
            raise SimError("CANCELLED", "已取消")
        if time.monotonic() > deadline:
            kill()
            p.wait(10)
            raise SimError("TIMED_OUT", f"超過 {int(timeout)} 秒")
        time.sleep(0.05)
    t.join(5)
    if size[0] > MAX_STDOUT:
        raise SimError("OUTPUT_REJECTED", "模擬程序印太多東西")
    return b"".join(chunks)


if __name__ == "__main__":                       # 方便手動試：python -m nexus.sim.pvwatts <PySAM Python>
    print(probe_python(sys.argv[1] if len(sys.argv) > 1 else sys.executable))

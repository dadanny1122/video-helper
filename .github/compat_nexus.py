"""S3 相容性 C3：新 Master × 舊 Worker（真 TLS、真程序）
用法（由 compat_test.py 呼叫）：
  python -I .github/compat_nexus.py master <舊原始碼資料夾>      → 這支用「現在的」nexus 當 Master，另開子程序用舊的當 Worker
  python -I .github/compat_nexus.py worker <舊原始碼資料夾> <url> <配對碼>   → 子程序（只載入舊原始碼的 nexus）
過程：配對 → 核准 → 心跳 → 派 test.echo → 成功 → 撤銷 → 舊 Worker 斷線。印一行 JSON 結果。
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def wait(cond, sec, what):
    end = time.time() + sec
    while time.time() < end:
        v = cond()
        if v:
            return v
        time.sleep(0.2)
    raise AssertionError(f"等太久：{what}")


def worker(old_src, url, code):
    sys.path.insert(0, old_src)
    from nexus import NEXUS_VERSION, PROTOCOL_VERSION
    from nexus.core.config import Config
    from nexus.worker.agent import WorkerAgent
    from nexus.worker.credstore import CredStore
    cfg = Config("worker")
    cfg["heartbeat_seconds"] = 1
    agent = WorkerAgent(cfg, CredStore(), poll_seconds=0.3)
    print(json.dumps({"hello": NEXUS_VERSION, "protocol": PROTOCOL_VERSION}), flush=True)
    agent.pair(url, code, "compat-old-worker")
    agent.start()
    stop = threading.Event()
    threading.Thread(target=lambda: (sys.stdin.read(), stop.set()), daemon=True).start()
    last = None
    while not stop.is_set():
        cur = (agent.state, agent.detail)
        if cur != last:
            print(json.dumps({"state": cur[0], "detail": cur[1]}, ensure_ascii=False), flush=True)
            last = cur
        time.sleep(0.2)
    try:
        agent.stop(disconnect=False)
    except Exception:
        pass


def master(old_src):
    tmp = tempfile.mkdtemp(prefix="vh-compat-")
    os.environ["VH_NEXUS_ROOT"] = os.path.join(tmp, "master")
    sys.path.insert(0, ROOT)
    from nexus import NEXUS_VERSION, PROTOCOL_VERSION
    from nexus.core.config import Config
    from nexus.master.service import MasterService
    cfg = Config("master")
    cfg["bind_host"] = "127.0.0.1"
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    cfg["port"] = s.getsockname()[1]
    s.close()
    svc = MasterService(cfg)
    host, port = svc.start()
    code = svc.registry.pair.new()
    env = dict(os.environ, VH_NEXUS_ROOT=os.path.join(tmp, "worker"), PYTHONIOENCODING="utf-8")
    child = subprocess.Popen([sys.executable, "-I", __file__, "worker", old_src, f"https://{host}:{port}", code],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, env=env,
                             text=True, encoding="utf-8", errors="replace")
    lines, states = [], []

    def read():
        for ln in child.stdout:
            lines.append(ln.rstrip())
            try:
                d = json.loads(ln)
                if "state" in d:
                    states.append(d["state"])
            except ValueError:
                pass
    threading.Thread(target=read, daemon=True).start()
    out = {"master": NEXUS_VERSION, "master_protocol": PROTOCOL_VERSION}
    try:
        hello = wait(lambda: next((json.loads(x) for x in lines if x.startswith('{"hello"')), None), 30, "舊 Worker 啟動")
        out.update(worker=hello["hello"], worker_protocol=hello["protocol"])
        dev = wait(lambda: next((d for d in svc.devices() if d["status"] == "PENDING"), None), 30, "舊 Worker 申請配對")
        svc.registry.approve(dev["device_id"], "owner", "S3 相容性測試")
        wait(lambda: states and states[-1] == "ACTIVE", 30, "核准後舊 Worker 變 ACTIVE")
        wait(lambda: next((d for d in svc.devices() if d["device_id"] == dev["device_id"]
                           and d["connection_state"] == "CONNECTED"), None), 30, "Master 看到心跳（CONNECTED）")
        out["heartbeat"] = True
        wait(lambda: svc.capabilities(dev["device_id"]), 30, "舊 Worker 回報能力")
        t = svc.tasks.create("test.echo", {"text": "S3 相容性"}, actor="owner")
        done = wait(lambda: (lambda x: x if x["status"] in ("SUCCEEDED", "FAILED", "RESULT_REJECTED", "UNKNOWN") else None)(
            svc.tasks.get(t["task_id"])), 60, "回聲任務結束")
        assert done["status"] == "SUCCEEDED", f"回聲任務 {done['status']} {done['error_code']} {done['error_message']}"
        out["task"] = "SUCCEEDED"
        svc.registry.revoke(dev["device_id"], "owner", "S3 相容性測試結束")
        wait(lambda: states and states[-1] in ("REVOKED", "UNCONFIGURED"), 30, "撤銷後舊 Worker 斷線")
        out["revoked"] = states[-1]
        out["ok"] = True
    except AssertionError as e:
        out.update(ok=False, error=str(e), log=lines[-15:])
    finally:
        try:
            child.stdin.close()
            child.wait(10)
        except Exception:
            child.kill()
        svc.stop()
    print(json.dumps(out, ensure_ascii=False), flush=True)
    sys.exit(0 if out.get("ok") else 1)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if sys.argv[1] == "worker":
        worker(*sys.argv[2:5])
    else:
        master(sys.argv[2])

"""
沙箱子程序（`VideoHelper.exe --nexus-run <工作資料夾>`；原始碼執行時由 runner 用 python 開）。

規則：
  ・只讀 <工作資料夾>/input/task.json，只寫 output、tmp
  ・跟節點溝通只用 stdout，一行一個 JSON：{"event":"progress",...}／{"event":"result",...}／{"event":"error",...}
  ・不讀節點設定、憑證、金鑰；環境變數已被 runner 換成白名單
  ・CHILD_ADAPTERS 是固定清單：沒有 shell／python／任意命令。sandbox.probe 是沙箱自我測試用，
    Master 派不到（不在 nexus.tasks.adapters.ADAPTERS 裡）
"""
import json
import os
import subprocess
import sys
import time

TASK_FILE = "task.json"


class ChildError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code, self.message, self.retryable = code, message, retryable


def emit(event: str, **kw):
    # ensure_ascii：中文寫成 \uXXXX，不管子程序的輸出編碼是什麼（Windows 管線預設不是 UTF-8）都不會寫壞
    sys.stdout.write(json.dumps({"event": event, **kw}, ensure_ascii=True) + "\n")
    sys.stdout.flush()


# ---- sandbox.probe：沙箱自我測試（不連網、只碰 output／tmp） ----
def run_probe(params: dict, ws: dict) -> dict:
    mode = params.get("mode")
    if mode == "write":                               # 寫一個檔到 output（測成果搬運）
        n = int(params.get("bytes", 1024))
        if not 0 < n <= 64 << 20:
            raise ChildError("INVALID_INPUT", "bytes 超出範圍")
        name = str(params.get("name", "probe.mp4"))
        with open(os.path.join(ws["output"], os.path.basename(name)), "wb") as f:
            f.write(os.urandom(n))
        return {"wrote": n}
    if mode == "sleep":                               # 睡 N 秒、每 0.2 秒回報進度（測取消／逾時）
        secs = float(params.get("seconds", 1))
        t0 = time.monotonic()
        while (el := time.monotonic() - t0) < secs:
            emit("progress", progress=min(el / secs, 1.0))
            time.sleep(0.2)
        return {"slept": secs}
    if mode == "spawn":                               # 開一個會睡很久的孫程序（測整組結束、沒有殘留）
        subprocess.Popen([sys.executable] + _self_args() + ["--probe-sleeper"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        emit("progress", progress=0.5, note="grandchild started")
        time.sleep(float(params.get("seconds", 60)))
        return {"spawned": 1}
    if mode == "escape":                              # 故意想寫到工作資料夾外（測路徑檢查；應該被 deliver 擋）
        target = str(params.get("target", ""))
        try:
            os.symlink(target, os.path.join(ws["output"], "escape.mp4"))
        except OSError as e:
            raise ChildError("ESCAPE_FAILED", f"建連結失敗：{type(e).__name__}") from None
        return {"linked": True}
    if mode == "env":                                 # 回報看得到哪些環境變數（測白名單）
        return {"env_keys": sorted(os.environ)}
    raise ChildError("INVALID_INPUT", f"不認得的 mode：{mode}")


CHILD_ADAPTERS = {"sandbox.probe": run_probe}


def _self_args() -> list[str]:
    """孫程序怎麼開自己：打包後是同一個 EXE；原始碼時用 -c 載入這個模組"""
    if getattr(sys, "frozen", False):
        return []
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return ["-c", f"import sys;sys.path.insert(0,{root!r});from nexus.sandbox.child import entry;entry()"]


def main(workdir: str) -> int:
    ws = {k: os.path.join(workdir, k) for k in ("input", "output", "tmp")}
    try:
        with open(os.path.join(ws["input"], TASK_FILE), encoding="utf-8") as f:
            task = json.load(f)
        fn = CHILD_ADAPTERS.get(task.get("type"))
        if not fn:
            raise ChildError("ADAPTER_UNSUPPORTED", f"沙箱不支援 {task.get('type')}")
        os.chdir(ws["tmp"])
        result = fn(task.get("params") or {}, ws)
        emit("result", result=result)
        return 0
    except ChildError as e:
        emit("error", code=e.code, message=e.message, retryable=e.retryable)
        return 2
    except MemoryError:
        emit("error", code="RESOURCE_LIMIT", message="記憶體超過上限", retryable=False)
        return 3
    except Exception as e:                            # 不把 traceback 傳回去（可能含路徑），只給類型
        emit("error", code="ADAPTER_ERROR", message=f"執行失敗：{type(e).__name__}", retryable=False)
        return 4


def entry(argv: list[str] | None = None) -> int:
    """命令列入口：--nexus-run <資料夾>；--probe-sleeper＝孫程序（只睡覺）"""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:                                              # python -I 會忽略 PYTHONIOENCODING，這裡自己設 UTF-8
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    if "--probe-sleeper" in argv:
        time.sleep(120)
        return 0
    if len(argv) >= 2 and argv[0] == "--nexus-run":
        sys.exit(main(argv[1]))
    if len(argv) >= 1 and os.path.isdir(argv[-1]):
        sys.exit(main(argv[-1]))
    sys.stderr.write("用法：--nexus-run <工作資料夾>\n")
    return 64

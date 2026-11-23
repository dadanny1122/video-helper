"""
節點這邊的沙箱執行器：把一個任務放進沙箱跑完，回傳結果。

流程（每一步失敗都有明確原因，不會偷偷降級）：
  1. 建工作資料夾（workspace）、寫 input/task.json
  2. Job Object 裡開子程序：打包後＝`VideoHelper.exe --nexus-run <資料夾>`；原始碼＝python 載入 nexus.sandbox.child
     環境變數只給白名單（safe_env），stderr 寫到 tmp（不傳回 Master）
  3. 讀子程序 stdout 的 JSON 事件（進度／結果／錯誤）；取消或逾時 → 整組程序結束
  4. 確認 Job 裡 0 個程序（有殘留＝失敗）
  5. 成功才把 output 搬到下載資料夾（deliver 做路徑／副檔名／大小檢查）
  6. 一定清理工作資料夾；清不掉就把結果標成失敗（AT-024，不假裝完成）
"""
import json
import os
import sys
import threading
import time
from dataclasses import dataclass, field

from nexus.sandbox import jobs
from nexus.sandbox.child import TASK_FILE
from nexus.sandbox.workspace import Workspace, WorkspaceError, safe_env

MAX_EVENT_LINE = 64 * 1024          # 子程序一行事件最多 64KB（太長就當成格式錯誤，不讓它吃記憶體）
MAX_EVENTS = 100_000


@dataclass
class RunResult:
    status: str                      # succeeded／failed／cancelled／timed_out
    code: str = ""
    message: str = ""
    retryable: bool = False
    result: dict = field(default_factory=dict)
    files: list = field(default_factory=list)
    cleanup_ok: bool = True
    leftover_processes: int = 0
    memory_limit_hit: bool = False

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


def child_command(workdir: str) -> list[str]:
    """開沙箱子程序的指令（參數陣列，不用 shell）"""
    if getattr(sys, "frozen", False):
        return [sys.executable, "--nexus-run", workdir]
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [sys.executable, "-I", "-c",
            f"import sys;sys.path.insert(0,{root!r});from nexus.sandbox.child import entry;entry()",
            "--nexus-run", workdir]


class SandboxRunner:
    def __init__(self, root: str, dest_dir: str, limits: jobs.JobLimits | None = None,
                 extra_path: list[str] | None = None):
        self.root, self.dest_dir = root, dest_dir
        self.limits = limits or jobs.JobLimits()
        self.extra_path = extra_path or []

    def run(self, task: dict, *, on_event=None, cancel: threading.Event | None = None,
            timeout: float = 7200.0, max_output_bytes: int | None = None) -> RunResult:
        cancel = cancel or threading.Event()
        ws = Workspace(self.root)
        try:
            res = self._run_in(ws, task, on_event, cancel, timeout, max_output_bytes)
        except jobs.SandboxError as e:
            res = RunResult("failed", "SANDBOX_UNAVAILABLE", str(e))
        ok, err = ws.cleanup()
        if not ok:
            res.cleanup_ok = False
            if res.status == "succeeded":            # 成果已經搬走，但工作資料夾刪不掉：照實回報失敗
                res.status, res.code = "failed", "CLEANUP_FAILED"
            res.message = (res.message + "；" if res.message else "") + f"工作資料夾清不掉：{err}"
        return res

    def _run_in(self, ws, task, on_event, cancel, timeout, max_output_bytes) -> RunResult:
        with open(os.path.join(ws.input, TASK_FILE), "w", encoding="utf-8") as f:
            params = dict(task.get("params") or {})
            if max_output_bytes is not None:            # 節點自己加的上限（Master 的參數檢查不准有底線開頭的欄位）
                params["_max_output_bytes"] = int(max_output_bytes)
            json.dump({"type": task["type"], "params": params}, f, ensure_ascii=False)
        events, final = [], {}
        with jobs.Job(self.limits) as job, open(os.path.join(ws.tmp, "stderr.log"), "wb") as err:
            p = job.spawn(child_command(ws.path), cwd=ws.tmp, env=safe_env(ws, self.extra_path),
                          stdout=jobs.subprocess.PIPE, stderr=err, stdin=jobs.subprocess.DEVNULL)

            def reader():
                for raw in iter(lambda: p.stdout.readline(MAX_EVENT_LINE + 1), b""):
                    if len(raw) > MAX_EVENT_LINE or len(events) >= MAX_EVENTS:
                        final.setdefault("bad", "子程序回報格式不對（太長或太多）")
                        break
                    try:
                        ev = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        continue                     # 不是事件的雜訊（例如套件印的字）就略過
                    if not isinstance(ev, dict):
                        continue
                    events.append(ev)
                    if ev.get("event") in ("result", "error"):
                        final.setdefault("ev", ev)
                    elif ev.get("event") == "progress" and on_event:
                        try:
                            on_event(ev)
                        except Exception:
                            pass
            t = threading.Thread(target=reader, daemon=True)
            t.start()

            deadline = time.monotonic() + timeout
            stop = None
            while p.poll() is None:
                if cancel.is_set():
                    stop = "cancelled"
                elif time.monotonic() > deadline:
                    stop = "timed_out"
                elif "bad" in final:
                    stop = "bad"
                if stop:
                    job.terminate()
                    break
                time.sleep(0.05)
            try:
                p.wait(10)
            except jobs.subprocess.TimeoutExpired:
                job.terminate()
            empty = job.wait_empty(10)
            t.join(5)
            leftover = 0 if empty else len(job.process_ids())
            mem_hit = job.memory_limit_hit()

        if leftover:
            return RunResult("failed", "LEFTOVER_PROCESSES", f"結束後還有 {leftover} 個程序沒停",
                             leftover_processes=leftover, memory_limit_hit=mem_hit)
        if stop == "cancelled":
            return RunResult("cancelled", "CANCELLED", "已取消，整組程序都停了")
        if stop == "timed_out":
            return RunResult("timed_out", "TIMED_OUT", f"超過 {int(timeout)} 秒，整組程序都停了")
        if stop == "bad":
            return RunResult("failed", "PROTOCOL_ERROR", final["bad"])
        ev = final.get("ev")
        if not ev:
            msg = "記憶體超過上限" if mem_hit else f"子程序沒有回報就結束了（結束碼 {p.returncode}）"
            return RunResult("failed", "RESOURCE_LIMIT" if mem_hit else "CHILD_CRASHED", msg, memory_limit_hit=mem_hit)
        if ev["event"] == "error":
            return RunResult("failed", str(ev.get("code", "ADAPTER_ERROR"))[:64], str(ev.get("message", ""))[:500],
                             retryable=bool(ev.get("retryable")), memory_limit_hit=mem_hit)
        res = RunResult("succeeded", result=ev.get("result") if isinstance(ev.get("result"), dict) else {})
        if task.get("deliver", True):
            try:
                res.files = ws.deliver(self.dest_dir, max_bytes=max_output_bytes)
            except WorkspaceError as e:
                return RunResult("failed", e.code, e.reason)
        return res

"""
Worker 端任務執行（06-V2 §8）：長輪詢 inbox → 自己再檢查一次 → ACK → **等 Master 回 start=true 才開始** → 回報事件。
  ・收到任務先驗：manifest 雜湊（T-05）、格式、Adapter 版本（AT-018）、接不接任務、名額、記憶體／磁碟 → 不合就拒收＋原因
  ・test.echo／test.sleep／test.fail：在這個程序的執行緒裡跑，不開子程序、不碰檔案、不連網
  ・video.download（V3）：交給 nexus.sandbox.runner 在沙箱子程序裡跑；沙箱不能用就不宣稱支援
  ・自己也守執行時間上限（逾時就停、回報 TIMED_OUT）；收到取消就停、回報 CANCELLED
  ・事件送失敗會用同一個 event_id 重送（Master 會去重）
  ・V4 傳回：下載完 → 回報 UPLOADING（附檔案清單）→ nexus.worker.uploader 分段傳 → SUCCEEDED → Master 驗證成功才刪節點這份
    傳回不佔「同時下載」名額；節點關掉不算失敗（Master 等它回來，inbox 會提醒接著傳）
"""
import hashlib
import logging
import os
import shutil
import sys
import threading
import time
import uuid

from nexus.core.config import mode_dir, worker_profile
from nexus.core.errors import NexusError
from nexus.tasks import adapters, manifest
from nexus.worker.client import NexusClient

log = logging.getLogger("nexus.worker.exec")


class Cancelled(Exception):
    pass


class TimedOut(Exception):
    pass


class AdapterFailure(Exception):
    def __init__(self, code, message=""):
        super().__init__(message or code)
        self.code = code


class Ctx:
    def __init__(self, deadline: float, cancel: threading.Event, progress):
        self.deadline, self.cancel, self.progress = deadline, cancel, progress

    def check(self):
        if self.cancel.is_set():
            raise Cancelled()
        if time.monotonic() > self.deadline:
            raise TimedOut()

    def sleep(self, seconds: float):
        end = time.monotonic() + seconds
        while True:
            self.check()
            left = end - time.monotonic()
            if left <= 0:
                return
            self.cancel.wait(min(left, 0.2))


# ---- 測試 Adapter 的實作（版本跟 nexus.tasks.adapters 一致） ----
def run_echo(params, ctx):
    ctx.check()
    text = params["text"]
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def run_sleep(params, ctx):
    n = params["seconds"]
    for i in range(n):
        ctx.progress(round(i / n, 3))
        ctx.sleep(1)
    return {"slept": n}


def run_fail(params, ctx):
    ctx.check()
    raise AdapterFailure(params["code"], f"test.fail 照要求失敗：{params['code']}")


# ---- V3：video.download 在沙箱子程序裡跑（不在這個程序裡） ----
def default_download_dir(profile: str | None = None) -> str:
    base = os.path.join(os.path.expanduser("~"), "Downloads")
    return os.path.join(base, "VideoHelper 節點" + (f" {profile}" if profile else ""))


def _ffmpeg_dirs() -> list[str]:
    """原始碼執行時把 PATH 上的 ffmpeg 資料夾交給沙箱；打包後子程序用自己解壓出來的迷你 ffmpeg"""
    ff = shutil.which("ffmpeg")
    return [os.path.dirname(ff)] if ff and not getattr(sys, "frozen", False) else []


def run_video_download(params, ctx):
    from nexus.sandbox.jobs import JobLimits
    from nexus.sandbox.runner import SandboxRunner
    cfg = getattr(ctx, "cfg", None) or {}
    dest = (cfg.get("download_dir") if hasattr(cfg, "get") else "") or default_download_dir(worker_profile())
    # 沙箱工作資料夾跟這個節點的設定檔放一起（每個 profile 各自一份）
    base = os.path.dirname(cfg.path) if getattr(cfg, "path", None) else mode_dir("worker")
    runner = SandboxRunner(os.path.join(base, "sandbox"), dest, JobLimits(), _ffmpeg_dirs())
    limits = getattr(ctx, "limits", {}) or {}
    left = max(1.0, ctx.deadline - time.monotonic())
    r = runner.run({"type": "video.download", "params": dict(params)},
                   on_event=lambda ev: ctx.progress(float(ev.get("progress") or 0.0)),
                   cancel=ctx.cancel, timeout=left, max_output_bytes=limits.get("max_output_bytes"))
    if r.status == "cancelled":
        raise Cancelled()
    if r.status == "timed_out":
        raise TimedOut()
    if not r.ok:
        raise AdapterFailure(r.code or "ADAPTER_ERROR", r.message)
    files = [{k: f[k] for k in ("name", "size", "sha256", "path")} for f in r.files]
    return {"files": files, **{k: v for k, v in r.result.items() if k in ("title", "site", "id", "height")}}


def node_download_dir(cfg) -> str:
    return (cfg.get("download_dir") if hasattr(cfg, "get") else "") or default_download_dir(worker_profile())


def run_test_file(params, ctx):
    """V4 測試用：在下載資料夾寫一個固定內容的 N MB 檔案（不連網、不開子程序）"""
    from nexus.sandbox.workspace import Workspace
    dest = node_download_dir(getattr(ctx, "cfg", None) or {})
    os.makedirs(dest, exist_ok=True)
    n = params["size_mb"]
    tid = str(getattr(ctx, "task_id", "") or uuid.uuid4())[:8]
    path = Workspace.unique_target(dest, f"nexus-test-{tid}-{n}MB.mp4")
    block = hashlib.sha256(tid.encode()).digest() * (1 << 15)           # 1 MB 固定內容
    h = hashlib.sha256()
    try:
        with open(path, "wb") as f:
            for i in range(n):
                ctx.check()
                f.write(block)
                h.update(block)
                ctx.progress(round((i + 1) / n, 3))
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return {"files": [{"name": os.path.basename(path), "size": n << 20, "sha256": h.hexdigest(), "path": path}]}


RUNNERS = {"test.echo": run_echo, "test.sleep": run_sleep, "test.fail": run_fail, "test.file": run_test_file,
           "video.download": run_video_download}
SANDBOXED = {"video.download"}          # 要沙箱的任務：這台的沙箱不能用就不宣稱支援（D6，不降級）


def sandbox_status() -> dict:
    """沙箱能力（啟動時測一次）；不是 Windows 或建不起來 → isolation none＋原因"""
    global _SANDBOX
    if _SANDBOX is None:
        try:
            from nexus.sandbox.jobs import probe
            _SANDBOX = probe()
        except Exception as e:
            _SANDBOX = {"isolation": "none", "reasons": [f"偵測失敗：{type(e).__name__}"]}
    return _SANDBOX


_SANDBOX = None
_HARDENING = None
_hardening_lock = threading.Lock()
_hardening_thread = None


def hardening_status(probe_dir: str | None = None) -> dict | None:
    """
    APEX PR2：強隔離（AppContainer）偵測結果；**只偵測，不影響下載**。
    第一次呼叫在背景開始偵測（要開一個子程序，打包後可能要好幾秒），還沒好之前回 None。
    """
    global _hardening_thread
    with _hardening_lock:
        if _HARDENING is None and _hardening_thread is None:
            d = probe_dir or mode_dir("worker")
            _hardening_thread = threading.Thread(target=_run_hardening_probe, args=(d,), daemon=True,
                                                 name="nexus-hardening-probe")
            _hardening_thread.start()
    return _HARDENING


def _run_hardening_probe(probe_dir: str):
    global _HARDENING
    try:
        from nexus.sandbox.appcontainer import probe_strong
        r = probe_strong(probe_dir)
    except Exception as e:
        r = {"hardening": "none", "reasons": [f"偵測失敗：{type(e).__name__}"], "checks": {}}
    log.info("強隔離偵測：%s %s", r.get("hardening"), "；".join(r.get("reasons") or []))
    _HARDENING = r


class Executor:
    def __init__(self, agent, versions: dict | None = None, poll_wait: int = 25):
        self.agent = agent
        if versions is None:
            versions = {n: a.version for n, a in adapters.ADAPTERS.items() if n in RUNNERS}
            if sandbox_status().get("isolation") != "basic":
                versions = {n: v for n, v in versions.items() if n not in SANDBOXED}
        self.versions = versions
        self.poll_wait = poll_wait
        self.running = {}                          # task_id → {"attempt_id", "cancel": Event, "thread", "type", "progress"}
        self.uploads = {}                          # V4：傳回中的 task_id → 同上（不佔下載名額）
        self.lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._client = None
        self._client_key = None

    # ---- 對外 ----
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nexus-exec", daemon=True)
        self._thread.start()

    def stop(self, cancel_running=True):
        self._stop.set()
        if cancel_running:
            self.cancel_all()
        if self._client:
            self._client.close()
        if self._thread:
            self._thread.join(timeout=3)

    def running_ids(self) -> list[str]:
        with self.lock:
            return list(self.running)

    def summary(self) -> list[dict]:
        with self.lock:
            return ([{"task_id": k, "type": v["type"], "progress": v["progress"]} for k, v in self.running.items()]
                    + [{"task_id": k, "type": "upload", "progress": v["progress"], "status": v.get("status", "")}
                       for k, v in self.uploads.items()])

    def cancel_all(self, reason="shutdown"):
        with self.lock:
            for r in list(self.running.values()) + list(self.uploads.values()):
                r.setdefault("cancel_reason", reason)
                r["cancel"].set()

    # ---- 連線（自己一條，跟心跳分開；憑證換了就重建） ----
    def _new_client(self, timeout: float = 30.0) -> NexusClient:
        st = self.agent.store.state
        certfile, keyfile, pw = self.agent.store.tls_material()
        return NexusClient(st["master_url"], sender_id=f"worker:{st['device_id']}", ca_pem=self.agent.store.ca_pem(),
                           certfile=certfile, keyfile=keyfile, key_password=pw, timeout=timeout)

    def client(self) -> NexusClient:
        """長輪詢＋ACK 用的連線（只在主迴圈執行緒用）；每個任務另外開自己的連線"""
        st = self.agent.store.state
        key = (st["device_id"], st["credential_expires_at"], st["master_url"])
        if self._client is None or key != self._client_key:
            if self._client:
                self._client.close()
            self._client = self._new_client(self.poll_wait + 10)
            self._client_key = key
        return self._client

    # ---- 主迴圈 ----
    def _loop(self):
        backoff = 1
        while not self._stop.is_set():
            if self.agent.state != "ACTIVE" or not self.agent.store.has_credential():
                if self.agent.state in ("REVOKED", "EXPIRED", "MASTER_CHANGED", "STOPPED"):
                    self.cancel_all("revoked")     # 被撤銷之類：手上的任務都停（回報也送不出去）
                self._stop.wait(1)
                continue
            try:
                r = self.client().inbox(self.agent.store.state["device_id"], self.poll_wait)
                for m in r.get("messages", []):
                    if self._stop.is_set():            # 已經在關了：路上回來的訊息不要再處理（不然會偷偷開新的傳回）
                        break
                    self._handle(m)
                backoff = 1
            except NexusError as e:
                log.info("拿任務失敗：%s", e.code)
                self._stop.wait(min(30, backoff))
                backoff *= 2
            except Exception as e:                  # 網路斷之類 → 等一下再試（心跳那邊會處理重連）
                log.info("拿任務連線問題：%s", type(e).__name__)
                if self._client:
                    self._client.close()
                    self._client = None
                self._stop.wait(min(30, backoff))
                backoff *= 2

    def _handle(self, m: dict):
        if m.get("kind") == "cancel":
            with self.lock:
                r = self.running.get(m.get("task_id")) or self.uploads.get(m.get("task_id"))
                if r and r["attempt_id"] == m.get("attempt_id"):
                    r.setdefault("cancel_reason", m.get("reason"))     # 先寫原因再設旗標（任務執行緒會讀）
                    r["cancel"].set()
            return
        if m.get("kind") == "upload":
            self._resume_upload(m)
            return
        if m.get("kind") != "task":
            return
        with self.lock:
            if m["task_id"] in self.running:
                return                              # 同一份派送又來一次（Master 重送）→ 已經在做了
        reason = self.check(m)
        did = self.agent.store.state["device_id"]
        c = self.client()
        if reason:
            log.info("拒收任務 %s：%s", m["task_id"][:8], reason)
            c.task_ack(did, m["task_id"], m["attempt_id"], m["lease_id"], False, reason)
            return
        r = c.task_ack(did, m["task_id"], m["attempt_id"], m["lease_id"], True)
        if not r.get("start"):
            log.info("Master 說不要開始 %s（%s）", m["task_id"][:8], r.get("reason"))
            return
        cancel = threading.Event()
        entry = {"attempt_id": m["attempt_id"], "cancel": cancel, "type": m["manifest"]["type"], "progress": 0.0}
        th = threading.Thread(target=self._run, args=(m, entry), name=f"task-{m['task_id'][:8]}", daemon=True)
        entry["thread"] = th
        with self.lock:
            self.running[m["task_id"]] = entry
        th.start()

    def check(self, m: dict) -> str:
        """回傳拒收原因（空字串＝接）"""
        mf = m.get("manifest")
        if not isinstance(mf, dict) or manifest.sha256(mf) != m.get("manifest_sha256"):
            return "MANIFEST_INVALID"                                  # T-05：內容被改過
        if mf.get("task_id") != m.get("task_id") or manifest.validate(mf):
            return "MANIFEST_INVALID"
        have = self.versions.get(mf["type"])
        if have is None or have.split(".")[0] != str(mf["adapter_version"]).split(".")[0]:
            return "ADAPTER_UNSUPPORTED"                               # AT-018
        if not self.agent.cfg.get("accept_tasks", True):
            return "NOT_ACCEPTING"
        if len(self.running_ids()) >= max(1, int(self.agent.cfg.get("max_concurrency", 1) or 1)):
            return "FULL"
        res = self.agent.probe.snapshot()
        req = mf["requirements"]
        if req.get("min_memory_mb") and res.get("memory_available_mb", 0) < req["min_memory_mb"]:
            return "RESOURCE_LIMIT"
        if req.get("min_disk_mb") and res.get("disk_free_mb", 0) < req["min_disk_mb"]:
            return "RESOURCE_LIMIT"
        if manifest.expired(mf):
            return "EXPIRED"
        return ""

    # ---- 回報事件（每個任務自己一條連線；seq 從 start_seq 開始往上） ----
    def _sender(self, task_id: str, attempt_id: str, entry: dict, start_seq: int = 0):
        seq = [start_seq]
        conn = [None]

        def send(etype, data=None):
            seq[0] += 1
            eid = str(uuid.uuid4())
            for i in range(5):                                   # 同一個 event_id 重送，Master 會去重
                try:
                    if conn[0] is None:
                        conn[0] = self._new_client()
                    r = conn[0].task_event(task_id, attempt_id, seq[0], etype, data, event_id=eid)
                    if r.get("stop") and etype in ("PREPARING", "RUNNING", "PROGRESS"):
                        entry["cancel"].set()
                    return r
                except NexusError as e:
                    if e.code in ("EVENT_OUT_OF_ORDER", "INVALID_TRANSITION", "TASK_NOT_ASSIGNED", "TASK_NOT_FOUND",
                                  "CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED", "CREDENTIAL_MISMATCH", "DEVICE_NOT_ACTIVE"):
                        entry["cancel"].set()                    # 送不出去也不該再送：停
                        return {}
                    time.sleep(min(5, 0.5 * 2 ** i))
                except Exception:
                    if conn[0]:
                        conn[0].close()
                    conn[0] = None
                    time.sleep(min(5, 0.5 * 2 ** i))
            return {}

        def close():
            if conn[0]:
                conn[0].close()
                conn[0] = None
        return send, close

    # ---- 跑一個任務 ----
    def _run(self, m: dict, entry: dict):
        task_id, attempt_id = m["task_id"], m["attempt_id"]
        mf = m["manifest"]
        last_progress = [0.0]
        send, close = self._sender(task_id, attempt_id, entry)
        handed_over = [False]

        def progress(p):
            entry["progress"] = p
            if p - last_progress[0] >= 0.05 or p == 0:
                last_progress[0] = p
                send("PROGRESS", {"progress": p})

        ctx = Ctx(time.monotonic() + mf["limits"]["max_runtime_seconds"], entry["cancel"], progress)
        ctx.limits, ctx.cfg, ctx.task_id = mf["limits"], self.agent.cfg, task_id
        try:
            send("PREPARING")
            send("RUNNING")
            ctx.check()
            result = RUNNERS[mf["type"]](mf["params"], ctx)
            if mf["params"].get("return_to_master") and isinstance(result, dict) and result.get("files"):
                r = send("UPLOADING", {"result": result})
                if r.get("status") != "UPLOADING":
                    log.info("Master 不收任務 %s 的傳回（%s）", task_id[:8], r.get("status"))
                    return
                handed_over[0] = True                   # 名額讓出來：傳回改在 uploads 裡算
                with self.lock:
                    self.running.pop(task_id, None)
                    self.uploads[task_id] = entry
                entry["progress"] = 0.0
                self._upload(task_id, attempt_id, result["files"], entry, send)
                return
            send("SUCCEEDED", {"result": result})
        except Cancelled:
            why = entry.get("cancel_reason")
            if why == "shutdown":                         # 節點要關了（不是使用者取消）→ 算失敗，Master 照重試規則處理
                send("FAILED", {"error_code": "ADAPTER_ERROR", "message": "節點關閉，任務中斷"})
            elif why == "revoked":
                log.warning("節點被撤銷／停用，任務 %s 已停止（無法回報）", task_id[:8])
            elif why == "timed_out":
                send("TIMED_OUT")
            else:
                send("CANCELLED")
        except TimedOut:
            send("TIMED_OUT")
        except AdapterFailure as e:
            send("FAILED", {"error_code": e.code, "message": str(e)[:300]})
        except Exception as e:
            log.exception("任務 %s 出錯", task_id[:8])
            send("FAILED", {"error_code": "ADAPTER_ERROR", "message": type(e).__name__})
        finally:
            with self.lock:
                self.running.pop(task_id, None)
                if handed_over[0]:
                    self.uploads.pop(task_id, None)
            close()

    # ---- V4 傳回 ----
    def _upload(self, task_id: str, attempt_id: str, files: list, entry: dict, send):
        """在呼叫的執行緒裡傳完（或停下）。Cancelled／例外在這裡處理完，不往外丟"""
        from nexus.worker.uploader import UploadAbort, Uploader, check_files
        last = [0.0]

        def progress(p):
            entry["progress"] = p
            if p - last[0] >= 0.05 or p >= 1:
                last[0] = p
                send("PROGRESS", {"progress": p, "phase": "upload"})

        def status(s):
            entry["status"] = s
        ctx = Ctx(time.monotonic() + 10 * 365 * 86400, entry["cancel"], progress)   # 傳回沒有執行時間上限（D7 由 Master 判斷）
        dest = node_download_dir(self.agent.cfg)
        why = check_files(files, dest)
        if why:
            send("FAILED", {"error_code": "TRANSFER_FAILED", "message": why})
            return
        up = Uploader(lambda: self._new_client(120.0), ctx, status)
        try:
            up.run(task_id, attempt_id, files)
            r = send("SUCCEEDED", {"result": {"files": files}})
            if r.get("status") == "SUCCEEDED" and not self.agent.cfg.get("keep_after_return", False):
                for f in files:                           # D3：Master 驗證成功才刪
                    try:
                        if os.path.isfile(f["path"]) and check_files([f], dest) == "":
                            os.remove(f["path"])
                    except OSError as e:
                        log.warning("傳回後刪不掉節點這份：%s", e)
        except Cancelled:
            why = entry.get("cancel_reason")
            if why in ("shutdown", "revoked"):
                log.info("傳回 %s 暫停（%s），之後接著傳", task_id[:8], why)
            else:
                send("CANCELLED")
        except UploadAbort as e:
            if e.code == "TRANSFER_FAILED":
                send("FAILED", {"error_code": "TRANSFER_FAILED", "message": str(e)[:300]})
            else:
                log.info("傳回 %s 停止：%s", task_id[:8], e.code)
        except Exception as e:
            log.exception("傳回 %s 出錯", task_id[:8])
            send("FAILED", {"error_code": "TRANSFER_FAILED", "message": type(e).__name__})

    def _resume_upload(self, m: dict):
        """Master 提醒「這個還沒傳完」（節點重開、斷線回來、按了重新傳回）"""
        tid = m.get("task_id")
        files = m.get("files")
        if not tid or not isinstance(files, list) or not files or self._stop.is_set():
            return
        with self.lock:                                  # 鎖裡再看一次：stop() 先設 _stop 再 cancel_all（也要拿這把鎖）
            if tid in self.running or tid in self.uploads or self._stop.is_set():
                return                                   # 已經在傳了，或正在關
            entry = {"attempt_id": m["attempt_id"], "cancel": threading.Event(), "type": "upload", "progress": 0.0}
            self.uploads[tid] = entry

        def go():
            send, close = self._sender(tid, m["attempt_id"], entry, int(m.get("next_seq") or 1) - 1)
            try:
                self._upload(tid, m["attempt_id"], files, entry, send)
            finally:
                with self.lock:
                    self.uploads.pop(tid, None)
                close()
        th = threading.Thread(target=go, name=f"upload-{tid[:8]}", daemon=True)
        entry["thread"] = th
        th.start()

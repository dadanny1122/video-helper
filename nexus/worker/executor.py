"""
Worker 端任務執行（06-V2 §8）：長輪詢 inbox → 自己再檢查一次 → ACK → **等 Master 回 start=true 才開始** → 回報事件。
  ・收到任務先驗：manifest 雜湊（T-05）、格式、Adapter 版本（AT-018）、接不接任務、名額、記憶體／磁碟 → 不合就拒收＋原因
  ・Adapter 只有 test.echo／test.sleep／test.fail：在這個程序的執行緒裡跑，不開子程序、不碰檔案、不連網
  ・自己也守執行時間上限（逾時就停、回報 TIMED_OUT）；收到取消就停、回報 CANCELLED
  ・事件送失敗會用同一個 event_id 重送（Master 會去重）
"""
import hashlib
import logging
import threading
import time
import uuid

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


RUNNERS = {"test.echo": run_echo, "test.sleep": run_sleep, "test.fail": run_fail}


class Executor:
    def __init__(self, agent, versions: dict | None = None, poll_wait: int = 25):
        self.agent = agent
        self.versions = versions or {n: a.version for n, a in adapters.ADAPTERS.items() if n in RUNNERS}
        self.poll_wait = poll_wait
        self.running = {}                          # task_id → {"attempt_id", "cancel": Event, "thread", "type", "progress"}
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
            return [{"task_id": k, "type": v["type"], "progress": v["progress"]} for k, v in self.running.items()]

    def cancel_all(self, reason="shutdown"):
        with self.lock:
            for r in self.running.values():
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
                r = self.running.get(m.get("task_id"))
                if r and r["attempt_id"] == m.get("attempt_id"):
                    r.setdefault("cancel_reason", m.get("reason"))     # 先寫原因再設旗標（任務執行緒會讀）
                    r["cancel"].set()
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

    # ---- 跑一個任務 ----
    def _run(self, m: dict, entry: dict):
        task_id, attempt_id = m["task_id"], m["attempt_id"]
        mf = m["manifest"]
        seq = [0]
        last_progress = [0.0]
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

        def progress(p):
            entry["progress"] = p
            if p - last_progress[0] >= 0.05 or p == 0:
                last_progress[0] = p
                send("PROGRESS", {"progress": p})

        ctx = Ctx(time.monotonic() + mf["limits"]["max_runtime_seconds"], entry["cancel"], progress)
        try:
            send("PREPARING")
            send("RUNNING")
            ctx.check()
            result = RUNNERS[mf["type"]](mf["params"], ctx)
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
            if conn[0]:
                conn[0].close()

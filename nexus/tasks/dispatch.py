"""
派送層（06-V2 §3.4、§5）：Worker 主動來拿（inbox long-poll）→ lease → ACK → 事件 → 完成／失敗／取消。
關鍵保證：
  ・Worker 一定要等 ACK 的回覆是 start=true 才開始 → 沒 ACK 的派送可以安全收回重派（AT-009）
  ・ACK 之後節點離線／被撤銷 → UNKNOWN，不自動重跑、不宣稱成功（AT-010 V2 版）
  ・事件用 event_id 去重、(attempt, seq) 排序；重複的回 200 不改狀態，倒退的拒絕（AT-012）
  ・結果由 Master 自己驗證（大小、內容、雜湊），不信 Worker 說的（VERIFYING）
  ・所有狀態改變都經 TaskService（同交易寫稽核）
  ・V4 傳回：RUNNING → UPLOADING（結果先驗格式、存下檔名／大小／雜湊）→ 節點分段上傳（nexus.master.incoming）
    → 全部收齊且雜湊對了，節點送 SUCCEEDED → VERIFYING → SUCCEEDED。UPLOADING 時節點離線不判 UNKNOWN，等 24 小時
"""
import datetime as dt
import hashlib
import json
import logging
import os
import re
import threading
import uuid

from nexus.core.config import mode_dir
from nexus.core.errors import NexusError
from nexus.core.models import parse_utc, utcnow
from nexus.master.incoming import Incoming
from nexus.tasks import adapters, manifest
from nexus.tasks.scheduler import Scheduler
from nexus.tasks.states import DONE, ON_WORKER, AttemptStatus, TaskStatus

log = logging.getLogger("nexus.dispatch")
S, A = TaskStatus, AttemptStatus
LEASE_SECONDS = 30            # 派出去多久沒 ACK 就收回
SCHEDULED_STALE_SECONDS = 30  # 指派了但節點一直沒來拿 → 重新排程
TIMEOUT_GRACE_SECONDS = 15    # 超過執行上限再給 Worker 這麼久自己停
EVENT_TYPES = ("PREPARING", "RUNNING", "PROGRESS", "UPLOADING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT")
WORKER_ERROR_CODES = ("ADAPTER_ERROR", "RESOURCE_LIMIT", "INVALID_INPUT", "MANIFEST_INVALID", "ADAPTER_UNSUPPORTED", "TIMED_OUT",
                      "NETWORK", "RATE_LIMITED", "NET_BLOCKED", "NOT_FOUND", "DISK_FULL", "SANDBOX_UNAVAILABLE",
                      "OUTPUT_REJECTED", "TOO_BIG", "CLEANUP_FAILED", "LEFTOVER_PROCESSES", "PROTOCOL_UNSUPPORTED",
                      "SITE_REFUSED", "TRANSFER_FAILED")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")
UPLOAD_EXPIRY_SECONDS = 24 * 3600      # D7：傳回 24 小時沒進展就放棄（檔案留在節點）
UPLOAD_NUDGE_SECONDS = 60              # 傳回中卻 60 秒沒動靜 → inbox 提醒節點接著傳（節點重開、斷線後）
FILE_TASKS = ("video.download", "test.file")


def wants_return(t: dict) -> bool:
    """這個任務下載完要不要傳回 Master（D2）"""
    try:
        return bool(json.loads(t["manifest_json"])["params"].get("return_to_master"))
    except Exception:
        return False


def _now():
    return dt.datetime.now(dt.timezone.utc)


def _iso(t: dt.datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


class Dispatcher:
    def __init__(self, service, registry, tick_seconds: float = 1.0):
        self.svc = service
        self.tr = service.tr
        self.reg = registry
        self.scheduler = Scheduler(service, registry.repo)
        self.tick = tick_seconds
        self.cond = threading.Condition()
        self.timeout_cancels = {}                 # device_id → {task_id: attempt_id}：逾時要 Worker 停下（記憶體即可，Worker 自己也會停）
        self._stop = threading.Event()
        self._thread = None
        prev = service.on_change
        service.on_change = lambda t: (prev(t), self.notify())
        registry.on_device_change = self.device_changed
        # V4 收檔：MasterService 會換成電腦版的下載資料夾、限速、收完的通知
        self.incoming = Incoming(service.db, os.path.join(mode_dir("master"), "incoming"))
        self.rate_limit_mbps = lambda: 0
        self.on_returned = None
        self._nudged = {}

    # ---------- 背景迴圈：排程＋清查 ----------
    def start(self):
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="nexus-dispatch", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        self.notify()
        if self._thread:
            self._thread.join(timeout=5)

    def notify(self):
        with self.cond:
            self.cond.notify_all()

    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sweep()
                if self.scheduler.run_once():
                    self.notify()
            except Exception:
                log.exception("派送迴圈錯誤")
            self._stop.wait(self.tick)

    # ---------- Worker：拿任務 ----------
    def inbox(self, dev: dict, wait: float = 0) -> dict:
        did = dev["device_id"]
        deadline = _now() + dt.timedelta(seconds=max(0.0, min(float(wait), 25.0)))
        while True:
            msgs = self._collect(did)
            if msgs or _now() >= deadline or self._stop.is_set():
                return {"messages": msgs, "server_time": utcnow()}
            with self.cond:
                self.cond.wait(timeout=min(2.0, max(0.05, (deadline - _now()).total_seconds())))

    def _collect(self, did: str) -> list:
        msgs = []
        # 1) 取消指令（使用者按取消，或逾時）
        for t in self.tr.tasks_on_device(did, [S.CANCELLING.value]):
            msgs.append({"kind": "cancel", "task_id": t["task_id"], "attempt_id": t["current_attempt_id"], "reason": "cancelled"})
        for task_id, attempt_id in list(self.timeout_cancels.get(did, {}).items()):
            msgs.append({"kind": "cancel", "task_id": task_id, "attempt_id": attempt_id, "reason": "timed_out"})
        # 2) 已派出但還沒 ACK 的（Worker 可能沒收到上一次的回應）→ 同一份再給一次
        for t in self.tr.tasks_on_device(did, [S.DISPATCHED.value]):
            a = self.tr.get_attempt(t["current_attempt_id"]) if t["current_attempt_id"] else None
            if a and a["status"] == A.OFFERED.value and parse_utc(a["lease_expires_at"]) > _now():
                msgs.append(self._offer_msg(t, a))
        # 2b) V4：傳回中卻沒動靜（節點重開、斷線剛回來、按了「重新傳回」）→ 提醒它接著傳
        for t in self.tr.tasks_on_device(did, [S.UPLOADING.value]):
            m = self._upload_msg(t)
            if m:
                msgs.append(m)
        # 3) 指派給它的 → 建 attempt＋lease，SCHEDULED → DISPATCHED
        for t in self.tr.tasks_on_device(did, [S.SCHEDULED.value]):
            try:
                with self.svc.db.tx() as c:
                    cur = self.tr.get_task(t["task_id"], c)
                    if cur["status"] != S.SCHEDULED.value or cur["assigned_device_id"] != did:
                        continue
                    n = len(self.tr.attempts_of(cur["task_id"], c)) + 1
                    a = dict(attempt_id=str(uuid.uuid4()), task_id=cur["task_id"], attempt_no=n, device_id=did,
                             lease_id=str(uuid.uuid4()), lease_expires_at=_iso(_now() + dt.timedelta(seconds=LEASE_SECONDS)),
                             status=A.OFFERED.value, offered_at=utcnow())
                    self.tr.insert_attempt(c, a)
                    cur = self.svc._transition(c, cur, S.DISPATCHED, "dispatcher", f"派給 {did[:8]}（第 {n} 次）",
                                               current_attempt_id=a["attempt_id"])
                msgs.append(self._offer_msg(cur, a))
            except NexusError as e:
                log.info("派送 %s 跳過：%s", t["task_id"][:8], e.code)
        return msgs

    def _upload_msg(self, t: dict) -> dict | None:
        prog = self.incoming.progress(t["task_id"])
        if not prog or not t["current_attempt_id"]:
            return None
        now = _now().timestamp()
        last = max(parse_utc(prog["updated_at"]).timestamp(), parse_utc(t["updated_at"]).timestamp())
        if now - last < UPLOAD_NUDGE_SECONDS or now - self._nudged.get(t["task_id"], 0) < UPLOAD_NUDGE_SECONDS:
            return None
        self._nudged[t["task_id"]] = now
        a = self.tr.get_attempt(t["current_attempt_id"])
        files = [{"index": r["file_index"], "name": r["name"], "size": r["size"], "sha256": r["sha256"], "path": r["node_path"]}
                 for r in prog["files"]]
        return {"kind": "upload", "task_id": t["task_id"], "attempt_id": t["current_attempt_id"],
                "next_seq": (a["last_seq"] if a else 0) + 1, "files": files}

    @staticmethod
    def _offer_msg(t: dict, a: dict) -> dict:
        return {"kind": "task", "task_id": t["task_id"], "attempt_id": a["attempt_id"], "lease_id": a["lease_id"],
                "lease_expires_at": a["lease_expires_at"], "manifest": json.loads(t["manifest_json"]),
                "manifest_sha256": t["manifest_sha256"]}

    # ---------- Worker：ACK ----------
    def ack(self, dev: dict, body: dict) -> dict:
        did = dev["device_id"]
        with self.svc.db.tx() as c:
            t = self.tr.get_task(body["task_id"], c)
            a = self.tr.get_attempt(body["attempt_id"], c)
            if not t or not a or a["task_id"] != t["task_id"]:
                raise NexusError("TASK_NOT_FOUND")
            if a["device_id"] != did:
                raise NexusError("TASK_NOT_ASSIGNED")
            if a["lease_id"] != body["lease_id"]:
                raise NexusError("LEASE_EXPIRED")
            if a["status"] == A.ACCEPTED.value and body["accept"]:
                return {"start": True}                                  # 重送的 ACK（冪等）
            if a["status"] != A.OFFERED.value or t["current_attempt_id"] != a["attempt_id"] or t["status"] != S.DISPATCHED.value:
                return {"start": False, "reason": "lease_invalid"}     # 已取消／已收回 → Worker 丟掉，不執行
            if parse_utc(a["lease_expires_at"]) <= _now():
                self.tr.update_attempt(c, a["attempt_id"], status=A.EXPIRED.value, ended_at=utcnow())
                self.svc._transition(c, t, S.QUEUED, "dispatcher", "ACK 太晚，lease 已過期", assigned_device_id=None,
                                     current_attempt_id=None)
                return {"start": False, "reason": "lease_expired"}
            if body["accept"]:
                self.tr.update_attempt(c, a["attempt_id"], status=A.ACCEPTED.value, accepted_at=utcnow())
                t = self.svc._transition(c, t, S.ACCEPTED, f"worker:{did}", "", attempt_count=t["attempt_count"] + 1)
                out = {"start": True}
            else:
                reason = str(body.get("reason") or "declined")[:64]
                self.tr.update_attempt(c, a["attempt_id"], status=A.DECLINED.value, ended_at=utcnow(), error_code=reason)
                t = self.svc._transition(c, t, S.QUEUED, f"worker:{did}", f"節點拒收：{reason}", assigned_device_id=None,
                                         current_attempt_id=None)
                out = {"start": False}
        self.svc.on_change(t)
        return out

    # ---------- Worker：事件 ----------
    def event(self, dev: dict, task_id: str, body: dict) -> dict:
        did = dev["device_id"]
        etype = body["type"]
        if etype not in EVENT_TYPES:
            raise NexusError("SCHEMA_INVALID", details={"field": "type"})
        data = body.get("data") or {}
        if len(json.dumps(data)) > 8192:
            raise NexusError("PAYLOAD_TOO_LARGE")
        actor = f"worker:{did}"
        with self.svc.db.tx() as c:
            t = self.tr.get_task(task_id, c)
            a = self.tr.get_attempt(body["attempt_id"], c)
            if not t or not a or a["task_id"] != task_id:
                raise NexusError("TASK_NOT_FOUND")
            if a["device_id"] != did:
                raise NexusError("TASK_NOT_ASSIGNED")
            ev = dict(event_id=body["event_id"], task_id=task_id, attempt_id=a["attempt_id"], device_id=did, seq=body["seq"],
                      type=etype, data=data)
            if c.execute("SELECT 1 FROM task_events WHERE event_id=?", (ev["event_id"],)).fetchone():
                return {"duplicate": True}                                # AT-012：重送 → 不改狀態
            if body["seq"] <= a["last_seq"]:
                raise NexusError("EVENT_OUT_OF_ORDER")
            self.tr.insert_event(c, ev)
            self.tr.update_attempt(c, a["attempt_id"], last_seq=body["seq"])
            if t["current_attempt_id"] != a["attempt_id"]:
                return {"stale": True, "stop": True}                       # 舊 attempt 的事件：記錄但不影響任務
            t = self._apply(c, t, a, etype, data, actor)
        self.svc.on_change(t)
        if t["status"] == S.SUCCEEDED.value and etype == "SUCCEEDED" and wants_return(t) and self.on_returned:
            try:
                self.on_returned(t, json.loads(t["result_json"] or "{}"))
            except Exception:
                log.exception("通知「傳回完成」失敗")
        return {"status": t["status"], "stop": t["status"] not in {s.value for s in ON_WORKER}}

    def _apply(self, c, t, a, etype, data, actor):
        st = S(t["status"])
        if st in DONE:
            return t                                  # 已經結束（例如 Master 先判逾時）：晚到的回報只記錄，不改結果
        if st == S.UNKNOWN and etype in ("RUNNING", "PROGRESS", "UPLOADING", "SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"):
            t = self.svc._transition(c, t, S.RUNNING, actor, "節點回來了，繼續回報")       # 規格表：UNKNOWN → RUNNING
            self.tr.update_attempt(c, a["attempt_id"], status=A.RUNNING.value)
            st = S.RUNNING
        if etype == "PREPARING":
            return self.svc._transition(c, t, S.PREPARING, actor)
        if etype == "RUNNING":
            if st == S.ACCEPTED:
                t = self.svc._transition(c, t, S.PREPARING, actor)
            if S(t["status"]) == S.CANCELLING:
                return t
            self.tr.update_attempt(c, a["attempt_id"], status=A.RUNNING.value, started_at=utcnow())
            return self.svc._transition(c, t, S.RUNNING, actor)
        if etype == "PROGRESS":
            return t
        if etype == "UPLOADING":
            return self._start_upload(c, t, a, data, actor)
        if etype == "SUCCEEDED" and wants_return(t):
            return self._finish_upload(c, t, a, actor)
        if etype == "SUCCEEDED":
            if st in (S.ACCEPTED, S.PREPARING):
                t = self.svc._transition(c, t, S.PREPARING, actor) if st == S.ACCEPTED else t
                t = self.svc._transition(c, t, S.RUNNING, actor)
            if S(t["status"]) == S.CANCELLING:
                # 取消來不及：結果照樣驗證，但任務記成取消前已完成
                pass
            else:
                t = self.svc._transition(c, t, S.VERIFYING, actor)
                ok, why = self.verify(t, data.get("result"))
                self.tr.update_attempt(c, a["attempt_id"], status=A.SUCCEEDED.value if ok else A.FAILED.value, ended_at=utcnow(),
                                       error_code=None if ok else "RESULT_REJECTED")
                if ok:
                    return self.svc._transition(c, t, S.SUCCEEDED, "master", "結果驗證通過",
                                                result_json=json.dumps(data.get("result"), ensure_ascii=False))
                return self.svc._transition(c, t, S.RESULT_REJECTED, "master", why, error_code="RESULT_REJECTED", error_message=why)
            self.tr.update_attempt(c, a["attempt_id"], status=A.SUCCEEDED.value, ended_at=utcnow())
            return self.svc._transition(c, t, S.CANCELLED, actor, "取消時任務已經完成")
        if etype == "CANCELLED":
            self.tr.update_attempt(c, a["attempt_id"], status=A.CANCELLED.value, ended_at=utcnow())
            self.incoming.discard_task(t["task_id"], c, status="cancelled")
            self.timeout_cancels.get(t["assigned_device_id"] or "", {}).pop(t["task_id"], None)
            return self.svc._transition(c, t, S.CANCELLED, actor, "節點確認已停止")
        if etype == "TIMED_OUT":
            self.tr.update_attempt(c, a["attempt_id"], status=A.FAILED.value, ended_at=utcnow(), error_code="TIMED_OUT")
            self.timeout_cancels.get(t["assigned_device_id"] or "", {}).pop(t["task_id"], None)
            if st == S.CANCELLING:
                return self.svc._transition(c, t, S.CANCELLED, actor, "取消時剛好逾時")
            t = self.svc._transition(c, t, S.TIMED_OUT, actor, "超過執行時間上限")
            return self.svc._transition(c, t, S.FAILED, actor, "逾時", error_code="TIMED_OUT", error_message="超過執行時間上限")
        if etype == "FAILED":
            code = data.get("error_code") if data.get("error_code") in WORKER_ERROR_CODES else "ADAPTER_ERROR"
            msg = str(data.get("message") or code)[:500]
            self.tr.update_attempt(c, a["attempt_id"], status=A.FAILED.value, ended_at=utcnow(), error_code=code)
            if st == S.CANCELLING:
                return self.svc._transition(c, t, S.CANCELLED, actor, f"取消時失敗：{msg}")
            if st == S.UPLOADING:                       # 節點放棄傳回（例如檔案在節點上不見了）
                self.incoming.discard_task(t["task_id"], c)
                return self.svc._transition(c, t, S.FAILED, actor, msg[:300], error_code=code, error_message=msg)
            if st in (S.ACCEPTED, S.PREPARING, S.RUNNING):
                return self.svc.fail_in_tx(c, t, code, msg, actor)
        raise NexusError("INVALID_TRANSITION", details={"from": st.value, "event": etype})

    # ---------- V4 傳回 ----------
    def _start_upload(self, c, t, a, data, actor):
        st = S(t["status"])
        if not wants_return(t) or t["type"] not in FILE_TASKS:
            raise NexusError("INVALID_TRANSITION", details={"from": st.value, "event": "UPLOADING"})
        if st == S.UPLOADING:
            return t                                   # 重送（節點重開後）：已經在傳了
        if st in (S.ACCEPTED, S.PREPARING):
            t = self.svc._transition(c, t, S.PREPARING, actor) if st == S.ACCEPTED else t
            t = self.svc._transition(c, t, S.RUNNING, actor)
        elif st != S.RUNNING:
            raise NexusError("INVALID_TRANSITION", details={"from": st.value, "event": "UPLOADING"})
        result = data.get("result")
        ok, why = self.verify(t, result)
        if not ok:
            t = self.svc._transition(c, t, S.VERIFYING, actor)
            self.tr.update_attempt(c, a["attempt_id"], status=A.FAILED.value, ended_at=utcnow(), error_code="RESULT_REJECTED")
            return self.svc._transition(c, t, S.RESULT_REJECTED, "master", why, error_code="RESULT_REJECTED", error_message=why)
        self.incoming.prepare(c, t["task_id"], a["device_id"], result["files"])
        return self.svc._transition(c, t, S.UPLOADING, actor, f"下載完成，開始傳回 {len(result['files'])} 個檔案",
                                    result_json=json.dumps(result, ensure_ascii=False))

    def _finish_upload(self, c, t, a, actor):
        st = S(t["status"])
        rows = self.incoming.of_task(t["task_id"], c)
        if st == S.CANCELLING:
            self.tr.update_attempt(c, a["attempt_id"], status=A.CANCELLED.value, ended_at=utcnow())
            self.incoming.discard_task(t["task_id"], c, status="cancelled")
            return self.svc._transition(c, t, S.CANCELLED, actor, "取消時剛好傳完")
        if st != S.UPLOADING:
            raise NexusError("INVALID_TRANSITION", details={"from": st.value, "event": "SUCCEEDED"})
        if not rows or any(r["status"] != "done" for r in rows):
            raise NexusError("UPLOAD_INCOMPLETE")
        node = json.loads(t["result_json"] or "{}")
        files = []
        for f, r in zip(node.get("files", []), rows, strict=False):
            files.append({"name": os.path.basename(r["master_path"]), "size": r["size"], "sha256": r["sha256"],
                          "path": r["master_path"], "node_path": f.get("path", "")})
        result = {**{k: v for k, v in node.items() if k != "files"}, "files": files, "returned": True}
        t = self.svc._transition(c, t, S.VERIFYING, actor)
        self.tr.update_attempt(c, a["attempt_id"], status=A.SUCCEEDED.value, ended_at=utcnow())
        return self.svc._transition(c, t, S.SUCCEEDED, "master", "檔案已傳回，大小和 SHA-256 都對",
                                    result_json=json.dumps(result, ensure_ascii=False))

    def _upload_row(self, dev: dict, upload_id: str, c=None) -> tuple[dict, dict]:
        """這筆上傳一定要是「派給這台、而且正在傳回中」的任務"""
        r = self.incoming.get(upload_id, c)
        if not r or r["device_id"] != dev["device_id"]:
            raise NexusError("UPLOAD_NOT_ALLOWED")
        t = self.tr.get_task(r["task_id"], c)
        if not t or t["assigned_device_id"] != dev["device_id"]:
            raise NexusError("UPLOAD_NOT_ALLOWED")
        if t["status"] != S.UPLOADING.value:
            raise NexusError("UPLOAD_EXPIRED" if t["status"] == S.FAILED.value else "UPLOAD_NOT_ALLOWED",
                             details={"task_status": t["status"]})
        return r, t

    def upload_open(self, dev: dict, task_id: str, body: dict) -> dict:
        t = self.tr.get_task(task_id)
        if not t or t["assigned_device_id"] != dev["device_id"] or t["current_attempt_id"] != body["attempt_id"]:
            raise NexusError("UPLOAD_NOT_ALLOWED")
        if t["status"] != S.UPLOADING.value:
            raise NexusError("UPLOAD_EXPIRED" if t["status"] == S.FAILED.value else "UPLOAD_NOT_ALLOWED",
                             details={"task_status": t["status"]})
        rows = self.incoming.of_task(task_id)
        i = body["file_index"]
        if not 0 <= i < len(rows):
            raise NexusError("UPLOAD_NOT_ALLOWED", message="沒有這個檔案")
        out = self.incoming.open_for(rows[i])
        with self.svc.db.tx() as c:
            self.incoming._update(c, rows[i]["upload_id"])          # 有動靜（D7 的 24 小時從這裡重算）
        out["rate_limit_mbps"] = float(self.rate_limit_mbps() or 0)
        return out

    def upload_chunk(self, dev: dict, upload_id: str, n: int, length: int, sha: str, stream) -> dict:
        r, _ = self._upload_row(dev, upload_id)
        return self.incoming.write_chunk(r, n, length, sha, stream)

    def upload_status(self, dev: dict, upload_id: str) -> dict:
        from nexus.master.incoming import have_list
        r = self.incoming.get(upload_id)
        if not r or r["device_id"] != dev["device_id"]:
            raise NexusError("UPLOAD_NOT_ALLOWED")
        return {"have": have_list(r), "chunks_total": r["chunks_total"], "status": r["status"]}

    def upload_complete(self, dev: dict, upload_id: str) -> dict:
        r, t = self._upload_row(dev, upload_id)
        out = self.incoming.complete(r)
        rows = self.incoming.of_task(t["task_id"])
        out["all_done"] = all(x["status"] == "done" for x in rows)
        self.svc.on_change(t)
        return out

    def retry_return(self, task_id: str, actor: str = "owner") -> dict:
        """「重新傳回」（D7）：傳回失敗／逾時的任務回到 UPLOADING，節點上線就會被提醒接著傳"""
        with self.svc.db.tx() as c:
            t = self.tr.get_task(task_id, c)
            if not t:
                raise NexusError("TASK_NOT_FOUND")
            if t["status"] != S.FAILED.value or t["error_code"] not in ("TRANSFER_FAILED", "TRANSFER_EXPIRED") \
                    or not self.incoming.of_task(task_id, c) or not t["current_attempt_id"]:
                raise NexusError("INVALID_TRANSITION", "只有「傳回失敗」的任務可以重新傳回")
            self.incoming.reopen(c, task_id)
            self.tr.update_attempt(c, t["current_attempt_id"], status=A.RUNNING.value, ended_at=None, error_code=None)
            t = self.svc._transition(c, t, S.UPLOADING, actor, "重新傳回", error_code=None, error_message=None)
        self._nudged.pop(task_id, None)
        self.svc.on_change(t)
        return t

    def upload_progress(self, task_id: str) -> dict | None:
        try:
            return self.incoming.progress(task_id)
        except Exception:
            return None

    # ---------- Master 自己驗證結果 ----------
    @staticmethod
    def verify(t: dict, result) -> tuple[bool, str]:
        m = json.loads(t["manifest_json"])
        if not isinstance(result, dict):
            return False, "結果不是物件"
        size = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        if size > m["limits"]["max_result_bytes"]:
            return False, f"結果太大（{size} bytes）"
        if t["type"] == "test.echo":
            text = m["params"]["text"]
            want = hashlib.sha256(text.encode("utf-8")).hexdigest()
            if result.get("text") != text or result.get("sha256") != want:
                return False, "回聲內容或雜湊不符"
        if t["type"] == "test.sleep" and result.get("slept") != m["params"]["seconds"]:
            return False, "睡的秒數不符"
        if t["type"] == "test.fail":
            return False, "test.fail 不該成功"
        if t["type"] in FILE_TASKS:
            files = result.get("files")
            if not isinstance(files, list) or not 1 <= len(files) <= 10:
                return False, "下載結果沒有檔案清單"
            for f in files:
                if not isinstance(f, dict):
                    return False, "檔案資料格式不對"
                name, sz, h = f.get("name"), f.get("size"), f.get("sha256")
                if not (isinstance(name, str) and 0 < len(name) <= 255 and not any(c in name for c in '\\/:')):
                    return False, "檔名不合規"
                if not (isinstance(sz, int) and not isinstance(sz, bool) and 0 < sz <= m["limits"].get("max_output_bytes", 8 << 30)):
                    return False, "檔案大小不合理"
                if not (isinstance(h, str) and _HEX64.match(h)):
                    return False, "檔案雜湊格式不對"
        return True, ""

    # ---------- 清查（每秒） ----------
    def sweep(self, now: dt.datetime | None = None):
        now = now or _now()
        changed = []
        # 1) 派出去沒 ACK、lease 過期 → 收回（確定沒開始）
        for t in self.tr.list_tasks([S.DISPATCHED.value]):
            a = self.tr.get_attempt(t["current_attempt_id"]) if t["current_attempt_id"] else None
            if a and a["status"] == A.OFFERED.value and parse_utc(a["lease_expires_at"]) <= now:
                changed += self._guard(lambda c, t=t, a=a: (
                    self.tr.update_attempt(c, a["attempt_id"], status=A.EXPIRED.value, ended_at=utcnow()),
                    self.svc._transition(c, self.tr.get_task(t["task_id"], c), S.QUEUED, "dispatcher",
                                         "派送後 30 秒沒有確認，收回重派", assigned_device_id=None, current_attempt_id=None)))
        # 2) 指派了但節點一直沒來拿 → 重新排程
        for t in self.tr.list_tasks([S.SCHEDULED.value]):
            if (now - parse_utc(t["updated_at"])).total_seconds() > SCHEDULED_STALE_SECONDS:
                changed += self._guard(lambda c, t=t: self.svc._transition(
                    c, self.tr.get_task(t["task_id"], c), S.QUEUED, "dispatcher", "節點一直沒來拿，重新排程", assigned_device_id=None))
        # 3) 逾時：超過上限＋寬限 → TIMED_OUT → FAILED，並叫節點停
        for t in self.tr.list_tasks([S.ACCEPTED.value, S.PREPARING.value, S.RUNNING.value]):
            a = self.tr.get_attempt(t["current_attempt_id"]) if t["current_attempt_id"] else None
            if not a or not a["accepted_at"]:
                continue
            limit = json.loads(t["manifest_json"])["limits"]["max_runtime_seconds"]
            if (now - parse_utc(a["accepted_at"])).total_seconds() > limit + TIMEOUT_GRACE_SECONDS:
                self.timeout_cancels.setdefault(a["device_id"], {})[t["task_id"]] = a["attempt_id"]
                changed += self._guard(lambda c, t=t, a=a: self._timeout(c, t, a))
        # 4) 過期還在排隊的 → 取消
        for t in self.tr.list_tasks([S.QUEUED.value]):
            if manifest.expired(json.loads(t["manifest_json"])):
                changed += self._guard(lambda c, t=t: self.svc._transition(
                    c, self.tr.get_task(t["task_id"], c), S.CANCELLED, "dispatcher", "任務已過期（expires_at）",
                    error_code="QUEUE_EXPIRED", error_message="排隊太久，一直沒有節點接（超過任務的有效時間）"))
        # 5) 節點離線：在它上面跑的 → UNKNOWN（傳回中的除外：檔案在節點上，等它回來接著傳，D7）
        for d in self.reg.repo.list_devices():
            if d["status"] == "ACTIVE" and d["connection_state"] in ("OFFLINE", "DISCONNECTED"):
                changed += self._lose_tasks(d["device_id"], f"節點{'離線' if d['connection_state'] == 'OFFLINE' else '斷線'}",
                                            include_uploading=False)
        # 6) V4：傳回 24 小時沒進展 → 失敗（檔案留在節點，可以「重新傳回」）
        for t in self.tr.list_tasks([S.UPLOADING.value]):
            prog = self.incoming.progress(t["task_id"])
            last = parse_utc(t["updated_at"])
            if prog:
                last = max(last, parse_utc(prog["updated_at"]))
            if (now - last).total_seconds() > UPLOAD_EXPIRY_SECONDS:
                changed += self._guard(lambda c, t=t: self._expire_upload(c, t))
        # 7) 結束了的任務還有半成品 → 刪掉
        for row in self.svc.db.conn().execute(
                "SELECT DISTINCT u.task_id FROM uploads u JOIN tasks t ON t.task_id=u.task_id"
                " WHERE u.status='open' AND t.status NOT IN (?,?,?)",
                (S.UPLOADING.value, S.CANCELLING.value, S.UNKNOWN.value)).fetchall():
            self.incoming.discard_task(row[0])
        for t in changed:
            if t:
                self.svc.on_change(t)
        return changed

    def _expire_upload(self, c, t):
        cur = self.tr.get_task(t["task_id"], c)
        if cur["status"] != S.UPLOADING.value:
            return cur
        if cur["current_attempt_id"]:
            self.tr.update_attempt(c, cur["current_attempt_id"], status=A.FAILED.value, ended_at=utcnow(), error_code="TRANSFER_EXPIRED")
        self.incoming.discard_task(cur["task_id"], c)
        msg = "傳回 24 小時沒有進展，已放棄；檔案還在節點上，可以按「重新傳回」"
        return self.svc._transition(c, cur, S.FAILED, "dispatcher", msg, error_code="TRANSFER_EXPIRED", error_message=msg)

    def _timeout(self, c, t, a):
        cur = self.tr.get_task(t["task_id"], c)
        if cur["status"] in (S.ACCEPTED.value, S.PREPARING.value):
            cur = self.svc._transition(c, cur, S.PREPARING, "dispatcher") if cur["status"] == S.ACCEPTED.value else cur
            cur = self.svc._transition(c, cur, S.RUNNING, "dispatcher")
        self.tr.update_attempt(c, a["attempt_id"], status=A.FAILED.value, ended_at=utcnow(), error_code="TIMED_OUT")
        cur = self.svc._transition(c, cur, S.TIMED_OUT, "dispatcher", "超過執行時間上限（Master 判斷）")
        return self.svc._transition(c, cur, S.FAILED, "dispatcher", "逾時", error_code="TIMED_OUT", error_message="超過執行時間上限")

    def _guard(self, fn) -> list:
        try:
            with self.svc.db.tx() as c:
                r = fn(c)
            return [r[-1] if isinstance(r, tuple) else r]
        except NexusError as e:
            log.info("清查跳過：%s", e.code)
            return []

    # ---------- 節點被撤銷／暫停／過期／離線 ----------
    def device_changed(self, device_id: str, status: str):
        self._lose_tasks(device_id, {"REVOKED": "節點被撤銷", "SUSPENDED": "節點被暫停", "EXPIRED": "節點憑證過期"}.get(status, status))
        self.notify()

    def _lose_tasks(self, device_id: str, why: str, include_uploading: bool = True) -> list:
        out = []
        statuses = [S.SCHEDULED.value, S.DISPATCHED.value] + [s.value for s in ON_WORKER
                                                              if include_uploading or s != S.UPLOADING]
        for t in self.tr.tasks_on_device(device_id, statuses):
            def fn(c, t=t):
                cur = self.tr.get_task(t["task_id"], c)
                a = self.tr.get_attempt(cur["current_attempt_id"], c) if cur["current_attempt_id"] else None
                if cur["status"] in (S.SCHEDULED.value, S.DISPATCHED.value):
                    if a and a["status"] == A.OFFERED.value:
                        self.tr.update_attempt(c, a["attempt_id"], status=A.EXPIRED.value, ended_at=utcnow())
                    return self.svc._transition(c, cur, S.QUEUED, "dispatcher", f"{why}，改派別台", assigned_device_id=None,
                                                current_attempt_id=None)
                if a:
                    self.tr.update_attempt(c, a["attempt_id"], status=A.LOST.value)
                return self.svc._transition(c, cur, S.UNKNOWN, "dispatcher", f"{why}，不知道任務有沒有跑完（不會自動重跑）")
            out += self._guard(fn)
        return out

    # ---------- 給 GUI ----------
    def adapters_info(self):
        return {n: {"version": a.version, "description": a.description} for n, a in adapters.ADAPTERS.items()}

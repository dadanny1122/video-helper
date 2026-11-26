"""
TaskService：任務狀態唯一的修改入口（規格 §11.2）。
  ・每次轉移：檢查目前狀態 → 目標狀態合法 → 樂觀鎖（version）→ 同一個交易寫 tasks＋audit_events
  ・建立：組 manifest → CREATED → VALIDATING → QUEUED（或 REJECTED），idempotency key 去重（AT-004）
  ・取消：還沒到 Worker 的直接 CANCELLED；已在 Worker 上的先 CANCELLING，等 Worker 確認（AT-015）
  ・失敗：照 retry policy 決定重試（新 attempt）或停在 FAILED（規格 §13.2）
GUI 與派送層都只呼叫這裡，不直接改資料庫。
"""
import datetime as dt
import json
import logging

from nexus.core.errors import NexusError
from nexus.core.models import AuditEvent, utcnow
from nexus.storage.task_repo import TaskRepo
from nexus.tasks import adapters, manifest
from nexus.tasks.states import (
    NON_RETRYABLE_ERRORS,
    ON_WORKER,
    RETRYABLE_ERRORS,
    TERMINAL,
    AttemptStatus,
    TaskStatus,
    can_transition,
)

log = logging.getLogger("nexus.tasks")
S = TaskStatus
# 比對 idempotency 時看的內容（task_id、時間每次都不同，不算）
_CONTENT_KEYS = ("type", "adapter_version", "params", "limits", "requirements", "retry_policy", "priority")


def _store_json(m: dict) -> str:
    """超大的不合格 manifest 只存摘要（不存垃圾）"""
    j = json.dumps(m, ensure_ascii=False)
    if len(j.encode("utf-8")) > manifest.MAX_MANIFEST_BYTES:
        j = json.dumps({"truncated": True, "type": str(m.get("type"))[:64]}, ensure_ascii=False)
    return j


def content_of(m: dict) -> str:
    return json.dumps({k: m.get(k) for k in _CONTENT_KEYS}, sort_keys=True, ensure_ascii=False)


class TaskService:
    def __init__(self, db, repo, task_repo: TaskRepo | None = None, on_change=None):
        self.db = db
        self.repo = repo                          # V1 Repo：寫 audit
        self.tr = task_repo or TaskRepo(db)
        self.on_change = on_change or (lambda *_: None)   # 狀態變了通知排程器／GUI（不在交易裡呼叫）

    # ---------- 內部 ----------
    def _audit(self, c, action, actor, task_id, outcome="ok", reason="", correlation_id="", **details):
        self.repo.audit(AuditEvent(action, actor, "task", task_id, outcome, reason, correlation_id, details), c)

    def _transition(self, c, task: dict, new: TaskStatus, actor: str, reason: str = "", correlation_id: str = "",
                    **fields) -> dict:
        """在呼叫者的交易裡做一次轉移；回傳更新後的 task"""
        cur = TaskStatus(task["status"])
        if not can_transition(cur, new):
            raise NexusError("INVALID_TRANSITION", f"任務狀態 {cur.value} 不能變成 {new.value}",
                             details={"from": cur.value, "to": new.value})
        if not self.tr.update_task(c, task["task_id"], task["version"], status=new.value, **fields):
            raise NexusError("CONCURRENT_UPDATE")
        self._audit(c, f"task.{new.value.lower()}", actor, task["task_id"], reason=reason, correlation_id=correlation_id,
                    previous=cur.value, **{k: v for k, v in fields.items() if k in ("assigned_device_id", "error_code")})
        return self.tr.get_task(task["task_id"], c)

    def transition(self, task_id: str, new: TaskStatus, actor: str, reason: str = "", expected_version: int | None = None,
                   correlation_id: str = "", **fields) -> dict:
        """單一入口（自己開交易）"""
        with self.db.tx() as c:
            t = self.tr.get_task(task_id, c)
            if not t:
                raise NexusError("TASK_NOT_FOUND")
            if expected_version is not None and t["version"] != expected_version:
                raise NexusError("CONCURRENT_UPDATE")
            t = self._transition(c, t, TaskStatus(new), actor, reason, correlation_id, **fields)
        self.on_change(t)
        return t

    # ---------- 建立 ----------
    def create(self, task_type: str, params: dict, actor: str = "owner", **opts) -> dict:
        """回傳 task dict（可能是已存在的那筆）。不合格的任務會被記成 REJECTED（不會被排程）"""
        try:
            m = manifest.build(task_type, params, **opts)
        except TypeError as e:
            raise NexusError("MANIFEST_INVALID", "選項不對", details={"problem": str(e)[:200]}) from e
        return self.submit(m, actor)

    def submit(self, m: dict, actor: str = "owner") -> dict:
        key = m.get("idempotency_key") if isinstance(m, dict) else None
        if isinstance(key, str) and key:
            old = self.tr.task_by_idempotency_key(key)
            if old:
                if content_of(json.loads(old["manifest_json"])) == content_of(m):
                    return old                                    # AT-004：同 key 同內容 → 回傳原本那筆
                raise NexusError("IDEMPOTENCY_CONFLICT", details={"task_id": old["task_id"]})
        problems = manifest.validate(m)
        if problems and not (isinstance(m, dict) and isinstance(key, str) and 1 <= len(key) <= 64
                             and isinstance(m.get("task_id"), str) and isinstance(m.get("type"), str)):
            # 連基本欄位都沒有 → 不建立任何資料，直接拒絕
            raise NexusError("MANIFEST_INVALID", details={"problems": problems[:10]})
        now = utcnow()
        spec = adapters.get(m["type"])
        rp = m.get("retry_policy") if isinstance(m.get("retry_policy"), dict) else {}
        row = dict(task_id=m["task_id"], type=m["type"][:64], adapter_version=str(m.get("adapter_version", ""))[:16],
                   manifest_json=_store_json(m),
                   manifest_sha256=manifest.sha256(m), idempotency_key=key, status=S.CREATED.value,
                   priority=m["priority"] if isinstance(m.get("priority"), int) and not isinstance(m.get("priority"), bool) else 0,
                   max_attempts=rp.get("max_attempts", 1) if isinstance(rp.get("max_attempts"), int) else 1,
                   created_by=actor, created_at=now, updated_at=now,
                   expires_at=m.get("expires_at") if isinstance(m.get("expires_at"), str) else now)
        with self.db.tx() as c:
            try:
                self.tr.insert_task(c, row)
            except Exception as e:                                # task_id 撞了（不太可能）或 key 剛好被同時用掉
                raise NexusError("IDEMPOTENCY_CONFLICT") from e
            self._audit(c, "task.created", actor, row["task_id"], type=row["type"], idempotency_key=key)
            t = self.tr.get_task(row["task_id"], c)
            t = self._transition(c, t, S.VALIDATING, actor)
            if problems:
                t = self._transition(c, t, S.REJECTED, actor, reason="；".join(problems)[:500],
                                     error_code="MANIFEST_INVALID", error_message="；".join(problems)[:1000])
            elif spec is None:
                t = self._transition(c, t, S.REJECTED, actor, error_code="ADAPTER_UNSUPPORTED")
            else:
                t = self._transition(c, t, S.QUEUED, actor)
        log.info("新任務 %s %s → %s", row["task_id"][:8], row["type"], t["status"])
        self.on_change(t)
        return t

    # ---------- 取消 ----------
    def cancel(self, task_id: str, actor: str, reason: str) -> dict:
        if not reason or not reason.strip():
            raise NexusError("SCHEMA_INVALID", "取消要填理由")
        with self.db.tx() as c:
            t = self.tr.get_task(task_id, c)
            if not t:
                raise NexusError("TASK_NOT_FOUND")
            st = TaskStatus(t["status"])
            if st == S.CANCELLING:
                return t
            if st in (S.CREATED, S.QUEUED, S.SCHEDULED, S.DISPATCHED):
                if t["current_attempt_id"] and st == S.DISPATCHED:
                    # Worker 要等 Master 回 ACK 才會開始 → 作廢這次派送即可
                    self.tr.update_attempt(c, t["current_attempt_id"], status=AttemptStatus.CANCELLED.value, ended_at=utcnow())
                t = self._transition(c, t, S.CANCELLED, actor, reason)
            elif st in ON_WORKER:
                t = self._transition(c, t, S.CANCELLING, actor, reason)   # 派送層會把取消指令送給 Worker
            else:
                raise NexusError("INVALID_TRANSITION", f"{st.value} 的任務不能取消")
        self.on_change(t)
        return t

    def mark_failed(self, task_id: str, actor: str, reason: str) -> dict:
        """UNKNOWN 的任務：人工確認後標記失敗（不自動重跑；V4 才做自動核對）"""
        if not reason or not reason.strip():
            raise NexusError("SCHEMA_INVALID", "要填理由")
        with self.db.tx() as c:
            t = self.tr.get_task(task_id, c)
            if not t:
                raise NexusError("TASK_NOT_FOUND")
            if t["status"] != S.UNKNOWN.value:
                raise NexusError("INVALID_TRANSITION", "只有「狀態不明」的任務需要人工標記")
            t = self._transition(c, t, S.FAILED, actor, reason, error_code="MANUAL", error_message=reason[:500])
        self.on_change(t)
        return t

    # ---------- 失敗與重試（派送層呼叫，在它的交易裡） ----------
    def fail_in_tx(self, c, t: dict, error_code: str, message: str, actor: str) -> dict:
        """Attempt 失敗：先到 FAILED；能重試就再回 QUEUED（新 attempt 由排程器建立）"""
        t = self._transition(c, t, S.FAILED, actor, message[:300], error_code=error_code[:64], error_message=message[:1000])
        if self.should_retry(t, error_code):
            wait = self.retry_delay(t, error_code)
            at = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=wait)).isoformat(timespec="seconds").replace("+00:00", "Z")
            note = f"，{wait:g} 秒後" if wait > 0 else ""
            t = self._transition(c, t, S.QUEUED, actor, f"自動重試（第 {t['attempt_count'] + 1} 次{note}）",
                                 assigned_device_id=None, current_attempt_id=None, retry_at=at if wait > 0 else None)
        return t

    @staticmethod
    def retry_delay(t: dict, error_code: str) -> float:
        """照 Adapter 的重試政策算要等幾秒（第幾次重試＝已經跑過幾次）"""
        spec = adapters.get(t["type"])
        policy = spec.retry if spec else adapters.RetryPolicy()
        return policy.delay(max(1, int(t["attempt_count"])), error_code)

    @staticmethod
    def should_retry(t: dict, error_code: str) -> bool:
        spec = adapters.get(t["type"])
        if not spec or not spec.retry_safe or error_code in NON_RETRYABLE_ERRORS or error_code not in RETRYABLE_ERRORS:
            return False
        if manifest.expired(json.loads(t["manifest_json"])):
            return False
        return t["attempt_count"] < t["max_attempts"]

    # ---------- 查詢 ----------
    def get(self, task_id: str) -> dict | None:
        return self.tr.get_task(task_id)

    def list(self, active_only=False, limit=300) -> list[dict]:
        if active_only:
            return self.tr.list_tasks([s.value for s in TaskStatus if s not in TERMINAL and s != S.FAILED], limit)
        return self.tr.list_tasks(None, limit)

    def detail(self, task_id: str) -> dict:
        t = self.tr.get_task(task_id)
        if not t:
            raise NexusError("TASK_NOT_FOUND")
        return {"task": t, "manifest": json.loads(t["manifest_json"]), "attempts": self.tr.attempts_of(task_id),
                "events": self.tr.events_of(task_id), "decisions": self.tr.decisions_of(task_id),
                "audit": list(reversed(self.repo.audit_tail(200, task_id)))}

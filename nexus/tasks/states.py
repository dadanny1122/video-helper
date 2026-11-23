"""
任務狀態機（規格 §11.1 的 19 個狀態）。轉移表寫死在這裡；唯一能改狀態的地方是 TaskService.transition()。

跟規格摘要表的差異（都有理由，寫在 06-V2技術規格.md §3.3）：
  ・DISPATCHED → CANCELLED：Worker 一定要等 Master 回覆 ACK 才開始，派出去還沒 ACK 時取消＝作廢 lease 即可
  ・ACCEPTED／PREPARING → CANCELLING：Worker 可能已經在準備，要等它確認停止
  ・ACCEPTED／PREPARING／RUNNING／UPLOADING／CANCELLING → UNKNOWN：節點離線或被撤銷
  ・RUNNING → VERIFYING：V2 的測試結果很小，直接放在完成事件裡，不走 UPLOADING
  ・FAILED → QUEUED：只有 retry policy 允許時（建立新 attempt，舊的保留）
"""
from enum import Enum


class TaskStatus(str, Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    QUEUED = "QUEUED"
    SCHEDULED = "SCHEDULED"
    DISPATCHED = "DISPATCHED"
    ACCEPTED = "ACCEPTED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    UPLOADING = "UPLOADING"
    VERIFYING = "VERIFYING"
    CANCELLING = "CANCELLING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    REJECTED = "REJECTED"
    RESULT_REJECTED = "RESULT_REJECTED"
    CANCELLED = "CANCELLED"
    TIMED_OUT = "TIMED_OUT"
    UNKNOWN = "UNKNOWN"
    RECONCILING = "RECONCILING"


S = TaskStatus
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    S.CREATED: {S.VALIDATING, S.CANCELLED},
    S.VALIDATING: {S.QUEUED, S.REJECTED, S.FAILED},
    S.QUEUED: {S.SCHEDULED, S.CANCELLED},
    S.SCHEDULED: {S.DISPATCHED, S.QUEUED, S.CANCELLED},
    S.DISPATCHED: {S.ACCEPTED, S.QUEUED, S.UNKNOWN, S.CANCELLED},
    S.ACCEPTED: {S.PREPARING, S.FAILED, S.CANCELLING, S.UNKNOWN},
    S.PREPARING: {S.RUNNING, S.FAILED, S.CANCELLING, S.UNKNOWN},
    S.RUNNING: {S.UPLOADING, S.VERIFYING, S.FAILED, S.CANCELLING, S.TIMED_OUT, S.UNKNOWN},
    S.UPLOADING: {S.VERIFYING, S.FAILED, S.UNKNOWN},
    S.VERIFYING: {S.SUCCEEDED, S.RESULT_REJECTED, S.FAILED},
    S.CANCELLING: {S.CANCELLED, S.FAILED, S.UNKNOWN},
    S.TIMED_OUT: {S.FAILED},
    S.FAILED: {S.QUEUED},                       # 只有重試；TaskService 另外檢查 retry policy
    S.UNKNOWN: {S.RECONCILING, S.FAILED, S.RUNNING},
    S.RECONCILING: {S.QUEUED, S.RUNNING, S.VERIFYING, S.FAILED},
    S.SUCCEEDED: set(),
    S.REJECTED: set(),
    S.RESULT_REJECTED: set(),
    S.CANCELLED: set(),
}
assert set(TRANSITIONS) == set(TaskStatus), "每個狀態都要在轉移表裡"

TERMINAL = {s for s, nxt in TRANSITIONS.items() if not nxt}
# FAILED 對「這次嘗試」是終點；任務本身可能因重試回到 QUEUED
DONE = TERMINAL | {S.FAILED}
# 已經交給 Worker（Worker 可能正在做）的狀態：節點離線／撤銷時要變 UNKNOWN，不能直接重派
ON_WORKER = {S.ACCEPTED, S.PREPARING, S.RUNNING, S.UPLOADING, S.CANCELLING}
CANCELLABLE = {S.CREATED, S.QUEUED, S.SCHEDULED, S.DISPATCHED, S.ACCEPTED, S.PREPARING, S.RUNNING}


def can_transition(cur: TaskStatus | str, new: TaskStatus | str) -> bool:
    return TaskStatus(new) in TRANSITIONS[TaskStatus(cur)]


class AttemptStatus(str, Enum):
    OFFERED = "OFFERED"          # 已派出，等 ACK
    ACCEPTED = "ACCEPTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"          # lease 到期沒 ACK（確定沒開始）
    DECLINED = "DECLINED"        # Worker 拒收
    LOST = "LOST"                # ACK 後節點不見（不知道有沒有跑）


ATTEMPT_OPEN = {AttemptStatus.OFFERED, AttemptStatus.ACCEPTED, AttemptStatus.RUNNING}

# Worker 回報的錯誤 → 能不能自動重試（規格 §13.2）
RETRYABLE_ERRORS = {"ADAPTER_ERROR", "NETWORK", "RATE_LIMITED"}
NON_RETRYABLE_ERRORS = {"INVALID_INPUT", "RESOURCE_LIMIT", "TIMED_OUT", "MANIFEST_INVALID", "ADAPTER_UNSUPPORTED",
                        # V3 下載／沙箱：重試也不會好，要人處理
                        "NET_BLOCKED", "NOT_FOUND", "DISK_FULL", "SANDBOX_UNAVAILABLE", "OUTPUT_REJECTED",
                        "TOO_BIG", "CLEANUP_FAILED", "LEFTOVER_PROCESSES", "PROTOCOL_UNSUPPORTED", "SITE_REFUSED"}

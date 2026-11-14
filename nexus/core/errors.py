"""錯誤碼與對外回應格式（規格 §14.3）。對外絕不帶堆疊、路徑、金鑰。"""
import uuid
from datetime import datetime, timezone

# 錯誤碼 → (HTTP 狀態, 可重試？, 給人看的說明)
ERROR_CODES = {
    "PAIR_CODE_INVALID": (403, False, "配對碼不對"),
    "PAIR_CODE_EXPIRED": (403, False, "配對碼已過期，請在 Master 重新產生"),
    "PAIR_CODE_BURNED": (403, False, "配對碼猜錯太多次已作廢，請在 Master 重新產生"),
    "REGISTRATION_NOT_FOUND": (404, False, "找不到這筆註冊"),
    "DEVICE_PENDING": (403, True, "裝置還沒被核准"),
    "DEVICE_NOT_ACTIVE": (403, False, "裝置不是啟用狀態"),
    "DEVICE_SUSPENDED": (403, True, "裝置已被暫停"),
    "CREDENTIAL_REVOKED": (403, False, "憑證已被撤銷，請重新配對"),
    "CREDENTIAL_EXPIRED": (403, False, "憑證已過期，請重新配對"),
    "CREDENTIAL_MISMATCH": (403, False, "憑證身分與請求的裝置不符"),
    "PROTOCOL_INCOMPATIBLE": (426, False, "協定版本不相容，請更新"),
    "AGENT_VERSION_TOO_OLD": (426, False, "Worker 版本太舊，請更新"),
    "REPLAY_DETECTED": (409, False, "重複的訊息"),
    "CLOCK_SKEW": (400, True, "時間差太大，請校正電腦時鐘"),
    "RATE_LIMITED": (429, True, "請求太頻繁"),
    "PAYLOAD_TOO_LARGE": (413, False, "資料太大"),
    "SCHEMA_INVALID": (400, False, "資料格式不對"),
    "NOT_FOUND": (404, False, "找不到"),
    "METHOD_NOT_ALLOWED": (405, False, "不支援的方法"),
    "INTERNAL_ERROR": (500, True, "內部錯誤"),
}


class NexusError(Exception):
    def __init__(self, code: str, message: str | None = None, details: dict | None = None, retry_after: int | None = None):
        if code not in ERROR_CODES:
            code = "INTERNAL_ERROR"
        self.code = code
        self.http_status, self.retryable, default = ERROR_CODES[code]
        self.message = message or default
        self.details = details or {}
        self.retry_after = retry_after
        super().__init__(f"{code}: {self.message}")

    def to_response(self, correlation_id: str | None = None, protocol_version: str = "1.0") -> dict:
        return {
            "protocol_version": protocol_version,
            "message_id": str(uuid.uuid4()),
            "correlation_id": correlation_id or str(uuid.uuid4()),
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "error": {"code": self.code, "message": self.message, "retryable": self.retryable, "details": self.details},
        }

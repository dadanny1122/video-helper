"""裝置／憑證／連線的狀態，以及「哪些轉移是合法的」（規格 §8.2、§11.2 的精神：狀態只能照表走）"""
from enum import Enum


class DeviceStatus(str, Enum):
    PENDING = "PENDING"        # 已提出註冊，等人核准；只能查註冊狀態
    ACTIVE = "ACTIVE"          # 已核准、憑證有效
    SUSPENDED = "SUSPENDED"    # 使用者暫停；拒絕一切，但保留身分，可恢復
    REVOKED = "REVOKED"        # 撤銷；終態，永遠不能回 ACTIVE
    EXPIRED = "EXPIRED"        # 憑證過期沒更新；終態，要重新註冊成新裝置
    REJECTED = "REJECTED"      # 註冊被拒；終態


# 合法轉移表：key → 可以變成的狀態。沒列的一律拒絕。
DEVICE_TRANSITIONS = {
    DeviceStatus.PENDING: {DeviceStatus.ACTIVE, DeviceStatus.REJECTED},
    DeviceStatus.ACTIVE: {DeviceStatus.SUSPENDED, DeviceStatus.REVOKED, DeviceStatus.EXPIRED},
    DeviceStatus.SUSPENDED: {DeviceStatus.ACTIVE, DeviceStatus.REVOKED, DeviceStatus.EXPIRED},
    DeviceStatus.REVOKED: set(),
    DeviceStatus.EXPIRED: set(),
    DeviceStatus.REJECTED: set(),
}
TERMINAL_DEVICE_STATES = {s for s, nxt in DEVICE_TRANSITIONS.items() if not nxt}


def can_transition(cur: DeviceStatus, new: DeviceStatus) -> bool:
    return new in DEVICE_TRANSITIONS.get(DeviceStatus(cur), set())


class ConnectionState(str, Enum):
    """Master 對一台裝置「現在連著嗎」的看法（V1 只影響顯示與稽核）"""
    UNKNOWN = "UNKNOWN"            # Master 剛啟動，還沒收到心跳
    CONNECTED = "CONNECTED"        # 心跳正常
    STALE = "STALE"                # 連續 3 次沒心跳（30 秒）
    OFFLINE = "OFFLINE"            # 超過 5 分鐘沒心跳
    DISCONNECTED = "DISCONNECTED"  # Worker 自己說要下線


class CredentialStatus(str, Enum):
    ACTIVE = "ACTIVE"
    SUPERSEDED = "SUPERSEDED"      # 已換新憑證，舊的在重疊期內還能用
    REVOKED = "REVOKED"
    EXPIRED = "EXPIRED"


HEARTBEAT_SECONDS = 10
STALE_AFTER_SECONDS = 30
OFFLINE_AFTER_SECONDS = 300

"""
Video Helper Nexus：Master／Worker 節點連線層（V1：註冊、核准、心跳、撤銷）
規格與決策見 docs/nexus/。這個套件不碰既有下載器的任何狀態檔。
"""
NEXUS_VERSION = "0.7.0"          # Nexus 元件版本（跟 App 的 VERSION 分開）
# 控制協定版本；major 不同就拒絕（1.1＝V2：心跳帶節點能力、任務派送；1.2＝V3：能力多沙箱等級；1.3＝V4：檔案傳回 Master；
#   1.4＝APEX PR2：能力多強隔離偵測結果；1.5＝能力多自訂資源 named）
PROTOCOL_VERSION = "1.5"
RETURN_MIN_PROTOCOL = "1.3"      # 要這個版本以上才會被派「下載完傳回」的任務
NAMED_MIN_PROTOCOL = "1.5"       # Master 要這個版本以上，節點才送自訂資源 named（舊版會整份能力丟掉）
HARDENING_MIN_PROTOCOL = "1.4"   # Master 要這個版本以上，節點才送 hardening 欄位（舊版會整份能力丟掉）
TASKS_MIN_PROTOCOL = "1.1"       # 要這個版本以上才會被派任務（1.0 的 Worker 照樣能心跳）
MIN_PEER_PROTOCOL = "1.0"        # Master 接受的最低 Worker 協定版本
MIN_AGENT_VERSION = "0.1.0"      # Master 接受的最低 Worker Agent 版本


def protocol_compatible(peer: str, mine: str = PROTOCOL_VERSION) -> bool:
    """協定相容＝major 相同（"1.0" 跟 "1.3" 相容，跟 "2.0" 不相容）"""
    try:
        return int(str(peer).split(".")[0]) == int(str(mine).split(".")[0])
    except (ValueError, AttributeError):
        return False


def version_tuple(v: str) -> tuple:
    return tuple(int(x) for x in str(v).split("."))

"""
Video Helper Nexus：Master／Worker 節點連線層（V1：註冊、核准、心跳、撤銷）
規格與決策見 docs/nexus/。這個套件不碰既有下載器的任何狀態檔。
"""
NEXUS_VERSION = "0.1.0"          # Nexus 元件版本（跟 App 的 VERSION 分開）
PROTOCOL_VERSION = "1.0"         # 控制協定版本；major 不同就拒絕
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

"""
Nexus 畫面上用的中文名稱、時間／大小格式（S5，docs/infra/00-架構稽核與改善計畫.md）。
不碰 tkinter：原本的 Tk 分頁（nexus/gui/）和新介面（電腦版的 _web_nexus_full）共用這一份。
"""
import json

from nexus.core.models import parse_utc, utcnow

# ---- 裝置（節點）----
KIND_NAME = {"loopback": "只有這台電腦", "lan": "區域網路", "vpn": "VPN（Tailscale）", "all": "所有網卡（不建議）"}
DEVICE_STATE_NAME = {"PENDING": "等待核准", "ACTIVE": "已核准", "SUSPENDED": "已暫停", "REVOKED": "已撤銷", "EXPIRED": "憑證過期",
                     "REJECTED": "已拒絕"}
CONN_NAME = {"CONNECTED": "連線中", "STALE": "心跳延遲", "OFFLINE": "離線", "DISCONNECTED": "已斷線", "UNKNOWN": "—"}


def ago(ts):
    if not ts:
        return "—"
    s = int((parse_utc(utcnow()) - parse_utc(ts)).total_seconds())
    return f"{s} 秒前" if s < 90 else f"{s // 60} 分前" if s < 5400 else f"{s // 3600} 小時前"


def local_min(ts):
    """UTC → 本機時間 MM-DD HH:MM"""
    if not ts:
        return "—"
    try:
        return parse_utc(ts).astimezone().strftime("%m-%d %H:%M")
    except Exception:
        return ts[5:16]


# ---- 任務 ----
TYPE_NAME = {"test.echo": "回聲", "test.sleep": "等待", "test.fail": "故意失敗", "test.file": "測試檔（測傳回）",
             "sim.pvwatts": "太陽光電模擬（PySAM）",
             "video.download": "下載影片（節點）"}
FILE_TYPES = ("video.download", "test.file")
RETURN_ERRORS = ("TRANSFER_FAILED", "TRANSFER_EXPIRED")
TASK_STATE_NAME = {
    "CREATED": "已建立", "VALIDATING": "檢查中", "QUEUED": "排隊中", "SCHEDULED": "已選節點", "DISPATCHED": "已派出（等確認）",
    "ACCEPTED": "節點已接受", "PREPARING": "準備中", "RUNNING": "執行中", "UPLOADING": "傳回中", "VERIFYING": "驗證結果",
    "CANCELLING": "取消中…", "SUCCEEDED": "成功", "FAILED": "失敗", "REJECTED": "不合格（已拒絕）", "RESULT_REJECTED": "結果不通過",
    "CANCELLED": "已取消", "TIMED_OUT": "逾時", "UNKNOWN": "狀態不明（節點失聯）", "RECONCILING": "核對中"}
TAG = {"SUCCEEDED": "ok", "FAILED": "bad", "REJECTED": "bad", "RESULT_REJECTED": "bad", "TIMED_OUT": "bad", "UNKNOWN": "warn",
       "CANCELLED": "muted", "CANCELLING": "warn", "QUEUED": "muted"}
def sim_result(result_json: str) -> str:
    """sim.pvwatts 結果 → 白話（年發電量、每 kW、容量因數、各月）"""
    import json
    try:
        r = json.loads(result_json)
        months = "、".join(f"{i + 1}月 {v / 1000:,.1f}" for i, v in enumerate(r["monthly_kwh"]))
        w = r.get("weather") or {}
        src = w.get("source", "—") + ("（用快取）" if w.get("cached") else "")
        miss = w.get("missing_hours") or 0
        lines = [f"年發電量 {r['annual_kwh'] / 1000:,.1f} MWh（每 kW {r['kwh_per_kw']:,.0f} kWh，容量因數 {r['capacity_factor']:.2f}%）",
                 f"各月（MWh）：{months}", f"氣象：{src}", f"PySAM {r.get('pysam_version', '?')}"]
        if miss:
            lines.insert(3, f"日照缺值 {miss} 小時（當 0 算）")
        return "\n".join(lines)
    except Exception:
        return "結果：" + str(result_json)[:200]


# APEX（借 Temporal「逾時分三種」）：同樣是失敗／取消，看得出是哪一種
WHY_NAME = {"TIMED_OUT": "執行太久", "QUEUE_EXPIRED": "排隊太久，沒有節點接"}


def task_text(t: dict, now: str | None = None) -> str:
    """任務狀態的中文：排隊中但在等自動重試 → 寫幾點重試；失敗／取消 → 括號寫原因（逾時那幾種）"""
    st = t.get("status", "")
    text = TASK_STATE_NAME.get(st, st)
    if st == "QUEUED" and t.get("retry_at"):
        from nexus.core.models import utcnow
        if t["retry_at"] > (now or utcnow()):
            return f"等待重試（{local_min(t['retry_at'])} 再試）"
    why = WHY_NAME.get(t.get("error_code") or "")
    if why and st in ("FAILED", "CANCELLED"):
        return f"{text}（{why}）"
    return text


SCORE_NAME = {"cpu": "CPU", "memory": "記憶體", "disk": "磁碟", "queue": "空閒", "reliability": "可靠度"}


def local(ts):
    if not ts:
        return "—"
    try:
        return parse_utc(ts).astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:
        return ts


def summary_of(task: dict) -> str:
    try:
        p = json.loads(task["manifest_json"]).get("params", {})
    except Exception:
        return ""
    if "text" in p:
        t = str(p["text"])
        return t if len(t) <= 24 else t[:23] + "…"
    if "seconds" in p:
        return f"{p['seconds']} 秒"
    if "code" in p:
        return str(p["code"])
    if "size_mb" in p:
        return f"{p['size_mb']} MB" + ("，傳回" if p.get("return_to_master") else "")
    if "url" in p:                                    # 下載影片：只顯示網站和最後一段，不顯示完整網址
        try:
            import urllib.parse
            u = urllib.parse.urlsplit(str(p["url"]))
            tail = u.path.rstrip("/").rsplit("/", 1)[-1] or u.query
            q = p.get("quality", "best")
            back = "，傳回" if p.get("return_to_master") else ""
            return f"{(u.hostname or '').removeprefix('www.')}／{tail[:14]}（{'最高' if q == 'best' else q + 'p'}{back}）"
        except Exception:
            return "影片"
    return ""


def fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"


def download_result(raw: str, node: str, status: str = "SUCCEEDED") -> str:
    """下載結果：檔名、大小、存在哪台的哪裡（V4：傳回成功就寫這台的路徑；還沒傳完／失敗就寫留在節點）"""
    try:
        r = json.loads(raw)
    except Exception:
        return "結果：" + raw[:200]
    lines = [f"下載完成：{r.get('site', '')}　{str(r.get('title', ''))[:60]}".rstrip()]
    for f in r.get("files", [])[:5]:
        if r.get("returned"):
            lines.append(f"・{f.get('name')}（{fmt_size(f.get('size'))}）已傳回這台：{f.get('path', '')}")
        else:
            where = "還在傳回中，目前在" if status == "UPLOADING" else "存在"
            lines.append(f"・{f.get('name')}（{fmt_size(f.get('size'))}）{where}「{node}」：{f.get('path', '')}")
    return "\n".join(lines)


def who(actor: str, names: dict) -> str:
    if actor.startswith("worker:"):
        return "節點 " + names.get(actor[7:], actor[7:15])
    names = {"owner": "你", "browser": "瀏覽器（交給節點）", "scheduler": "排程器", "dispatcher": "派送", "master": "Master 驗證"}
    return names.get(actor, actor)


def isolation_text(res: dict | None, known: bool = True) -> str:
    """節點「沙箱」欄（Tk 節點分頁、新介面共用）：基本／不能用＋原因；APEX PR2 再加上強隔離偵測結果"""
    res = res or {}
    if "isolation" not in res:
        return "—（舊版節點）" if known else "—"
    base = "基本（可下載）" if res["isolation"] == "basic" else "不能用：" + (res.get("isolation_reason") or "原因不明")[:40]
    h = res.get("hardening")
    if h == "appcontainer":
        base += "；強隔離：可以用（還沒啟用）"
    elif h == "none":
        base += "；強隔離：不能用（" + (res.get("hardening_reason") or "原因不明")[:30] + "）"
    if res.get("named"):
        from nexus.tasks.capabilities import named_text
        base += "；資源：" + named_text(res["named"])
    return base

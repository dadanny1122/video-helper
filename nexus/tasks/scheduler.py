"""
排程器（規格 §12、06-V2 §4）：先硬性篩選、再對合格節點評分；每次決定都存下來，GUI 要能說出「為什麼選這台、別台為什麼不行」。
  ・硬性條件不合格的節點，分數再高也不會被選（安全條件是篩選，不是加分）
  ・資源數字是 Worker 自己報的：只拿來挑，不拿來授權（規格 25.3）
  ・同分：執行中任務少的優先 → device_id 字典序（AT-017：結果可重現）
這裡只把任務指派給節點（QUEUED → SCHEDULED）；真的派出去（建立 lease）是 Worker 來拿的時候（dispatch.py）。
"""
import datetime as dt
import json
import logging
import uuid

from nexus import TASKS_MIN_PROTOCOL, version_tuple
from nexus.core.enums import STALE_AFTER_SECONDS
from nexus.core.errors import NexusError
from nexus.core.models import parse_utc, utcnow
from nexus.tasks import adapters
from nexus.tasks.states import TaskStatus

log = logging.getLogger("nexus.scheduler")
S = TaskStatus

# 權重：有版本號，改了要換版本（決策紀錄會寫是哪一版）。總和必須＝1（規格 §12.2）
WEIGHTS = {"w1": {"cpu": 0.25, "memory": 0.25, "disk": 0.10, "queue": 0.25, "reliability": 0.15}}
CURRENT_WEIGHTS = "w1"
MEMORY_REF_MB, DISK_REF_MB = 4096, 10240
# 佔用節點名額的狀態（UNKNOWN 也算：可能還在跑，保守一點）
BUSY = (S.SCHEDULED, S.DISPATCHED, S.ACCEPTED, S.PREPARING, S.RUNNING, S.UPLOADING, S.CANCELLING, S.UNKNOWN)

REASONS = {
    "not_active": "節點不是「已核准」狀態（暫停／撤銷／等待核准）",
    "not_connected": "心跳超過 30 秒沒來",
    "old_protocol": "節點版本太舊，不能接任務（請更新）",
    "no_capabilities": "節點沒回報能力資料",
    "not_accepting": "節點設定成「不接任務」",
    "adapter_missing": "節點不支援這種任務",
    "adapter_version": "節點的任務版本不相容",
    "platform": "平台不符",
    "memory": "可用記憶體不夠",
    "disk": "可用磁碟不夠",
    "full": "節點同時任務數已滿",
    "declined": "這台之前拒收過這個任務",
    "no_sandbox": "節點的沙箱不能用（下載影片一定要在沙箱裡跑）",
}


def _clip(x):
    return max(0.0, min(1.0, float(x)))


def evaluate(task: dict, manifest: dict, devices: list[dict], caps: dict, busy: dict, history: dict, declined: set,
             now: dt.datetime | None = None, weights_version: str = CURRENT_WEIGHTS) -> list[dict]:
    """純函式：回傳每台節點的候選資料（eligible、reasons、scores、total），已排序（第一個合格的＝要選的）"""
    now = now or dt.datetime.now(dt.timezone.utc)
    w = WEIGHTS[weights_version]
    spec = adapters.get(task["type"])
    req = manifest.get("requirements", {})
    out = []
    for d in devices:
        did = d["device_id"]
        cap = caps.get(did)
        reasons = []
        if d["status"] != "ACTIVE":
            reasons.append("not_active")
        seen = d.get("last_seen_at")
        if d.get("connection_state") != "CONNECTED" or not seen or (now - parse_utc(seen)).total_seconds() > STALE_AFTER_SECONDS:
            reasons.append("not_connected")
        try:
            if version_tuple(d.get("protocol_version") or "0") < version_tuple(TASKS_MIN_PROTOCOL):
                reasons.append("old_protocol")
        except ValueError:
            reasons.append("old_protocol")
        if cap is None:
            reasons.append("no_capabilities")
        else:
            res = cap["resources"]
            if not cap["accepting"]:
                reasons.append("not_accepting")
            have = cap["adapters"].get(task["type"])
            if have is None:
                reasons.append("adapter_missing")
            elif spec is None or not adapters.compatible(spec, have):
                reasons.append("adapter_version")
            if adapters.needs_sandbox(task["type"]) and res.get("isolation") != "basic":
                reasons.append("no_sandbox")
            if req.get("platform", "any") != "any" and res.get("platform") != req["platform"]:
                reasons.append("platform")
            if req.get("min_memory_mb", 0) and res.get("memory_available_mb", -1) < req["min_memory_mb"]:
                reasons.append("memory")
            if req.get("min_disk_mb", 0) and res.get("disk_free_mb", -1) < req["min_disk_mb"]:
                reasons.append("disk")
            if busy.get(did, 0) >= cap["max_concurrency"]:
                reasons.append("full")
        if did in declined:
            reasons.append("declined")
        row = {"device_id": did, "name": d["name"], "eligible": not reasons, "reasons": reasons, "busy": busy.get(did, 0)}
        if not reasons:
            res, mc = cap["resources"], cap["max_concurrency"]
            outcomes = history.get(did, [])
            ok = sum(1 for o in outcomes if o == "SUCCEEDED")
            sc = {"cpu": _clip(1 - res["cpu_load"]) if "cpu_load" in res else 0.5,
                  "memory": _clip(res.get("memory_available_mb", 0) / MEMORY_REF_MB),
                  "disk": _clip(res.get("disk_free_mb", 0) / DISK_REF_MB),
                  "queue": _clip(1 - busy.get(did, 0) / mc),
                  "reliability": (ok + 1) / (len(outcomes) + 2)}
            row["scores"] = {k: round(v, 3) for k, v in sc.items()}
            row["total"] = round(100 * sum(w[k] * sc[k] for k in w), 2)
        out.append(row)
    out.sort(key=lambda r: (not r["eligible"], -r.get("total", 0), r["busy"], r["device_id"]))
    return out


class Scheduler:
    def __init__(self, service, registry_repo):
        self.svc = service                     # TaskService
        self.tr = service.tr
        self.repo = registry_repo              # V1 Repo（devices）
        self._last_sig = {}                    # task_id → 上次「沒選到」的原因摘要（一樣就不重複存紀錄）

    def _inputs(self):
        devices = self.repo.list_devices()
        caps = {d["device_id"]: self.tr.capabilities(d["device_id"]) for d in devices}
        caps = {k: v for k, v in caps.items() if v}
        busy = self.tr.count_tasks_on_devices([s.value for s in BUSY])
        history = {d["device_id"]: self.tr.recent_attempt_outcomes(d["device_id"]) for d in devices}
        return devices, caps, busy, history

    def run_once(self) -> list[tuple[str, str | None]]:
        """把 QUEUED 的任務分給合格節點；回傳 [(task_id, 選中的 device_id 或 None)]"""
        queued = self.tr.list_tasks([S.QUEUED.value])
        if not queued:
            return []
        devices, caps, busy, history = self._inputs()
        out = []
        for t in queued:
            m = json.loads(t["manifest_json"])
            declined = {a["device_id"] for a in self.tr.attempts_of(t["task_id"]) if a["status"] == "DECLINED"}
            cands = evaluate(t, m, devices, caps, busy, history, declined)
            chosen = cands[0]["device_id"] if cands and cands[0]["eligible"] else None
            sig = json.dumps([(c["device_id"], c["reasons"]) for c in cands])
            if chosen is None and self._last_sig.get(t["task_id"]) == sig:
                out.append((t["task_id"], None))
                continue
            decision = {"decision_id": str(uuid.uuid4()), "task_id": t["task_id"], "decided_at": utcnow(),
                        "weights_version": CURRENT_WEIGHTS, "candidates": cands, "chosen_device_id": chosen}
            try:
                with self.svc.db.tx() as c:
                    self.tr.insert_decision(c, decision)
                    if chosen:
                        cur = self.tr.get_task(t["task_id"], c)
                        if cur["status"] != S.QUEUED.value:
                            continue                      # 剛被取消之類 → 這次不分（交易照樣提交決策紀錄）
                        cur = self.svc._transition(c, cur, S.SCHEDULED, "scheduler",
                                                   f"選 {cands[0]['name']}（{cands[0]['total']} 分）", assigned_device_id=chosen)
            except NexusError as e:
                log.info("排程 %s 跳過：%s", t["task_id"][:8], e.code)
                continue
            if chosen:
                busy[chosen] = busy.get(chosen, 0) + 1          # 同一輪後面的任務要看到名額少了
                self._last_sig.pop(t["task_id"], None)
                self.svc.on_change(self.tr.get_task(t["task_id"]))
            else:
                self._last_sig[t["task_id"]] = sig
            out.append((t["task_id"], chosen))
        return out

"""V2 任務資料存取（只走參數化 SQL）。狀態「合不合法」由 TaskService 判斷；這裡只負責讀寫與樂觀鎖。"""
import json

from nexus.core.models import utcnow

_TASK_COLS = ("task_id", "type", "adapter_version", "manifest_json", "manifest_sha256", "idempotency_key", "status", "version",
              "priority", "attempt_count", "max_attempts", "assigned_device_id", "current_attempt_id", "result_json", "error_code",
              "error_message", "created_by", "created_at", "updated_at", "expires_at")


def _row(r):
    return dict(r) if r else None


class TaskRepo:
    def __init__(self, db):
        self.db = db

    # ---- tasks ----
    def insert_task(self, c, t: dict):
        unknown = set(t) - set(_TASK_COLS)
        if unknown:
            raise ValueError(f"unknown task columns {unknown}")
        c.execute(f"INSERT INTO tasks({','.join(t)}) VALUES ({','.join('?' * len(t))})", tuple(t.values()))

    def get_task(self, task_id: str, c=None):
        return _row((c or self.db.conn()).execute("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone())

    def task_by_idempotency_key(self, key: str, c=None):
        return _row((c or self.db.conn()).execute("SELECT * FROM tasks WHERE idempotency_key=?", (key,)).fetchone())

    def update_task(self, c, task_id: str, expected_version: int, **fields) -> bool:
        """樂觀鎖：version 不符回 False（別人先改了）"""
        unknown = set(fields) - set(_TASK_COLS)
        if unknown:
            raise ValueError(f"unknown task columns {unknown}")
        fields["version"] = expected_version + 1
        fields["updated_at"] = utcnow()
        sets = ",".join(f"{k}=?" for k in fields)
        cur = c.execute(f"UPDATE tasks SET {sets} WHERE task_id=? AND version=?", (*fields.values(), task_id, expected_version))
        return cur.rowcount == 1

    def list_tasks(self, statuses=None, limit=500, c=None):
        c = c or self.db.conn()
        if statuses:
            q = ",".join("?" * len(statuses))
            rows = c.execute(f"SELECT * FROM tasks WHERE status IN ({q}) ORDER BY priority DESC, created_at LIMIT ?",
                             (*statuses, limit))
        else:
            rows = c.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,))
        return [dict(r) for r in rows]

    def tasks_on_device(self, device_id: str, statuses, c=None):
        c = c or self.db.conn()
        q = ",".join("?" * len(statuses))
        return [dict(r) for r in c.execute(f"SELECT * FROM tasks WHERE assigned_device_id=? AND status IN ({q})",
                                           (device_id, *statuses))]

    def count_tasks_on_devices(self, statuses, c=None) -> dict:
        c = c or self.db.conn()
        q = ",".join("?" * len(statuses))
        rows = c.execute(f"SELECT assigned_device_id, COUNT(*) FROM tasks WHERE status IN ({q}) AND assigned_device_id IS NOT NULL"
                         " GROUP BY assigned_device_id", tuple(statuses))
        return {r[0]: r[1] for r in rows}

    # ---- attempts ----
    def insert_attempt(self, c, a: dict):
        c.execute(f"INSERT INTO task_attempts({','.join(a)}) VALUES ({','.join('?' * len(a))})", tuple(a.values()))

    def get_attempt(self, attempt_id: str, c=None):
        return _row((c or self.db.conn()).execute("SELECT * FROM task_attempts WHERE attempt_id=?", (attempt_id,)).fetchone())

    def attempt_by_lease(self, lease_id: str, c=None):
        return _row((c or self.db.conn()).execute("SELECT * FROM task_attempts WHERE lease_id=?", (lease_id,)).fetchone())

    def attempts_of(self, task_id: str, c=None):
        c = c or self.db.conn()
        return [dict(r) for r in c.execute("SELECT * FROM task_attempts WHERE task_id=? ORDER BY attempt_no", (task_id,))]

    def update_attempt(self, c, attempt_id: str, **fields):
        sets = ",".join(f"{k}=?" for k in fields)
        c.execute(f"UPDATE task_attempts SET {sets} WHERE attempt_id=?", (*fields.values(), attempt_id))

    def recent_attempt_outcomes(self, device_id: str, n=20, c=None) -> list[str]:
        c = c or self.db.conn()
        rows = c.execute("SELECT status FROM task_attempts WHERE device_id=? AND status IN ('SUCCEEDED','FAILED','LOST')"
                         " ORDER BY offered_at DESC LIMIT ?", (device_id, n))
        return [r[0] for r in rows]

    # ---- events ----
    def insert_event(self, c, e: dict) -> bool:
        """回 False＝同 event_id 已經收過（重複事件，去重）；同 attempt 同 seq 但不同 event_id → IntegrityError"""
        if c.execute("SELECT 1 FROM task_events WHERE event_id=?", (e["event_id"],)).fetchone():
            return False
        cur = c.execute("INSERT INTO task_events(event_id, task_id, attempt_id, device_id, seq, type, data_json, received_at)"
                        " VALUES (?,?,?,?,?,?,?,?)",
                        (e["event_id"], e["task_id"], e.get("attempt_id"), e.get("device_id"), e["seq"], e["type"],
                         json.dumps(e.get("data") or {}, ensure_ascii=False), utcnow()))
        return cur.rowcount == 1

    def events_of(self, task_id: str, c=None):
        c = c or self.db.conn()
        return [dict(r) for r in c.execute("SELECT * FROM task_events WHERE task_id=? ORDER BY received_at, seq", (task_id,))]

    # ---- schedule decisions ----
    def insert_decision(self, c, d: dict):
        c.execute("INSERT INTO schedule_decisions(decision_id, task_id, decided_at, weights_version, candidates_json, chosen_device_id)"
                  " VALUES (?,?,?,?,?,?)",
                  (d["decision_id"], d["task_id"], d["decided_at"], d["weights_version"],
                   json.dumps(d["candidates"], ensure_ascii=False), d.get("chosen_device_id")))

    def decisions_of(self, task_id: str, c=None):
        c = c or self.db.conn()
        out = []
        for r in c.execute("SELECT * FROM schedule_decisions WHERE task_id=? ORDER BY decided_at", (task_id,)):
            d = dict(r)
            d["candidates"] = json.loads(d.pop("candidates_json"))
            out.append(d)
        return out

    # ---- device capabilities ----
    def upsert_capabilities(self, device_id: str, resources: dict, adapters: dict, max_concurrency: int, accepting: bool, c=None):
        (c or self.db.conn()).execute(
            "INSERT INTO device_capabilities(device_id, resources_json, adapters_json, max_concurrency, accepting, observed_at)"
            " VALUES (?,?,?,?,?,?) ON CONFLICT(device_id) DO UPDATE SET resources_json=excluded.resources_json,"
            " adapters_json=excluded.adapters_json, max_concurrency=excluded.max_concurrency, accepting=excluded.accepting,"
            " observed_at=excluded.observed_at",
            (device_id, json.dumps(resources), json.dumps(adapters), int(max_concurrency), 1 if accepting else 0, utcnow()))

    def capabilities(self, device_id: str, c=None):
        r = (c or self.db.conn()).execute("SELECT * FROM device_capabilities WHERE device_id=?", (device_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["resources"] = json.loads(d.pop("resources_json"))
        d["adapters"] = json.loads(d.pop("adapters_json"))
        d["accepting"] = bool(d["accepting"])
        return d

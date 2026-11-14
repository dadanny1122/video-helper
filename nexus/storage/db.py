"""
SQLite 連線與升級。每個執行緒自己一條連線（ThreadingHTTPServer 每請求一執行緒）。
WAL、外鍵開、busy_timeout 5 秒。
"""
import logging
import os
import shutil
import sqlite3
import threading
import time

from nexus.core.models import utcnow
from nexus.storage.migrations import LATEST, MIGRATIONS

log = logging.getLogger("nexus.db")


class MigrationError(Exception):
    def __init__(self, msg, backup_path=None):
        super().__init__(msg)
        self.backup_path = backup_path


class Database:
    def __init__(self, path: str):
        self.path = path
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._local = threading.local()

    # ---- 連線 ----
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=5, isolation_level=None)   # autocommit；交易自己 BEGIN
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA busy_timeout=5000")
            self._local.conn = c
        return c

    def close(self):
        c = getattr(self._local, "conn", None)
        if c is not None:
            c.close()
            self._local.conn = None

    class _Tx:
        def __init__(self, c):
            self.c = c

        def __enter__(self):
            self.c.execute("BEGIN IMMEDIATE")
            return self.c

        def __exit__(self, et, ev, tb):
            if et is None:
                self.c.execute("COMMIT")
            else:
                self.c.execute("ROLLBACK")
            return False

    def tx(self):
        """with db.tx() as c: …  同一個交易裡做狀態改變＋寫稽核（規格 11.2）"""
        return self._Tx(self.conn())

    # ---- 升級 ----
    def user_version(self) -> int:
        return int(self.conn().execute("PRAGMA user_version").fetchone()[0])

    def migrate(self) -> dict:
        """
        回傳 {"from": 舊版, "to": 新版, "backup": 備份路徑或 None}
        失敗：rollback、還原備份、丟 MigrationError（原資料不動，Master 不該啟動）
        """
        c = self.conn()
        cur = self.user_version()
        if cur > LATEST:
            raise MigrationError(f"資料庫版本 {cur} 比程式支援的 {LATEST} 新，請更新程式")
        if cur == LATEST:
            return {"from": cur, "to": cur, "backup": None}
        backup = None
        if cur > 0 and os.path.isfile(self.path):
            backup = f"{self.path}.bak-v{cur}-{int(time.time())}"
            c.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            shutil.copy2(self.path, backup)
        try:
            c.execute("BEGIN IMMEDIATE")
            for ver in range(cur + 1, LATEST + 1):
                self._run_script(c, MIGRATIONS[ver])
                c.execute(f"PRAGMA user_version={ver}")
            c.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('schema_version', ?)", (str(LATEST),))
            c.execute("INSERT OR REPLACE INTO schema_meta(key, value) VALUES ('last_migration_at', ?)", (utcnow(),))
            c.execute("INSERT OR IGNORE INTO schema_meta(key, value) VALUES ('created_at', ?)", (utcnow(),))
            c.execute("COMMIT")
        except Exception as e:
            try:
                c.execute("ROLLBACK")
            except Exception:
                pass
            self.close()
            if backup:
                shutil.copy2(backup, self.path)
                for ext in ("-wal", "-shm"):
                    try:
                        os.remove(self.path + ext)
                    except FileNotFoundError:
                        pass
            raise MigrationError(f"資料庫升級失敗（{cur}→{LATEST}）：{e}", backup_path=backup) from e
        log.info("資料庫升級 %s → %s%s", cur, LATEST, f"（備份：{backup}）" if backup else "")
        return {"from": cur, "to": LATEST, "backup": backup}

    @staticmethod
    def _run_script(c, script: str):
        # executescript 會自己 COMMIT，破壞我們的交易 → 逐句執行
        for stmt in _split_sql(script):
            c.execute(stmt)


def _split_sql(script: str):
    """用分號切，但 TRIGGER 的 BEGIN…END; 要當一句"""
    out, buf, in_trigger = [], [], False
    for line in script.splitlines():
        s = line.strip()
        if not s or s.startswith("--"):
            continue
        buf.append(line)
        up = s.upper()
        if up.startswith("CREATE TRIGGER"):
            in_trigger = True
        if in_trigger:
            if up.endswith("END;"):
                out.append("\n".join(buf))
                buf, in_trigger = [], False
        elif s.endswith(";"):
            out.append("\n".join(buf))
            buf = []
    if buf:
        out.append("\n".join(buf))
    return out

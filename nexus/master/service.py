"""
MasterService：把 Database／Repo／MasterCA／ConnectionRegistry／DeviceRegistry／MasterServer 包成一個「開／關」。
下載器（video_helper_app.py）只碰這個類別：
  ・`enabled` 預設 false → 程式啟動時不開任何埠（決策 3：不得自動啟動、Worker 要使用者明確啟動）
  ・start()：建資料夾、開 DB、建／載入 CA、起 HTTPS；失敗就全部收掉並丟出錯誤給畫面顯示
  ・stop()：關 HTTPS（會把所有 Worker 連線切掉）、關 DB
  ・日誌 nexus\\master\\master.log（有遮罩，跟 Worker 一樣）
所有管理操作（核准／拒絕／暫停／恢復／撤銷）一律經 `registry`，GUI 不直接動資料庫。
"""
import logging
import logging.handlers
import os
import threading

from nexus.core.config import Config, mode_dir
from nexus.core.masking import mask
from nexus.identity.ca import MasterCA
from nexus.master.connections import ConnectionRegistry
from nexus.master.registry import DeviceRegistry
from nexus.master.server import BindPolicy, MasterServer
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.tasks.dispatch import Dispatcher
from nexus.tasks.service import TaskService

log = logging.getLogger("nexus.master")
_LOG_READY = False


class _MaskFilter(logging.Filter):
    def filter(self, record):
        record.msg = mask(record.getMessage())
        record.args = ()
        return True


def setup_master_logging(level=logging.INFO) -> str:
    global _LOG_READY
    path = os.path.join(mode_dir("master"), "master.log")
    if _LOG_READY:
        return path
    h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    h.addFilter(_MaskFilter())
    root = logging.getLogger("nexus")
    root.setLevel(level)
    root.addHandler(h)
    _LOG_READY = True
    return path


class MasterService:
    def __init__(self, config: Config | None = None, rules=None):
        self.cfg = config or Config("master")
        self.rules = rules
        self.dir = mode_dir("master")
        self.db = self.repo = self.ca = self.conns = self.registry = self.server = None
        self.tasks = self.dispatcher = None
        self.host = self.port = None
        self.error = ""
        self.lock = threading.Lock()

    # ---- 狀態 ----
    @property
    def running(self) -> bool:
        return self.server is not None and self.server.httpd is not None

    def ca_fingerprint(self) -> str:
        """CA 指紋（沒啟動過就先建 CA，讓使用者能先核對）"""
        from nexus.identity import certs
        ca = self.ca or self._make_ca()
        return certs.cert_fingerprint(ca.ca_cert)

    def _make_ca(self) -> MasterCA:
        ca = MasterCA(os.path.join(self.dir, "pki"))
        ca.ensure(BindPolicy.local_addresses(), str(self.cfg["hostname"] or ""))
        return ca

    # ---- 開／關 ----
    def start(self) -> tuple[str, int]:
        with self.lock:
            if self.running:
                return self.host, self.port
            setup_master_logging()
            self._close_ro()
            try:
                self.db = Database(os.path.join(self.dir, "nexus.db"))
                self.db.migrate()
                self.repo = Repo(self.db)
                self.ca = self._make_ca()
                self.conns = ConnectionRegistry()
                self.registry = DeviceRegistry(self.db, self.repo, self.ca, self.conns, self.cfg)
                self.tasks = TaskService(self.db, self.repo)
                self.dispatcher = Dispatcher(self.tasks, self.registry)
                self.server = MasterServer(self.registry, self.cfg, self.repo, rules=self.rules, dispatcher=self.dispatcher)
                self.host, self.port = self.server.start()
                self.dispatcher.start()
                self.error = ""
                log.info("Master 已啟動 %s:%s", self.host, self.port)
                return self.host, self.port
            except Exception as e:
                self.error = str(e)
                log.error("Master 啟動失敗：%s", e)
                self._teardown()
                raise

    def stop(self):
        with self.lock:
            if self.server:
                log.info("Master 停止")
            self._teardown()

    def _teardown(self):
        if self.dispatcher:
            try:
                self.dispatcher.stop()          # 先放掉 long-poll，再關 HTTPS
            except Exception:
                pass
        if self.server:
            try:
                self.server.stop()
            except Exception:
                pass
        if self.db:
            try:
                self.db.close()
            except Exception:
                pass
        self.server = self.registry = self.conns = self.repo = self.db = None
        self.tasks = self.dispatcher = None
        self.host = self.port = None

    # ---- 給畫面用的唯讀資料（Master 沒開也能看名單、紀錄） ----
    _ro_db = None
    _ro_repo = None

    def _close_ro(self):
        if self._ro_db:
            try:
                self._ro_db.close()
            except Exception:
                pass
        self._ro_db = self._ro_repo = None

    def _any_repo(self) -> Repo | None:
        if self.repo:
            return self.repo
        if self._ro_repo:
            return self._ro_repo
        p = os.path.join(self.dir, "nexus.db")
        if not os.path.exists(p):
            return None
        self._ro_db = Database(p)
        self._ro_db.migrate()
        self._ro_repo = Repo(self._ro_db)
        return self._ro_repo

    def task_view(self) -> TaskService | None:
        """任務資料：Master 開著就用正式的 TaskService；沒開也能看以前的任務（不能新增／取消）"""
        if self.tasks:
            return self.tasks
        repo = self._any_repo()
        return TaskService(self._ro_db, repo) if repo and self._ro_db else None

    def devices(self) -> list:
        repo = self._any_repo()
        return repo.list_devices() if repo else []

    def audit(self, limit=100) -> list:
        repo = self._any_repo()
        return repo.audit_tail(limit) if repo else []

    def close(self):
        self.stop()
        self._close_ro()

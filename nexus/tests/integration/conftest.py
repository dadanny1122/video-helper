"""
整合測試共用：在 127.0.0.1 隨機埠真的起一個 Master（真 TLS、真 CA、真 SQLite），
再用 NexusClient 當 Worker 打進去。每個測試一個乾淨的 Master。
"""
import os
import socket

import pytest

from nexus import NEXUS_VERSION
from nexus.core.config import Config
from nexus.identity import certs
from nexus.identity.ca import MasterCA
from nexus.identity.keystore import KeyStore
from nexus.master.connections import ConnectionRegistry
from nexus.master.registry import DeviceRegistry
from nexus.master.server import MasterServer
from nexus.storage.db import Database
from nexus.storage.repo import Repo
from nexus.worker.client import NexusClient


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


class Master:
    def __init__(self, root, rules=None, **cfg_overrides):
        os.environ["VH_NEXUS_ROOT"] = str(root)
        self.root = root
        self.cfg = Config("master")
        self.cfg["bind_host"] = "127.0.0.1"
        self.cfg["port"] = cfg_overrides.pop("port", free_port())
        for k, v in cfg_overrides.items():
            self.cfg[k] = v
        # 測試打得很快：心跳速率放寬（AT-023 另外用嚴格規則測）
        self.rules = rules or {"register": (5, 60), "registration_poll": (12, 60), "heartbeat": (200, 1),
                               "renew": (20, 60), "disconnect": (6, 60), "info": (30, 60)}
        self.open()

    def open(self):
        mdir = os.path.join(str(self.root), "master")
        self.db = Database(os.path.join(mdir, "nexus.db"))
        self.db.migrate()
        self.repo = Repo(self.db)
        self.ca = MasterCA(os.path.join(mdir, "pki"))
        self.ca.ensure(["127.0.0.1"])
        self.conns = ConnectionRegistry()
        self.registry = DeviceRegistry(self.db, self.repo, self.ca, self.conns, self.cfg)
        self.server = MasterServer(self.registry, self.cfg, self.repo, rules=self.rules)
        self.host, self.port = self.server.start()
        self.url = f"https://{self.host}:{self.port}"

    def restart(self):
        """模擬 Master 重啟：關服務、丟掉所有記憶體狀態、從磁碟重新載入（同一個埠）"""
        self.server.stop()
        self.db.close()
        self.open()

    def stop(self):
        self.server.stop()
        self.db.close()

    def audit_actions(self, resource_id=None):
        return [r["action"] for r in reversed(self.repo.audit_tail(500, resource_id))]


class FakeWorker:
    """最小 Worker：產金鑰、註冊、輪詢拿憑證、心跳。憑證用 KeyStore 存（跟正式 Worker 同一套）"""

    def __init__(self, master: Master, root, name="pc-test"):
        self.master, self.name = master, name
        self.dir = os.path.join(str(root), "worker-" + name)
        os.makedirs(self.dir, exist_ok=True)
        self.ks = KeyStore(self.dir)
        self.key = certs.new_key()
        self.ks.save_private_key("device", self.key)
        self.device_id = self.token = self.ca_pem = self.certfile = None
        self.seq = 0

    def anon_client(self):
        return NexusClient(self.master.url, sender_id="anonymous", ca_pem=self.ca_pem)

    def register(self, pair_code, agent_version=NEXUS_VERSION):
        csr = certs.csr_pem(certs.make_csr(self.key, "ignored"))
        r = self.anon_client().register(pair_code=pair_code, name=self.name, platform="windows", agent_version=agent_version,
                                        csr_pem=csr)
        self.device_id, self.token = r["device_id"], r["registration_token"]
        return r

    def fetch_cert(self):
        r = self.anon_client().poll_registration(self.device_id, self.token)
        if r["status"] == "ACTIVE":
            self.ca_pem = r["ca_pem"]
            self.certfile = os.path.join(self.dir, "device.crt")
            open(self.certfile, "w").write(r["certificate_pem"])
        return r

    def client(self) -> NexusClient:
        keyfile, pw = self.ks.key_pem_path_and_password("device")
        return NexusClient(self.master.url, sender_id=f"worker:{self.device_id}", ca_pem=self.ca_pem,
                           certfile=self.certfile, keyfile=keyfile, key_password=pw)

    def beat(self, c: NexusClient | None = None, **kw):
        self.seq += 1
        return (c or self.client()).heartbeat(self.device_id, self.seq, kw.pop("agent_version", NEXUS_VERSION), **kw)

    def onboard(self, reason="測試核准"):
        """一路做到 ACTIVE 並拿到憑證"""
        self.register(self.master.registry.pair.new())
        self.master.registry.approve(self.device_id, "owner", reason)
        assert self.fetch_cert()["status"] == "ACTIVE"
        return self


@pytest.fixture
def master(tmp_path):
    m = Master(tmp_path)
    yield m
    m.stop()


@pytest.fixture
def worker_factory(master, tmp_path):
    def make(name="pc-test"):
        return FakeWorker(master, tmp_path, name)
    return make

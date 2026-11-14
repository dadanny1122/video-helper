"""
Worker Agent：只做 V1 的事——註冊、等核准、心跳、憑證更新、被撤銷／過期時停下來。沒有任何「執行任務」的路徑。

狀態（給畫面看）：
  UNCONFIGURED  還沒設定 Master 位址／還沒配對
  REGISTERING   正在送註冊
  PENDING       等 Master 那邊的人核准（每 5 秒問一次）
  ACTIVE        心跳中
  RECONNECTING  連不上 Master，指數退避重試
  REVOKED       被撤銷：停止，需重新配對（新身分）
  EXPIRED       憑證過期沒更新：停止，需重新配對
  MASTER_CHANGED Master 的 CA 跟釘住的不一樣：停止並警告（不自動信任新 CA）
  INCOMPATIBLE  協定／版本不相容：停止並顯示升級指引
  STOPPED       使用者結束
"""
import logging
import random
import socket
import ssl
import threading

from nexus import NEXUS_VERSION
from nexus.core.errors import NexusError
from nexus.core.models import parse_utc
from nexus.identity import certs
from nexus.worker.client import NexusClient
from nexus.worker.credstore import CredStore

log = logging.getLogger("nexus.worker")
STOP_CODES = {"CREDENTIAL_REVOKED": "REVOKED", "DEVICE_NOT_ACTIVE": "REVOKED", "CREDENTIAL_EXPIRED": "EXPIRED",
              "PROTOCOL_INCOMPATIBLE": "INCOMPATIBLE", "AGENT_VERSION_TOO_OLD": "INCOMPATIBLE"}


class WorkerAgent:
    def __init__(self, config, store: CredStore | None = None, on_state=None, heartbeat_seconds=None, poll_seconds=5.0):
        self.cfg = config
        self.store = store or CredStore()
        self.on_state = on_state or (lambda *_: None)
        self.hb = heartbeat_seconds or int(config["heartbeat_seconds"])
        self.poll = poll_seconds
        self.state = "UNCONFIGURED"
        self.detail = ""
        self.master_fingerprint_seen = None        # TOFU：第一次看到的伺服器憑證指紋（給人核對）
        self.last_error = None
        self.seq = 0
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._client = None
        self.lock = threading.Lock()

    # ---- 對外 ----
    def _set(self, state, detail=""):
        if (state, detail) != (self.state, self.detail):
            self.state, self.detail = state, detail
            log.info("Worker 狀態：%s %s", state, detail)
            try:
                self.on_state(state, detail)
            except Exception:
                pass

    def status(self) -> dict:
        return {"state": self.state, "detail": self.detail, "device_id": self.store.state["device_id"],
                "master_url": self.store.state["master_url"], "my_fingerprint": self.store.public_fingerprint()[:16],
                "master_ca_fingerprint": (self.store.state["master_ca_fingerprint"] or "")[:16],
                "master_server_fingerprint": (self.master_fingerprint_seen or "")[:16],
                "credential_expires_at": self.store.state["credential_expires_at"], "last_error": self.last_error}

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="nexus-worker", daemon=True)
        self._thread.start()

    def stop(self, disconnect=True):
        self._stop.set()
        self._wake.set()
        if disconnect and self.state == "ACTIVE" and self.store.state["device_id"]:
            try:
                self._auth_client().disconnect(self.store.state["device_id"])
            except Exception:
                pass
        if self._client:
            self._client.close()
        if self._thread:
            self._thread.join(timeout=5)
        self._set("STOPPED")

    def pair(self, master_url: str, pair_code: str, device_name: str):
        """使用者在畫面輸入 Master 位址＋配對碼 → 送註冊（同步，馬上知道成功或錯誤）"""
        with self.lock:
            if self.store.state["status"] in ("REVOKED", "EXPIRED") or not self.store.ks.exists("device"):
                self.store.new_identity()
            key = self.store.ensure_key()
            self.cfg["master_url"], self.cfg["device_name"] = master_url, device_name
            self.cfg.save()
            self._set("REGISTERING")
            c = NexusClient(master_url, sender_id="anonymous")
            try:
                r = c.register(pair_code=pair_code, name=device_name, platform="windows", agent_version=NEXUS_VERSION,
                               csr_pem=certs.csr_pem(certs.make_csr(key, "pending")))
            except NexusError as e:
                self.last_error = e.code
                self._set("UNCONFIGURED", e.message)
                raise
            except (OSError, ssl.SSLError) as e:
                self.last_error = type(e).__name__
                self._set("UNCONFIGURED", f"連不上 Master：{e}")
                raise
            finally:
                self.master_fingerprint_seen = c.server_cert_fingerprint
                c.close()
            self.store.save(device_id=r["device_id"], master_url=master_url, registration_token=r["registration_token"],
                            master_ca_fingerprint=r["master_ca_fingerprint"], status="PENDING")
            self._set("PENDING", "等待 Master 那邊核准")
            self._wake.set()
            return r

    # ---- 主迴圈 ----
    def _run(self):
        backoff = 5
        while not self._stop.is_set():
            st = self.store.state
            try:
                if st["status"] == "PENDING" and st["device_id"]:
                    self._set("PENDING", "等待 Master 那邊核准")
                    self._poll_registration()
                    wait = self.poll
                elif self.store.has_credential():
                    self._set("ACTIVE") if self.state in ("RECONNECTING", "PENDING", "UNCONFIGURED", "REGISTERING") else None
                    self._heartbeat_once()
                    backoff = 5
                    wait = self.hb
                elif st["status"] in ("REVOKED", "EXPIRED"):
                    self._set(st["status"], "請重新配對（會用新的身分）")
                    wait = 3600
                else:
                    self._set("UNCONFIGURED", "請輸入 Master 位址與配對碼")
                    wait = 3600
            except NexusError as e:
                self.last_error = e.code
                if e.code in STOP_CODES:
                    self._stop_because(STOP_CODES[e.code], e)
                    wait = 3600
                elif e.code == "DEVICE_SUSPENDED":
                    self._set("ACTIVE", "已被 Master 暫停，等待恢復")
                    wait = self.hb
                elif e.code == "RATE_LIMITED":
                    wait = (e.retry_after or 5) + random.uniform(0, 1)
                elif e.code == "REGISTRATION_NOT_FOUND" and st["status"] == "PENDING":
                    self.store.save(status="REJECTED", registration_token=None)
                    self._set("UNCONFIGURED", "註冊被拒絕或已過期，請重新配對")
                    wait = 3600
                else:
                    self._set("RECONNECTING", f"{e.code}：{e.message}")
                    wait = backoff
                    backoff = min(int(self.cfg["reconnect_max_seconds"]), backoff * 2)
            except ssl.SSLCertVerificationError as e:
                # 釘住的 CA 驗不過＝Master 換了憑證或有人冒充 → 停下來，不自動信任
                self.last_error = "MASTER_CHANGED"
                self._stop_because("MASTER_CHANGED", e)
                wait = 3600
            except (OSError, ssl.SSLError, socket.timeout) as e:
                self.last_error = type(e).__name__
                if self._client:
                    self._client.close()
                    self._client = None
                self._set("RECONNECTING", f"{backoff} 秒後重試（{type(e).__name__}）")
                wait = backoff * random.uniform(0.8, 1.2)
                backoff = min(int(self.cfg["reconnect_max_seconds"]), backoff * 2)
            except Exception as e:                   # 不讓未知錯誤殺掉 Agent
                log.exception("Worker 迴圈錯誤")
                self.last_error = type(e).__name__
                wait = backoff
            self._wake.wait(wait)
            self._wake.clear()

    def _stop_because(self, state, err):
        if state in ("REVOKED", "EXPIRED"):
            self.store.save(status=state)
            try:                                      # 憑證作廢：刪掉本機憑證（私鑰等重新配對時換新）
                import os
                os.remove(self.store.cert_path)
            except FileNotFoundError:
                pass
        if self._client:
            self._client.close()
            self._client = None
        self._set(state, str(getattr(err, "message", err)))

    def _poll_registration(self):
        st = self.store.state
        c = NexusClient(st["master_url"], sender_id="anonymous")
        try:
            r = c.poll_registration(st["device_id"], st["registration_token"])
            self.master_fingerprint_seen = c.server_cert_fingerprint
            server_cert = c.server_cert
        finally:
            c.close()
        if r["status"] == "ACTIVE":
            # TOFU 收口：收到的 CA 必須＝註冊時 Master 說的 CA，而且剛剛這條連線的伺服器憑證要真的是它簽的
            ca = certs.load_cert(r["ca_pem"])
            if certs.cert_fingerprint(ca) != st["master_ca_fingerprint"]:
                raise ssl.SSLCertVerificationError("Master CA 跟註冊時不一致")
            try:
                from cryptography.hazmat.primitives.asymmetric import ec
                ca.public_key().verify(server_cert.signature, server_cert.tbs_certificate_bytes,
                                       ec.ECDSA(server_cert.signature_hash_algorithm))
            except Exception as e:
                raise ssl.SSLCertVerificationError("Master 伺服器憑證不是這張 CA 簽的") from e
            self.store.save(status="ACTIVE")
            self.store.save_credential(r["certificate_pem"], r["ca_pem"])
            self._set("ACTIVE", "已核准")
        elif r["status"] in ("REJECTED", "REVOKED", "EXPIRED"):
            self.store.save(status="REJECTED" if r["status"] == "REJECTED" else r["status"], registration_token=None)
            self._set("UNCONFIGURED" if r["status"] == "REJECTED" else r["status"], f"Master 回覆：{r['status']}")

    def _auth_client(self) -> NexusClient:
        if self._client is None:
            certfile, keyfile, pw = self.store.tls_material()
            self._client = NexusClient(self.store.state["master_url"], sender_id=f"worker:{self.store.state['device_id']}",
                                       ca_pem=self.store.ca_pem(), certfile=certfile, keyfile=keyfile, key_password=pw)
        return self._client

    def _heartbeat_once(self):
        c = self._auth_client()
        self.seq += 1
        r = c.heartbeat(self.store.state["device_id"], self.seq, NEXUS_VERSION, "IDLE")
        self._set("ACTIVE", "心跳正常")
        exp = self.store.state["credential_expires_at"]
        days_left = (parse_utc(exp) - parse_utc(r.get("server_time") or exp)).total_seconds() / 86400 if exp else 99
        if r.get("renew_recommended") or days_left < 14:
            self._renew(c)

    def _renew(self, c: NexusClient):
        new_key = certs.new_key()
        r = c.renew(self.store.state["device_id"], certs.csr_pem(certs.make_csr(new_key, "renew")))
        self.store.replace_credential(r["certificate_pem"], new_key)
        c.close()
        self._client = None                           # 下一次連線用新憑證
        log.info("憑證已更新，到期：%s", r["expires_at"])

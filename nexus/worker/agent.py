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

from nexus import HARDENING_MIN_PROTOCOL, NAMED_MIN_PROTOCOL, NEXUS_VERSION, version_tuple
from nexus.core.errors import NexusError
from nexus.core.models import parse_utc
from nexus.identity import certs
from nexus.worker.client import NexusClient
from nexus.worker.credstore import CredStore

log = logging.getLogger("nexus.worker")
STOP_CODES = {"CREDENTIAL_REVOKED": "REVOKED", "DEVICE_NOT_ACTIVE": "REVOKED", "CREDENTIAL_EXPIRED": "EXPIRED",
              "CREDENTIAL_MISMATCH": "REVOKED",
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
        # V2：資源回報＋執行中任務（Executor 在 PR6 接上；沒有就回空清單）
        from nexus.worker.resources import ResourceProbe
        self.probe = ResourceProbe(self.store.dir)
        from nexus.worker.executor import Executor
        self.executor = Executor(self)
        self.running_ids = self.executor.running_ids

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
        self.executor.start()

    def stop(self, disconnect=True):
        self._stop.set()
        self._wake.set()
        self.executor.stop()
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
        pair_code = "".join(pair_code.split()).replace("-", "")     # 畫面上顯示成「945 083」，照抄空格也要能用
        # 先檢查輸入：位址或配對碼格式錯，不要先把現在的身分清掉（不然 Master 那邊會留下一筆找不到主人的舊紀錄）
        c = NexusClient(master_url, sender_id="anonymous")          # 位址格式錯 → ValueError
        if not (len(pair_code) == 6 and pair_code.isdigit()):
            raise ValueError("配對碼要是 6 位數字")
        with self.lock:
            # 每次配對都是全新身分：新金鑰、清掉舊憑證（舊憑證留著會跟新身分對不上）
            if self._client:
                self._client.close()
                self._client = None
            self.store.new_identity()
            key = self.store.ensure_key()
            self.cfg["master_url"], self.cfg["device_name"] = master_url, device_name
            self.cfg.save()
            self._set("REGISTERING")
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
            with self.lock:                           # 跟 pair() 互斥：配對到一半不會拿到半新半舊的狀態
                wait = self._step(backoff)
                backoff = self._backoff
            self._wake.wait(wait)
            self._wake.clear()

    def _step(self, backoff):
        self._backoff = backoff
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
        self._backoff = backoff
        return wait

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

    def capabilities(self) -> dict:
        running = list(self.running_ids())
        mc = max(1, min(4, int(self.cfg.get("max_concurrency", 1) or 1)))
        from nexus.worker.executor import sandbox_status
        sb = sandbox_status()
        res = {**self.probe.snapshot(), "isolation": sb.get("isolation", "none"),
               "isolation_reason": "；".join(sb.get("reasons") or [])[:200]}
        res.update(self._hardening_fields())
        res.update(self._named_fields())
        return {"resources": res, "adapters": dict(self.executor.versions),
                "max_concurrency": mc, "accepting": bool(self.cfg.get("accept_tasks", True)), "running": running}

    def _hardening_fields(self) -> dict:
        """APEX PR2：強隔離偵測結果。舊 Master（協定 < 1.4）看到不認得的欄位會整份能力丟掉 → 只送給 1.4 以上"""
        from nexus.worker.executor import hardening_status
        hs = hardening_status()                       # 第一次呼叫就在背景開始偵測
        peer = getattr(self._client, "peer_protocol", None) if self._client is not None else None
        try:
            if not peer or version_tuple(peer) < version_tuple(HARDENING_MIN_PROTOCOL):
                return {}
        except ValueError:
            return {}
        if not hs:
            return {}                                 # 還在偵測：先不送，下一次心跳再送
        return {"hardening": hs.get("hardening", "none"), "hardening_reason": "；".join(hs.get("reasons") or [])[:200]}

    def _peer_at_least(self, ver: str) -> bool:
        peer = getattr(self._client, "peer_protocol", None) if self._client is not None else None
        try:
            return bool(peer) and version_tuple(peer) >= version_tuple(ver)
        except ValueError:
            return False

    def _named_fields(self) -> dict:
        """APEX（借 Dask）：自訂資源。舊 Master（協定 < 1.5）看到不認得的欄位會整份能力丟掉 → 只送給 1.5 以上；設定壞掉就不送"""
        from nexus.tasks.capabilities import named_problem
        named = self.cfg.get("named_resources") or {}
        if not named or named_problem(named) or not self._peer_at_least(NAMED_MIN_PROTOCOL):
            return {}
        return {"named": dict(named)}

    def _heartbeat_once(self):
        c = self._auth_client()
        self.seq += 1
        caps = self.capabilities()
        r = c.heartbeat(self.store.state["device_id"], self.seq, NEXUS_VERSION, "BUSY" if caps["running"] else "IDLE", **caps)
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

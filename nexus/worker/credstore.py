"""
Worker 的身分檔案（nexus\\worker\\identity\\）：
  device.key(+.pw)  私鑰（加密 PEM＋DPAPI 密碼）     device.crt  裝置憑證
  ca.crt            釘住的 Master CA                 state.json  device_id、master_url、指紋、到期日、狀態
撤銷／過期：刪憑證與釘選，私鑰也換新（下次註冊是全新身分）。
"""
import json
import os
import tempfile

from nexus.core.config import mode_dir
from nexus.core.models import utcnow
from nexus.identity import certs
from nexus.identity.keystore import KeyStore

STATE_SCHEMA = 1


class CredStore:
    def __init__(self, directory: str | None = None):
        self.dir = directory or os.path.join(mode_dir("worker"), "identity")
        os.makedirs(self.dir, exist_ok=True)
        self.ks = KeyStore(self.dir)
        self.state_path = os.path.join(self.dir, "state.json")
        self.cert_path = os.path.join(self.dir, "device.crt")
        self.ca_path = os.path.join(self.dir, "ca.crt")
        self.state = self._load_state()

    # ---- state.json ----
    def _load_state(self) -> dict:
        base = {"schema_version": STATE_SCHEMA, "device_id": None, "master_url": None, "master_ca_fingerprint": None,
                "registration_token": None, "credential_expires_at": None, "status": "UNCONFIGURED", "updated_at": None}
        try:
            raw = json.load(open(self.state_path, encoding="utf-8"))
            if isinstance(raw, dict):
                base.update({k: raw.get(k, v) for k, v in base.items()})
        except (OSError, json.JSONDecodeError):
            pass
        return base

    def save(self, **changes):
        self.state.update(changes)
        self.state["updated_at"] = utcnow()
        fd, tmp = tempfile.mkstemp(dir=self.dir, prefix=".state-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self.state, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.state_path)

    # ---- 金鑰 ----
    def ensure_key(self):
        if not self.ks.exists("device"):
            self.ks.save_private_key("device", certs.new_key())
        return self.ks.load_private_key("device")

    def new_identity(self):
        """全新身分：新金鑰、清掉憑證與釘選（撤銷／過期後重新配對用）"""
        for p in (self.cert_path, self.ca_path):
            try:
                os.remove(p)
            except FileNotFoundError:
                pass
        self.ks.delete("device")
        self.ks.save_private_key("device", certs.new_key())
        self.save(device_id=None, master_ca_fingerprint=None, registration_token=None, credential_expires_at=None, status="UNCONFIGURED")

    # ---- 憑證 ----
    def has_credential(self) -> bool:
        """有憑證檔、狀態是 ACTIVE、而且憑證裡的裝置 id＝現在的身分（舊配對留下的憑證一律不算）"""
        if not (self.state["status"] == "ACTIVE" and self.state["device_id"] and os.path.isfile(self.cert_path)
                and os.path.isfile(self.ca_path) and self.ks.exists("device")):
            return False
        try:
            with open(self.cert_path, encoding="utf-8") as f:
                return certs.device_id_of(certs.load_cert(f.read())) == self.state["device_id"]
        except Exception:
            return False

    def save_credential(self, cert_pem: str, ca_pem: str):
        cert, ca = certs.load_cert(cert_pem), certs.load_cert(ca_pem)
        certs.verify_device_cert(cert, ca)                         # 自己先驗一次：真的是這張 CA 簽的、是我們的 device_id
        if certs.device_id_of(cert) != self.state["device_id"]:
            raise ValueError("憑證裡的裝置識別跟註冊時不同")
        if self.state["master_ca_fingerprint"] and certs.cert_fingerprint(ca) != self.state["master_ca_fingerprint"]:
            raise ValueError("Master CA 指紋跟註冊時看到的不同")
        self._write(self.cert_path, cert_pem)
        self._write(self.ca_path, ca_pem)
        self.save(credential_expires_at=cert.not_valid_after_utc.isoformat(timespec="seconds").replace("+00:00", "Z"),
                  registration_token=None, status="ACTIVE", master_ca_fingerprint=certs.cert_fingerprint(ca))

    def replace_credential(self, cert_pem: str, new_key):
        """憑證更新：新金鑰＋新憑證一起換（先驗再寫）"""
        cert = certs.load_cert(cert_pem)
        certs.verify_device_cert(cert, certs.load_cert(self.ca_pem()))
        self.ks.save_private_key("device", new_key)
        self._write(self.cert_path, cert_pem)
        self.save(credential_expires_at=cert.not_valid_after_utc.isoformat(timespec="seconds").replace("+00:00", "Z"))

    def ca_pem(self) -> str | None:
        return open(self.ca_path, encoding="utf-8").read() if os.path.isfile(self.ca_path) else None

    def cert(self):
        return certs.load_cert(open(self.cert_path, encoding="utf-8").read()) if os.path.isfile(self.cert_path) else None

    def tls_material(self):
        keyfile, pw = self.ks.key_pem_path_and_password("device")
        return self.cert_path, keyfile, pw

    def public_fingerprint(self) -> str:
        return certs.public_key_fingerprint(self.ensure_key().public_key())

    @staticmethod
    def _write(path, text):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)

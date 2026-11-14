"""
Worker → Master 的 HTTPS 客戶端（標準庫 http.client ＋ ssl）
  ・第一次接觸（還沒拿到 Master CA）：TOFU 模式，記下看到的伺服器憑證指紋給使用者核對
  ・之後：釘住 Master CA（只信這一張），帶裝置憑證做 mTLS
  ・連線 keep-alive；Master 撤銷時會把連線關掉，下一次請求會失敗 → 呼叫端處理
"""
import http.client
import json
import ssl
import tempfile
import urllib.parse

from nexus import PROTOCOL_VERSION
from nexus.core.errors import NexusError
from nexus.identity import certs
from nexus.protocol.messages import envelope

API = "/api/v1"


class NexusClient:
    def __init__(self, master_url: str, *, sender_id: str, ca_pem: str | None = None,
                 certfile: str | None = None, keyfile: str | None = None, key_password: bytes | None = None,
                 timeout: float = 30.0, connect_timeout: float = 5.0):
        u = urllib.parse.urlparse(master_url)
        if u.scheme != "https" or not u.hostname:
            raise ValueError("master_url 必須是 https://主機:埠")
        self.host, self.port = u.hostname, u.port or 47331
        self.sender_id = sender_id
        self.timeout, self.connect_timeout = timeout, connect_timeout
        self.ca_pem = ca_pem
        self.certfile, self.keyfile, self.key_password = certfile, keyfile, key_password
        self.server_cert_fingerprint = None      # TOFU 模式下看到的伺服器憑證指紋
        self._conn = None
        self._ca_tmp = None

    # ---- TLS ----
    def _context(self) -> ssl.SSLContext:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        if self.ca_pem:
            ctx.check_hostname = True
            ctx.verify_mode = ssl.CERT_REQUIRED
            ctx.load_verify_locations(cadata=self.ca_pem)
        else:                                      # 第一次接觸：還不知道 CA，只能先看指紋
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        if self.certfile and self.keyfile:
            ctx.load_cert_chain(self.certfile, self.keyfile, password=self.key_password)
        return ctx

    def connection(self) -> http.client.HTTPSConnection:
        if self._conn is None:
            self._conn = http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout, context=self._context())
            self._conn.connect()
            der = self._conn.sock.getpeercert(binary_form=True)
            if der:
                from cryptography import x509
                self.server_cert_fingerprint = certs.cert_fingerprint(x509.load_der_x509_certificate(der))
        return self._conn

    def close(self):
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    # ---- 請求 ----
    def _request(self, method: str, path: str, body: dict | None = None, headers: dict | None = None) -> dict:
        hdrs = {"Content-Type": "application/json", "User-Agent": "VideoHelperNexusWorker"}
        if headers:
            hdrs.update(headers)
        data = None
        if method == "POST":
            data = json.dumps(envelope(self.sender_id, **(body or {})), ensure_ascii=False).encode()
        else:
            env = envelope(self.sender_id)
            env.pop("nonce", None)
            hdrs["X-VH-Envelope"] = json.dumps(env)
        conn = self.connection()
        try:
            conn.request(method, API + path, body=data, headers=hdrs)
            resp = conn.getresponse()
            raw = resp.read()
        except (http.client.HTTPException, OSError):
            self.close()
            raise
        if resp.getheader("Connection", "").lower() == "close":
            self.close()
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else {}
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise NexusError("INTERNAL_ERROR", message=f"Master 回了看不懂的內容（HTTP {resp.status}）") from e
        if resp.status >= 400 or "error" in payload:
            err = payload.get("error") or {}
            ra = resp.getheader("Retry-After")
            raise NexusError(err.get("code", "INTERNAL_ERROR"), err.get("message"), err.get("details"),
                             retry_after=int(ra) if ra and ra.isdigit() else None)
        if payload.get("protocol_version", "").split(".")[0] != PROTOCOL_VERSION.split(".")[0]:
            raise NexusError("PROTOCOL_INCOMPATIBLE")
        return payload

    def register(self, *, pair_code, name, platform, agent_version, csr_pem) -> dict:
        return self._request("POST", "/devices/register", dict(pair_code=pair_code, name=name, platform=platform,
                                                              agent_version=agent_version, csr_pem=csr_pem))

    def poll_registration(self, device_id: str, token: str) -> dict:
        return self._request("GET", f"/devices/{device_id}/registration", headers={"Authorization": f"Registration {token}"})

    def heartbeat(self, device_id: str, sequence: int, agent_version: str, status: str = "IDLE") -> dict:
        return self._request("POST", f"/devices/{device_id}/heartbeat", dict(sequence=sequence, agent_version=agent_version, status=status))

    def renew(self, device_id: str, csr_pem: str) -> dict:
        return self._request("POST", f"/devices/{device_id}/renew", dict(csr_pem=csr_pem))

    def disconnect(self, device_id: str) -> dict:
        return self._request("POST", f"/devices/{device_id}/disconnect", {})

    def info(self) -> dict:
        return self._request("GET", "/system/info")

    @staticmethod
    def ca_pem_to_file(ca_pem: str) -> str:
        f = tempfile.NamedTemporaryFile("w", suffix=".crt", delete=False)
        f.write(ca_pem)
        f.close()
        return f.name

"""
Master 的 HTTPS 服務（標準庫 http.server ＋ ssl）
  ・TLS 1.2+；CERT_OPTIONAL：沒帶憑證的只能走註冊兩個端點，其他一律要裝置憑證（且每個請求再查 DB）
  ・綁定位址有規則（私有網段／CGNAT／本機；0.0.0.0 要 bind_all_confirmed）
  ・payload 上限、速率限制、nonce 防重放、時鐘偏移、keep-alive 60 秒
  ・管理操作（核准／撤銷…）不在這裡：只有本機 GUI 直接呼叫 registry
"""
import ipaddress
import json
import logging
import re
import socket
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from nexus import MIN_AGENT_VERSION, MIN_PEER_PROTOCOL, NEXUS_VERSION, PROTOCOL_VERSION
from nexus.core.errors import NexusError
from nexus.core.masking import mask
from nexus.core.models import AuditEvent, parse_utc
from nexus.identity import certs
from nexus.master.connections import ConnectionRegistry
from nexus.master.ratelimit import DEFAULT_RULES, RateLimiter
from nexus.protocol.messages import check_envelope, reply

log = logging.getLogger("nexus.master")
API = "/api/v1"
MASTER_ID = "master"

# 各端點的內容 schema（外框另外驗）
S_REGISTER = {"pair_code": (str, True, 8), "name": (str, True, 64), "platform": (str, True, 32),
              "agent_version": (str, True, 32), "csr_pem": (str, True, 4096)}
S_HEARTBEAT = {"sequence": (int, True, 2**53), "agent_version": (str, True, 32), "status": (str, True, 16)}
S_RENEW = {"csr_pem": (str, True, 4096)}
S_EMPTY = {}


class BindPolicy:
    """哪些位址可以綁（決策 1：可設定、不暴露公網、LAN 不是信任邊界）"""
    CGNAT = ipaddress.ip_network("100.64.0.0/10")

    @classmethod
    def classify(cls, host: str) -> str:
        if host in ("0.0.0.0", "::"):
            return "all"
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return "invalid"
        if ip.is_loopback:
            return "loopback"
        if ip in cls.CGNAT:
            return "vpn"
        if ip.is_private:
            return "lan"
        return "public"

    @classmethod
    def allowed(cls, host: str, bind_all_confirmed: bool) -> tuple[bool, str]:
        k = cls.classify(host)
        if k in ("loopback", "lan", "vpn"):
            return True, k
        if k == "all":
            return bool(bind_all_confirmed), k
        return False, k

    @staticmethod
    def local_addresses() -> list:
        """本機網卡位址（給 GUI 下拉；不含公網位址）"""
        out = {"127.0.0.1"}
        try:
            for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
                out.add(info[4][0])
        except OSError:
            pass
        try:                                              # 多數情況最準的「預設路由那張網卡」
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("10.255.255.255", 1))
            out.add(s.getsockname()[0])
            s.close()
        except OSError:
            pass
        return sorted(a for a in out if BindPolicy.classify(a) in ("loopback", "lan", "vpn"))


class MasterServer:
    def __init__(self, registry, config, repo, rules=None):
        self.registry, self.cfg, self.repo = registry, config, repo
        self.conns: ConnectionRegistry = registry.conns
        self.limiter = RateLimiter(rules or DEFAULT_RULES)
        self.httpd = None
        self.thread = None
        self.sweeper = None
        self._stop = threading.Event()

    # ---- 生命週期 ----
    def start(self) -> tuple[str, int]:
        host, port = str(self.cfg["bind_host"]), int(self.cfg["port"])
        ok, kind = BindPolicy.allowed(host, bool(self.cfg["bind_all_confirmed"]))
        if not ok:
            raise RuntimeError(f"不允許綁定 {host}（{kind}）：只能綁本機網卡的區域網路／VPN 位址；0.0.0.0 需要在設定明確確認")
        chk = self.registry.selfcheck()
        if not chk["ok"]:
            raise RuntimeError("啟動自檢失敗：" + "；".join(chk["problems"]))
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        certfile, keyfile, pw = self.registry.ca.server_tls_material()
        ctx.load_cert_chain(certfile, keyfile, password=pw)
        ctx.load_verify_locations(cafile=self.registry.ca.ca_cert_path)
        ctx.verify_mode = ssl.CERT_OPTIONAL           # 註冊時沒憑證；其他端點在請求層強制
        server = self
        max_payload = int(self.cfg["max_payload_bytes"])

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 60                              # keep-alive 閒置 60 秒就斷
            server_version = "VideoHelperNexus"
            sys_version = ""

            def log_message(self, fmt, *args):       # 不用預設的 stderr 日誌（會印完整路徑與 UA）
                log.debug("%s %s", self.client_address[0], mask(fmt % args))

            def setup(self):
                super().setup()
                self.peer_fp, self.peer_device = None, None
                try:
                    der = self.connection.getpeercert(binary_form=True)
                except Exception:
                    der = None
                if der:
                    from cryptography import x509
                    cert = x509.load_der_x509_certificate(der)
                    self.peer_fp = certs.cert_fingerprint(cert)
                    self.peer_device = certs.device_id_of(cert)
                    if self.peer_device:
                        server.conns.add(self.peer_device, self.connection)

            def finish(self):
                if self.peer_device:
                    server.conns.remove(self.peer_device, self.connection)
                try:
                    super().finish()
                except Exception:
                    pass

            def do_GET(self):
                self._handle("GET")

            def do_POST(self):
                self._handle("POST")

            def _send(self, status: int, payload: dict, extra_headers: dict | None = None):
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra_headers or {}).items():
                    self.send_header(k, str(v))
                self.end_headers()
                self.wfile.write(body)

            def _fail(self, e: NexusError, correlation_id=None, close=False):
                headers = {"Retry-After": e.retry_after} if e.retry_after else {}
                if close:
                    headers["Connection"] = "close"
                    self.close_connection = True
                self._send(e.http_status, e.to_response(correlation_id), headers)

            def _handle(self, method):
                corr = None
                try:
                    corr, payload = server._dispatch(self, method)
                    self._send(200, payload)
                except NexusError as e:
                    self._fail(e, corr, close=e.code in ("CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED", "CREDENTIAL_MISMATCH",
                                                        "DEVICE_NOT_ACTIVE", "DEVICE_SUSPENDED"))
                except Exception as e:                # 絕不把堆疊丟給對方
                    log.exception("內部錯誤 %s", mask(str(e)))
                    self._fail(NexusError("INTERNAL_ERROR"), corr)

        self.httpd = ThreadingHTTPServer((host, port), Handler)
        self.httpd.daemon_threads = True
        self.httpd.request_queue_size = int(self.cfg["max_connections"])
        self.httpd.socket = ctx.wrap_socket(self.httpd.socket, server_side=True)
        self.max_payload = max_payload
        self.thread = threading.Thread(target=self.httpd.serve_forever, name="nexus-master", daemon=True)
        self.thread.start()
        self._stop.clear()
        self.sweeper = threading.Thread(target=self._sweep_loop, name="nexus-sweeper", daemon=True)
        self.sweeper.start()
        self.repo.audit(AuditEvent("master.started", "system", "master", MASTER_ID, "ok", "", "", {"bind": f"{host}:{port}", "kind": kind}))
        log.info("Nexus Master 啟動：%s:%d（%s）", host, port, kind)
        return host, port

    def stop(self):
        self._stop.set()
        if self.httpd:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception:
                pass
            self.httpd = None
            try:
                self.repo.audit(AuditEvent("master.stopped", "system", "master", MASTER_ID, "ok"))
            except Exception:
                pass
            log.info("Nexus Master 關閉")

    def _sweep_loop(self):
        while not self._stop.wait(10):
            try:
                for did, st in self.registry.sweep():
                    if st in ("OFFLINE", "EXPIRED"):
                        self.conns.close_all(did)
            except Exception as e:
                log.warning("清查失敗：%s", mask(str(e)))

    # ---- 路由 ----
    _routes = [
        ("POST", re.compile(rf"^{API}/devices/register$"), "register", "register", False),
        ("GET", re.compile(rf"^{API}/devices/([0-9a-f-]{{36}})/registration$"), "registration", "registration_poll", False),
        ("POST", re.compile(rf"^{API}/devices/([0-9a-f-]{{36}})/heartbeat$"), "heartbeat", "heartbeat", True),
        ("POST", re.compile(rf"^{API}/devices/([0-9a-f-]{{36}})/renew$"), "renew", "renew", True),
        ("POST", re.compile(rf"^{API}/devices/([0-9a-f-]{{36}})/disconnect$"), "disconnect", "disconnect", True),
        ("GET", re.compile(rf"^{API}/system/info$"), "info", "info", True),
    ]

    def _dispatch(self, h, method):
        source = h.client_address[0]
        path = h.path.split("?")[0]
        name = kind = needs_auth = path_id = None
        known = False
        for m, pat, n, k, auth in self._routes:
            mm = pat.match(path)
            if not mm:
                continue
            known = True
            if m == method:
                name, kind, needs_auth = n, k, auth
                path_id = mm.group(1) if mm.groups() else None
                break
        if name is None:
            raise NexusError("METHOD_NOT_ALLOWED" if known else "NOT_FOUND")
        wait = self.limiter.check(source, kind)
        if wait:
            self.repo.audit(AuditEvent("ratelimit", "system", "source", source, "denied", kind))
            raise NexusError("RATE_LIMITED", retry_after=wait)
        # 內容
        length = int(h.headers.get("Content-Length") or 0)
        if length > self.max_payload:
            raise NexusError("PAYLOAD_TOO_LARGE", details={"max_bytes": self.max_payload})
        raw = h.rfile.read(length) if length else b""
        if method == "GET":
            # GET 的外框放在標頭（X-VH-Envelope＝JSON）；輪詢用 token 放 Authorization: Registration <token>
            try:
                data = json.loads(h.headers.get("X-VH-Envelope") or "{}")
            except json.JSONDecodeError as e:
                raise NexusError("SCHEMA_INVALID", details={"where": "envelope"}) from e
        else:
            try:
                data = json.loads(raw.decode("utf-8")) if raw else {}
            except (json.JSONDecodeError, UnicodeDecodeError) as e:
                raise NexusError("SCHEMA_INVALID", details={"where": "body"}) from e
        schema = {"register": S_REGISTER, "registration": S_EMPTY, "heartbeat": S_HEARTBEAT, "renew": S_RENEW,
                  "disconnect": S_EMPTY, "info": S_EMPTY}[name]
        body = check_envelope(data, schema, require_nonce=(method == "POST"))
        corr = data["correlation_id"]
        sender = data["sender_id"]
        # 重放／時鐘（POST 才有 nonce）
        if method == "POST":
            self._replay_check(sender, data["nonce"], data["sent_at"])
        # 身分
        dev = None
        if needs_auth:
            if not h.peer_fp:
                self.repo.audit(AuditEvent("credential.rejected", "anonymous", "device", path_id or "-", "denied", "no client cert", corr,
                                           {"source_ip": source}))
                raise NexusError("CREDENTIAL_MISMATCH", message="這個端點需要裝置憑證")
            dev = self.registry.authenticate(h.peer_fp, path_id, corr)
            if sender != f"worker:{dev['device_id']}":
                raise NexusError("CREDENTIAL_MISMATCH", message="sender_id 與憑證不符")
        # 端點
        if name == "register":
            out = self.registry.register(pair_code=body["pair_code"], name=body["name"], platform=body["platform"],
                                         agent_version=body["agent_version"], protocol_version=data["protocol_version"],
                                         csr_pem=body["csr_pem"], source_ip=source, correlation_id=corr)
        elif name == "registration":
            auth = h.headers.get("Authorization") or ""
            if not auth.startswith("Registration "):
                raise NexusError("REGISTRATION_NOT_FOUND")
            out = self.registry.registration_status(path_id, auth[len("Registration "):].strip())
        elif name == "heartbeat":
            out = self.registry.heartbeat(dev, int(body["sequence"]), body["agent_version"], data["protocol_version"], body["status"])
        elif name == "renew":
            out = self.registry.renew(dev, body["csr_pem"], h.peer_fp)
        elif name == "disconnect":
            self.registry.disconnect(dev)
            out = {"status": "ok"}
            h.close_connection = True
        else:
            out = {"nexus_version": NEXUS_VERSION, "protocol_version": PROTOCOL_VERSION,
                   "min_peer_protocol": MIN_PEER_PROTOCOL, "min_agent_version": MIN_AGENT_VERSION, "server_time": time.time()}
        return corr, reply(MASTER_ID, corr, **out)

    def _replay_check(self, sender: str, nonce: str, sent_at: str):
        try:
            ts = parse_utc(sent_at).timestamp()
        except Exception as e:
            raise NexusError("SCHEMA_INVALID", details={"field": "sent_at"}) from e
        skew = int(self.cfg["clock_skew_seconds"])
        if abs(time.time() - ts) > skew:
            raise NexusError("CLOCK_SKEW", details={"max_skew_seconds": skew})
        if self.repo.nonce_seen(sender, nonce, int(self.cfg["nonce_ttl_seconds"])):
            raise NexusError("REPLAY_DETECTED")

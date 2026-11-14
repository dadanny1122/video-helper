"""
Master 的私有 CA：第一次啟用時建立，之後載入。
檔案（nexus\\master\\pki\\）：ca.crt、ca.key(+ca.pw)、server.crt、server.key(+server.pw)
CA 私鑰只在簽發時解密到記憶體；伺服器憑證到期前 30 天自動換發。
"""
import logging
import os

from nexus.identity import certs
from nexus.identity.keystore import KeyStore

log = logging.getLogger("nexus.ca")


class MasterCA:
    def __init__(self, pki_dir: str):
        self.dir = pki_dir
        os.makedirs(pki_dir, exist_ok=True)
        self.ks = KeyStore(pki_dir)
        self.ca_cert = None
        self.server_cert = None

    @property
    def ca_cert_path(self):
        return os.path.join(self.dir, "ca.crt")

    @property
    def server_cert_path(self):
        return os.path.join(self.dir, "server.crt")

    def ensure(self, bind_ips: list, hostname: str = "") -> dict:
        """沒有就建，有就載入並檢查；回傳 {"created": bool, "server_renewed": bool, "ca_fingerprint": …}"""
        created = False
        if not (self.ks.exists("ca") and os.path.isfile(self.ca_cert_path)):
            key, cert = certs.make_ca()
            self.ks.save_private_key("ca", key)
            open(self.ca_cert_path, "w", encoding="utf-8").write(certs.pem(cert))
            created = True
            log.info("建立 Master CA：%s", certs.cert_fingerprint(cert)[:16])
        self.ca_cert = certs.load_cert(open(self.ca_cert_path, encoding="utf-8").read())
        if certs.days_left(self.ca_cert) <= 0:
            raise RuntimeError("Master CA 憑證已過期；請備份 nexus\\master 後重建（所有 Worker 需重新配對）")
        renewed = self._ensure_server(bind_ips, hostname)
        return {"created": created, "server_renewed": renewed, "ca_fingerprint": certs.cert_fingerprint(self.ca_cert)}

    def _ensure_server(self, bind_ips, hostname) -> bool:
        need = True
        if self.ks.exists("server") and os.path.isfile(self.server_cert_path):
            cur = certs.load_cert(open(self.server_cert_path, encoding="utf-8").read())
            try:
                from cryptography import x509
                san = cur.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
                have_ips = {str(ip) for ip in san.get_values_for_type(x509.IPAddress)}
                have_dns = set(san.get_values_for_type(x509.DNSName))
            except Exception:
                have_ips, have_dns = set(), set()
            covers = set(bind_ips) <= have_ips and (not hostname or hostname in have_dns)
            need = certs.days_left(cur) < 30 or not covers or cur.issuer != self.ca_cert.subject
            if not need:
                self.server_cert = cur
        if need:
            ca_key = self.ks.load_private_key("ca")
            key, cert = certs.make_server_cert(ca_key, self.ca_cert, bind_ips, hostname)
            self.ks.save_private_key("server", key)
            open(self.server_cert_path, "w", encoding="utf-8").write(certs.pem(cert))
            self.server_cert = cert
            log.info("簽發 Master 伺服器憑證（%s）", ", ".join(bind_ips))
        return need

    def issue_device_cert(self, csr_pem: str, device_id: str, days: int = 90):
        csr = certs.load_csr(csr_pem)
        ca_key = self.ks.load_private_key("ca")
        return certs.sign_device_cert(ca_key, self.ca_cert, csr, device_id, days=days)

    def ca_pem(self) -> str:
        return certs.pem(self.ca_cert)

    def server_tls_material(self):
        """給 ssl.SSLContext.load_cert_chain 用"""
        keyfile, password = self.ks.key_pem_path_and_password("server")
        return self.server_cert_path, keyfile, password

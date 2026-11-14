"""憑證的建立、簽發、驗證、指紋（用 cryptography；TLS 連線本身交給標準庫 ssl）"""
import datetime as dt
import hashlib

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

DEVICE_URN = "urn:vh:device:"


def new_key():
    return ec.generate_private_key(ec.SECP256R1())


def public_key_fingerprint(public_key) -> str:
    """裝置「身分指紋」：公鑰 DER 的 SHA-256（註冊時就有，不用等憑證）"""
    der = public_key.public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
    return hashlib.sha256(der).hexdigest()


def cert_fingerprint(cert) -> str:
    return cert.fingerprint(hashes.SHA256()).hex()


def pem(cert) -> str:
    return cert.public_bytes(serialization.Encoding.PEM).decode()


def load_cert(pem_text: str):
    return x509.load_pem_x509_certificate(pem_text.encode())


def _now():
    return dt.datetime.now(dt.timezone.utc)


def make_ca(name: str = "Video Helper Nexus Master CA", years: int = 10):
    import secrets
    key = new_key()
    # CN 加 8 碼隨機字：兩個不同 Master 的 CA 名字不會一樣（光比名字也分得出來）
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, f"{name} {secrets.token_hex(4)}")])
    cert = (x509.CertificateBuilder()
            .subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=365 * years))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                         key_encipherment=False, data_encipherment=False, key_agreement=False,
                                         encipher_only=False, decipher_only=False), critical=True)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
            .sign(key, hashes.SHA256()))
    return key, cert


def make_server_cert(ca_key, ca_cert, ips: list, hostname: str = "", days: int = 365):
    import ipaddress
    key = new_key()
    sans = [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips]
    if hostname:
        sans.append(x509.DNSName(hostname))
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Video Helper Nexus Master")]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5))
            .not_valid_after(_now() + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return key, cert


def make_csr(key, device_id: str):
    return (x509.CertificateSigningRequestBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
            .sign(key, hashes.SHA256()))


def csr_pem(csr) -> str:
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def load_csr(pem_text: str):
    csr = x509.load_pem_x509_csr(pem_text.encode())
    if not csr.is_signature_valid:
        raise ValueError("CSR 簽章無效")
    if not isinstance(csr.public_key(), ec.EllipticCurvePublicKey):
        raise ValueError("只接受 EC P-256 金鑰")
    return csr


def sign_device_cert(ca_key, ca_cert, csr, device_id: str, days: int = 90, not_before=None):
    """裝置憑證：CN＝device_id、SAN URI urn:vh:device:<id>、clientAuth。Master 決定 device_id，不信 CSR 寫什麼"""
    start = (not_before or _now()) - dt.timedelta(minutes=5)
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, device_id)]))
            .issuer_name(ca_cert.subject).public_key(csr.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(start)
            .not_valid_after(start + dt.timedelta(minutes=5) + dt.timedelta(days=days))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.UniformResourceIdentifier(DEVICE_URN + device_id)]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    return cert


def device_id_of(cert) -> str | None:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        for uri in san.get_values_for_type(x509.UniformResourceIdentifier):
            if uri.startswith(DEVICE_URN):
                return uri[len(DEVICE_URN):]
    except x509.ExtensionNotFound:
        pass
    return None


def verify_device_cert(cert, ca_cert, now=None) -> str:
    """回傳 device_id；任何不對就丟 ValueError（簽章、發行者、效期、用途）"""
    now = now or _now()
    if cert.issuer != ca_cert.subject:
        raise ValueError("發行者不是我們的 CA")
    try:
        ca_cert.public_key().verify(cert.signature, cert.tbs_certificate_bytes, ec.ECDSA(cert.signature_hash_algorithm))
    except Exception as e:                       # InvalidSignature 等 → 一律當「不是我們簽的」
        raise ValueError("簽章不是我們的 CA 簽的") from e
    if not (cert.not_valid_before_utc <= now <= cert.not_valid_after_utc):
        raise ValueError("憑證不在有效期內")
    try:
        eku = cert.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
    except x509.ExtensionNotFound as e:
        raise ValueError("沒有用途欄位") from e
    if ExtendedKeyUsageOID.CLIENT_AUTH not in eku:
        raise ValueError("不是裝置（clientAuth）憑證")
    did = device_id_of(cert)
    if not did:
        raise ValueError("沒有裝置識別")
    return did


def days_left(cert, now=None) -> float:
    now = now or _now()
    return (cert.not_valid_after_utc - now).total_seconds() / 86400

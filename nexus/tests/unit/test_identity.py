import datetime as dt
import os
import ssl

import pytest

from nexus.identity import certs
from nexus.identity.ca import MasterCA
from nexus.identity.keystore import KeyStore


def test_keystore_never_writes_plaintext_key(tmp_path):
    ks = KeyStore(str(tmp_path))
    key = certs.new_key()
    ks.save_private_key("ca", key)
    raw = open(tmp_path / "ca.key", "rb").read()
    assert b"ENCRYPTED" in raw and b"BEGIN PRIVATE KEY" not in raw
    assert not [f for f in os.listdir(tmp_path) if f.endswith(".tmp")]
    loaded = ks.load_private_key("ca")
    assert certs.public_key_fingerprint(loaded.public_key()) == certs.public_key_fingerprint(key.public_key())
    keyfile, pw = ks.key_pem_path_and_password("ca")
    assert os.path.isfile(keyfile) and len(pw) == 32


def test_issue_verify_device_cert_and_reject_wrong_ca(tmp_path):
    ca = MasterCA(str(tmp_path / "pki"))
    info = ca.ensure(["127.0.0.1"])
    assert info["created"] and len(info["ca_fingerprint"]) == 64
    wkey = certs.new_key()
    csr = certs.csr_pem(certs.make_csr(wkey, "whatever-the-worker-claims"))
    cert = ca.issue_device_cert(csr, "dev-123", days=90)
    assert certs.verify_device_cert(cert, ca.ca_cert) == "dev-123"        # device_id 由 Master 決定，不信 CSR
    assert 89 < certs.days_left(cert) <= 90.01
    other = MasterCA(str(tmp_path / "pki2"))
    other.ensure(["127.0.0.1"])
    with pytest.raises(ValueError):
        certs.verify_device_cert(cert, other.ca_cert)
    # 伺服器憑證不是裝置憑證
    with pytest.raises(ValueError):
        certs.verify_device_cert(ca.server_cert, ca.ca_cert)


def test_expired_cert_rejected(tmp_path):
    ca = MasterCA(str(tmp_path / "pki"))
    ca.ensure(["127.0.0.1"])
    csr = certs.csr_pem(certs.make_csr(certs.new_key(), "x"))
    old = certs.sign_device_cert(ca.ks.load_private_key("ca"), ca.ca_cert, certs.load_csr(csr), "d",
                                 days=1, not_before=dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=3))
    with pytest.raises(ValueError, match="有效期"):
        certs.verify_device_cert(old, ca.ca_cert)
    assert certs.days_left(old) < 0


def test_csr_must_be_ec_and_signed():
    from cryptography.hazmat.primitives.asymmetric import rsa
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    with pytest.raises(ValueError, match="EC"):
        certs.load_csr(certs.csr_pem(certs.make_csr(rsa_key, "x")))
    with pytest.raises(ValueError):
        certs.load_csr("-----BEGIN CERTIFICATE REQUEST-----\nAAAA\n-----END CERTIFICATE REQUEST-----")


def test_ca_reload_and_server_cert_reissue_when_ip_changes(tmp_path):
    ca = MasterCA(str(tmp_path / "pki"))
    a = ca.ensure(["127.0.0.1"])
    ca2 = MasterCA(str(tmp_path / "pki"))
    b = ca2.ensure(["127.0.0.1"])
    assert not b["created"] and not b["server_renewed"] and a["ca_fingerprint"] == b["ca_fingerprint"]
    c = ca2.ensure(["127.0.0.1", "192.168.1.10"])
    assert c["server_renewed"]


def test_ssl_context_can_load_our_material(tmp_path):
    """標準庫 ssl 真的吃得下加密 PEM＋密碼；mTLS 設定正確（CERT_REQUIRED）"""
    ca = MasterCA(str(tmp_path / "pki"))
    ca.ensure(["127.0.0.1"])
    certfile, keyfile, pw = ca.server_tls_material()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_cert_chain(certfile, keyfile, password=pw)
    ctx.load_verify_locations(cafile=ca.ca_cert_path)
    ctx.verify_mode = ssl.CERT_REQUIRED
    assert ctx.verify_mode == ssl.CERT_REQUIRED

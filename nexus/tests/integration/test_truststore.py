"""下載器會 truststore.inject_into_ssl()（把 ssl.SSLContext 換掉）；Nexus 的 TLS 要照樣能跑。
Windows 打包時真的炸過：truststore 的伺服器端 context 收到沒帶憑證的註冊連線會 AttributeError。"""
import ssl

import pytest

from nexus.tests.integration.conftest import FakeWorker, Master

truststore = pytest.importorskip("truststore")


def test_works_with_truststore_injected(tmp_path):
    import truststore._api as api
    truststore.inject_into_ssl()
    try:
        assert ssl.SSLContext is api.SSLContext            # 真的被換掉了
        m = Master(tmp_path)                               # 在換掉的狀態下啟動 Master
        try:
            w = FakeWorker(m, tmp_path, "ts").onboard()
            assert w.beat()["status"] == "ACTIVE"
            assert type(w.client()._context()) is api._original_SSLContext   # Worker 用原版
            assert ssl.SSLContext is api.SSLContext        # 用完有放回去
        finally:
            m.stop()
    finally:
        truststore.extract_from_ssl()

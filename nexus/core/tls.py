"""
拿「真正的」標準庫 SSLContext。
下載器為了讓 yt-dlp 用 Windows 的憑證庫，啟動時呼叫 truststore.inject_into_ssl()，
它會把 ssl.SSLContext 整個換成 truststore 的版本——那個版本只拿作業系統的憑證庫驗證，
我們的私有 CA 會驗不過；伺服器端收到「沒帶憑證」的連線（註冊階段）還會直接炸（Windows 打包時真的發生過）。
而且標準庫 SSLContext 的屬性 setter 寫的是 `super(SSLContext, SSLContext)`，名字被換掉後直接拿原版類別
去設 minimum_version 也會無限遞迴。所以：建立＋設定 Nexus 的 TLS context 時，先把 truststore 暫時拿掉，
設定完再放回去（有鎖；只有啟動 Master／Worker 建連線時各做一次）。
"""
import ssl
import threading

_lock = threading.Lock()


def stdlib_ssl_context(protocol, configure) -> ssl.SSLContext:
    """configure(ctx) 裡面做完所有設定；回傳設定好的原版 SSLContext"""
    with _lock:
        ts = None
        try:
            import truststore
            import truststore._api as _api
            if ssl.SSLContext is _api.SSLContext:
                ts = truststore
                truststore.extract_from_ssl()
        except Exception:
            ts = None
        try:
            ctx = ssl.SSLContext(protocol)
            configure(ctx)
            return ctx
        finally:
            if ts is not None:
                ts.inject_into_ssl()

"""
沙箱子程序裡的 video.download（V3 PR5）。只在 `--nexus-run` 的子程序裡被呼叫，不在節點主程序跑。

網路（ADAPTER 層允許清單，ADR-6 第三層）：
  ・install_net_guard() 換掉這個子程序的 socket.getaddrinfo／socket.connect：
    每一個連線（影片頁、API、影片伺服器、轉址後的網址）都要
      網域在 sites.json（頁面或影片伺服器）＋埠 443＋解析出來全是公開 IP，
    connect 只准連「剛剛檢查過的 IP」。所以 yt-dlp 不管走哪條路都繞不過。
  ・yt-dlp 一律用自己的下載器（native），**不讓 ffmpeg 連網**（ffmpeg 只在本機合併影音）；
    格式的協定不在 ALLOWED_PROTOCOLS（例如 rtmp、f4m）就拒絕
  ・不用系統代理、不讀 Cookie、不寫快取
誠實說明：這是 Python 程式自己擋，不是 OS 防火牆。
"""
import os
import shutil
import socket
import sys
import threading
import time
import urllib.parse

from nexus.net import guard, sites

ALLOWED_PROTOCOLS = {"https", "http", "m3u8", "m3u8_native", "http_dash_segments", "http_dash_segments_generator"}
NET_MARK = "[VH-NET]"
QUALITY_FORMATS = {
    "best": "bv*+ba/b",
    "1080": "bv*[height<=1080]+ba/b[height<=1080]/b",
    "720": "bv*[height<=720]+ba/b[height<=720]/b",
    "480": "bv*[height<=480]+ba/b[height<=480]/b",
}


class GuardBlocked(OSError):
    """網路檢查不通過（是 OSError，所以 urllib／yt-dlp 會當成連線失敗往上報）"""


# ---------- socket 層網路檢查 ----------
_installed = {}
# 這些套件自己（用 C）連網，會繞過 Python 的 socket → 在沙箱子程序裡一律當作沒裝（yt-dlp 會改用內建 urllib）
BYPASS_LIBS = ("curl_cffi", "requests", "urllib3", "websockets", "httpx", "pycurl", "aiohttp")


def block_bypass_libs():
    for m in BYPASS_LIBS:
        if m not in sys.modules or sys.modules[m] is None:
            sys.modules[m] = None                      # import 會丟 ImportError


def install_net_guard(*, resolve_override=None) -> set:
    """換掉 socket.getaddrinfo／socket.socket.connect；回傳「檢查過的 IP」集合（測試看用）"""
    if _installed:
        return _installed["approved"]
    block_bypass_libs()
    orig_gai, orig_connect = socket.getaddrinfo, socket.socket.connect
    approved: set[str] = set()
    lock = threading.Lock()

    def guarded_getaddrinfo(host, port, *args, **kw):
        h = host.decode("idna") if isinstance(host, bytes) else str(host or "")
        try:
            if resolve_override is not None:
                ips = guard.check_host(h, port, kind="any", resolve=resolve_override)
                res = [(socket.AF_INET6 if ":" in ip else socket.AF_INET, socket.SOCK_STREAM, 6, "",
                        (ip, 443) if ":" not in ip else (ip, 443, 0, 0)) for ip in ips]
            else:
                res = orig_gai(h, port, *args, **kw)      # 只解析一次：檢查的就是要連的（擋 DNS rebinding）
                ips = sorted({r[4][0] for r in res})
                guard.check_host(h, port, kind="any", resolve=lambda *_: ips)
        except guard.NetBlocked as e:
            raise GuardBlocked(f"{NET_MARK} {e.reason}") from None
        with lock:
            approved.update(ip.split("%", 1)[0] for ip in ips)
        return res

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            ip = str(address[0]).split("%", 1)[0]
            with lock:
                ok = ip in approved
            if not ok:
                raise GuardBlocked(f"{NET_MARK} 沒經過檢查的位址 {ip}，不連")
        return orig_connect(self, address)

    socket.getaddrinfo = guarded_getaddrinfo
    socket.socket.connect = guarded_connect
    _installed.update(approved=approved, orig_gai=orig_gai, orig_connect=orig_connect)
    return approved


def uninstall_net_guard():
    """只給測試用（子程序跑完就結束，不需要還原）"""
    if _installed:
        socket.getaddrinfo = _installed["orig_gai"]
        socket.socket.connect = _installed["orig_connect"]
        _installed.clear()


# ---------- 錯誤分類（跟電腦版 classify_error 同精神：重試有沒有用） ----------
def classify(msg: str) -> tuple[str, bool]:
    m = (msg or "").lower()
    if NET_MARK.lower() in m:
        return "NET_BLOCKED", False
    if "429" in m or "too many requests" in m:
        return "RATE_LIMITED", True
    if any(k in m for k in ("no space left", "not enough space", "errno 28", "disk full")):
        return "DISK_FULL", False
    if any(k in m for k in ("404", "not found", "has been removed", "video unavailable", "is unavailable",
                            "private video", "has been deleted", "no longer available")):
        return "NOT_FOUND", False
    if "unsupported url" in m:
        return "INVALID_INPUT", False
    if any(k in m for k in ("timed out", "timeout", "connection reset", "connection aborted", "temporarily",
                            "remote end closed", "502", "503", "504", "500")):
        return "NETWORK", True
    return "ADAPTER_ERROR", False


def ffmpeg_location() -> str | None:
    """打包後：子程序自己解壓出來的迷你 ffmpeg；原始碼：PATH 上的 ffmpeg（runner 會把它的資料夾放進 PATH）"""
    if getattr(sys, "frozen", False):
        d = os.path.join(getattr(sys, "_MEIPASS", ""), "ffmpeg")
        return d if os.path.isdir(d) else None
    return shutil.which("ffmpeg")


def build_opts(ws: dict, quality: str, max_bytes: int, hook) -> dict:
    ff = ffmpeg_location()
    fmt = QUALITY_FORMATS.get(quality, QUALITY_FORMATS["best"])
    if not ff:                                         # 沒有 ffmpeg 就不能合併影音 → 只選單一檔
        fmt = fmt.replace("bv*+ba/", "").replace("bv*[height<=", "b[height<=")
    return {
        "quiet": True, "no_warnings": True, "noprogress": True, "noplaylist": True,
        "paths": {"home": ws["output"], "temp": ws["tmp"]},
        "outtmpl": {"default": "%(title).100B [%(id)s].%(ext)s"},
        "windowsfilenames": True, "overwrites": False,
        "format": fmt, "merge_output_format": "mp4",
        **({"ffmpeg_location": ff} if ff else {}),
        "external_downloader": {"default": "native", "m3u8": "native", "dash": "native"},   # ffmpeg 不連網
        "hls_prefer_native": True,
        "proxy": "",                                   # 不用系統代理
        "cookiefile": None, "cachedir": False,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 5,
        "max_filesize": max_bytes,
        "progress_hooks": [hook],
    }


def run_video_download(params: dict, ws: dict, *, emit, error_cls, ydl_cls=None, resolve_override=None) -> dict:
    url = str(params.get("url", ""))
    quality = str(params.get("quality", "best"))
    max_bytes = int(params.get("_max_output_bytes", 8 << 30))
    try:
        host = guard.static_check(url, kind="page")
    except guard.NetBlocked as e:
        raise error_cls("NET_BLOCKED", e.reason) from None
    install_net_guard(resolve_override=resolve_override)

    last = [0.0]

    def hook(d):
        if d.get("status") != "downloading":
            return
        now = time.monotonic()
        if now - last[0] < 0.5:
            return
        last[0] = now
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        if total and total > max_bytes:
            raise error_cls("TOO_BIG", f"檔案太大（約 {total // (1 << 20)} MB）")
        emit("progress", progress=round(min(done / total, 0.99), 3) if total else 0.0,
             downloaded_bytes=int(done), total_bytes=int(total), speed=int(d.get("speed") or 0), eta=d.get("eta"))

    if ydl_cls is None:
        import yt_dlp
        ydl_cls = yt_dlp.YoutubeDL
    try:
        with ydl_cls(build_opts(ws, quality, max_bytes, hook)) as ydl:
            info = ydl.extract_info(url, download=False)
            if not isinstance(info, dict):
                raise error_cls("NOT_FOUND", "找不到影片資訊")
            if info.get("_type") in ("playlist", "multi_video"):
                raise error_cls("INVALID_INPUT", "這是播放清單，一次只能下載一部")
            chosen = info.get("requested_formats") or [info]
            protos = {str(f.get("protocol") or "https") for f in chosen}
            bad = sorted(p for p in protos if p not in ALLOWED_PROTOCOLS)
            if bad:
                raise error_cls("PROTOCOL_UNSUPPORTED", f"這部影片用的傳輸方式（{'、'.join(bad)}）節點不支援")
            for f in chosen:                            # 每個要下載的網址也先檢查一次（真正連線時 socket 還會再檢查）
                furl = f.get("url") or ""
                if furl.startswith("https://") or furl.startswith("http://"):
                    try:
                        guard.static_check(furl.replace("http://", "https://", 1), kind="any")
                    except guard.NetBlocked as e:
                        raise error_cls("NET_BLOCKED", f"影片伺服器不在清單：{e.reason}") from None
            size = sum(int(f.get("filesize") or f.get("filesize_approx") or 0) for f in chosen)
            if size > max_bytes:
                raise error_cls("TOO_BIG", f"檔案太大（約 {size // (1 << 20)} MB）")
            ydl.process_ie_result(info, download=True)
    except error_cls:
        raise
    except Exception as e:                              # yt-dlp 的 DownloadError 等：照訊息分類
        code, retry = classify(f"{type(e).__name__}: {e}")
        msg = str(e).replace(NET_MARK, "").strip()
        raise error_cls(code, _short(msg), retry) from None
    emit("progress", progress=1.0)
    return {"title": str(info.get("title") or "")[:200], "id": str(info.get("id") or "")[:64],
            "site": sites.site_name(host) or host, "height": info.get("height"),
            "url_host": urllib.parse.urlsplit(url).hostname}


def _short(msg: str, n: int = 300) -> str:
    msg = " ".join(str(msg).split())
    for marker in ("ERROR: ", "[download] "):
        msg = msg.replace(marker, "")
    return msg[:n]

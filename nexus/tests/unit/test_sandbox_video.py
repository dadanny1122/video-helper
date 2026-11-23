"""V3 PR5：沙箱裡的 video.download——socket 層網路檢查、錯誤分類、yt-dlp 設定（用假的 YoutubeDL，開發環境連不到真網站）"""
import os
import socket

import pytest

from nexus.sandbox import video
from nexus.sandbox.child import ChildError
from nexus.sandbox.workspace import Workspace


@pytest.fixture(autouse=True)
def _restore_socket():
    import sys
    saved = {m: sys.modules.get(m, "absent") for m in video.BYPASS_LIBS}
    yield
    video.uninstall_net_guard()
    for m, v in saved.items():
        if v == "absent":
            sys.modules.pop(m, None)
        else:
            sys.modules[m] = v


def test_bypass_network_libs_blocked():
    video.install_net_guard(resolve_override=public)
    for m in ("curl_cffi", "websockets", "requests"):
        with pytest.raises(ImportError):
            __import__(m)


def public(*_):
    return ["93.184.216.34"]


# ---------- socket 層 ----------
def test_guard_allows_listed_host_on_443():
    video.install_net_guard(resolve_override=public)
    res = socket.getaddrinfo("www.pornhub.com", 443)
    assert res[0][4][0] == "93.184.216.34"


@pytest.mark.parametrize("host,port", [("example.com", 443), ("www.pornhub.com", 80), ("www.pornhub.com", 8443),
                                       ("127.0.0.1", 443), ("localhost", 443), ("jable.tv", 443)])
def test_guard_blocks_unlisted_or_bad_port(host, port):
    video.install_net_guard(resolve_override=public)
    with pytest.raises(video.GuardBlocked) as e:
        socket.getaddrinfo(host, port)
    assert video.NET_MARK in str(e.value)


def test_guard_blocks_dns_rebinding_to_lan():
    video.install_net_guard(resolve_override=lambda *_: ["192.168.1.10"])
    with pytest.raises(video.GuardBlocked):
        socket.getaddrinfo("cdn.phncdn.com", 443)


def test_connect_only_to_checked_ips():
    video.install_net_guard(resolve_override=public)
    s = socket.socket()
    try:
        with pytest.raises(video.GuardBlocked):                 # 沒經過 getaddrinfo 檢查的 IP，直接連也不行
            s.connect(("127.0.0.1", 9))
    finally:
        s.close()


def test_guard_error_is_oserror_so_ytdlp_reports_it():
    assert issubclass(video.GuardBlocked, OSError)


# ---------- 錯誤分類 ----------
@pytest.mark.parametrize("msg,code,retry", [
    (f"{video.NET_MARK} x 不在清單", "NET_BLOCKED", False),
    ("HTTP Error 429: Too Many Requests", "RATE_LIMITED", True),
    ("HTTP Error 404: Not Found", "NOT_FOUND", False),
    ("This video has been removed", "NOT_FOUND", False),
    ("[Errno 28] No space left on device", "DISK_FULL", False),
    ("The read operation timed out", "NETWORK", True),
    ("HTTP Error 503: Service Unavailable", "NETWORK", True),
    ("Unsupported URL: https://x", "INVALID_INPUT", False),
    ("something weird", "ADAPTER_ERROR", False),
    ("[PornHub] 658229bd52f10: Unable to download webpage: HTTP Error 410: Gone", "SITE_REFUSED", False),
    ("HTTP Error 403: Forbidden", "SITE_REFUSED", False),
])
def test_classify(msg, code, retry):
    assert video.classify(msg) == (code, retry)


# ---------- yt-dlp 設定 ----------
def test_opts_never_let_ffmpeg_download_and_no_proxy_or_cookies(tmp_path):
    ws = Workspace(str(tmp_path))
    o = video.build_opts({"output": ws.output, "tmp": ws.tmp}, "720", 1000, lambda d: None)
    assert o["external_downloader"] == {"default": "native", "m3u8": "native", "dash": "native"}
    assert o["proxy"] == "" and o["cookiefile"] is None and o["cachedir"] is False
    assert o["paths"]["home"] == ws.output and o["max_filesize"] == 1000 and o["noplaylist"]
    assert "height<=720" in o["format"]


# ---------- 整個流程（假的 YoutubeDL） ----------
class FakeYDL:
    info = {}
    fail = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def extract_info(self, url, download=False):
        if self.fail:
            raise Exception(self.fail)
        return dict(self.info)

    def process_ie_result(self, info, download=True):
        for h in self.opts["progress_hooks"]:
            h({"status": "downloading", "downloaded_bytes": 50, "total_bytes": 100, "speed": 10, "eta": 5})
        with open(os.path.join(self.opts["paths"]["home"], f"{info['title']} [{info['id']}].mp4"), "wb") as f:
            f.write(b"v" * 100)


def go(tmp_path, params, info=None, fail=None):
    ws = Workspace(str(tmp_path))
    events = []
    FakeYDL.info = info or {"id": "abc", "title": "Clip", "height": 720,
                            "requested_formats": [{"protocol": "m3u8_native", "url": "https://hls.phncdn.com/a.m3u8"},
                                                  {"protocol": "https", "url": "https://ev.phncdn.com/a.m4a"}]}
    FakeYDL.fail = fail
    r = video.run_video_download({"url": "https://www.pornhub.com/view_video.php?viewkey=abc", **params},
                                 {"output": ws.output, "tmp": ws.tmp}, emit=lambda e, **kw: events.append((e, kw)),
                                 error_cls=ChildError, ydl_cls=FakeYDL, resolve_override=public)
    return r, events, ws


def test_success_writes_into_output_and_reports(tmp_path):
    r, events, ws = go(tmp_path, {})
    assert r["site"] == "Pornhub" and r["id"] == "abc" and r["height"] == 720
    assert os.listdir(ws.output) == ["Clip [abc].mp4"]
    assert any(e == "progress" and kw["progress"] == 0.5 for e, kw in events)
    assert events[-1] == ("progress", {"progress": 1.0})


def test_bad_page_url_refused_before_ytdlp(tmp_path):
    ws = Workspace(str(tmp_path))
    with pytest.raises(ChildError) as e:
        video.run_video_download({"url": "https://jable.tv/videos/x/"}, {"output": ws.output, "tmp": ws.tmp},
                                 emit=lambda *a, **k: None, error_cls=ChildError, ydl_cls=FakeYDL)
    assert e.value.code == "NET_BLOCKED"


def test_media_server_not_in_list_refused(tmp_path):
    info = {"id": "x", "title": "t", "requested_formats": [{"protocol": "https", "url": "https://evil-cdn.example/v.mp4"}]}
    with pytest.raises(ChildError) as e:
        go(tmp_path, {}, info=info)
    assert e.value.code == "NET_BLOCKED"


@pytest.mark.parametrize("proto", ["rtmp", "f4m", "ism", "websocket_frag"])
def test_unsupported_protocols_refused(tmp_path, proto):
    info = {"id": "x", "title": "t", "requested_formats": [{"protocol": proto, "url": "https://ev.phncdn.com/v"}]}
    with pytest.raises(ChildError) as e:
        go(tmp_path, {}, info=info)
    assert e.value.code == "PROTOCOL_UNSUPPORTED"


def test_playlist_refused(tmp_path):
    with pytest.raises(ChildError) as e:
        go(tmp_path, {}, info={"_type": "playlist", "entries": []})
    assert e.value.code == "INVALID_INPUT"


def test_too_big_refused_before_download(tmp_path):
    info = {"id": "x", "title": "t", "requested_formats": [{"protocol": "https", "url": "https://ev.phncdn.com/v",
                                                             "filesize": 10 << 30}]}
    with pytest.raises(ChildError) as e:
        go(tmp_path, {"_max_output_bytes": 8 << 30}, info=info)
    assert e.value.code == "TOO_BIG"


def test_ytdlp_errors_are_classified(tmp_path):
    with pytest.raises(ChildError) as e:
        go(tmp_path, {}, fail="ERROR: [PornHub] abc: HTTP Error 429: Too Many Requests")
    assert e.value.code == "RATE_LIMITED" and e.value.retryable
    with pytest.raises(ChildError) as e:
        go(tmp_path, {}, fail=f"<urlopen error {video.NET_MARK} 192.168.1.1 是內網>")
    assert e.value.code == "NET_BLOCKED" and video.NET_MARK not in e.value.message


def test_real_ytdlp_cannot_reach_lan_through_listed_domain(tmp_path):
    """真的 yt-dlp：網域在清單、但解析到內網 → 我們的 socket 檢查擋下，錯誤分類成 NET_BLOCKED（不是一般網路錯誤）"""
    pytest.importorskip("yt_dlp")
    ws = Workspace(str(tmp_path))
    with pytest.raises(ChildError) as e:
        video.run_video_download({"url": "https://www.pornhub.com/view_video.php?viewkey=ph0000000000"},
                                 {"output": ws.output, "tmp": ws.tmp}, emit=lambda *a, **k: None,
                                 error_cls=ChildError, resolve_override=lambda *_: ["127.0.0.1"])
    assert e.value.code == "NET_BLOCKED", (e.value.code, e.value.message)
    assert os.listdir(ws.output) == []


def test_worker_without_sandbox_does_not_offer_video_download(monkeypatch):
    from nexus.worker import executor as ex

    class A:
        cfg = {}
    monkeypatch.setattr(ex, "_SANDBOX", {"isolation": "none", "reasons": ["不是 Windows"]})
    e = ex.Executor(A())
    assert "video.download" not in e.versions and "test.echo" in e.versions


def test_site_refused_not_retried_and_explained(tmp_path):
    from nexus.tasks.states import NON_RETRYABLE_ERRORS, RETRYABLE_ERRORS
    with pytest.raises(ChildError) as e:
        go(tmp_path, {}, fail="ERROR: [PornHub] x: Unable to download webpage: HTTP Error 410: Gone")
    assert e.value.code == "SITE_REFUSED" and not e.value.retryable and "送到電腦版" in e.value.message
    assert "SITE_REFUSED" in NON_RETRYABLE_ERRORS and "SITE_REFUSED" not in RETRYABLE_ERRORS

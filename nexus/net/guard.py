"""
網址／IP 檢查（V3 沙箱第三層：網路）。節點下載每一個請求（含轉址、影片伺服器）發出去之前都要過這裡。

擋：
  ・不是 https、埠不是 443
  ・網域不在 sites.json（頁面或影片伺服器網域）
  ・網址裡有帳號密碼（https://user@host）
  ・解析出來的 IP 不是公開網路位址：私有網段、127.x、169.254.x（雲端 metadata）、100.64/10、IPv6 本機／ULA、
    IPv4-mapped 的私有位址……（擋 DNS rebinding：網域看起來對，解析結果卻指到內網）
  ・轉址超過 MAX_REDIRECTS 次

誠實說明：這是程式自己檢查，**不是 OS 防火牆**；同一台電腦上別的程式不受這裡限制。
"""
import ipaddress
import socket
import urllib.parse

from nexus.net import sites

MAX_REDIRECTS = 5


class NetBlocked(Exception):
    """網路檢查不通過；message 是給使用者看的中文原因"""

    def __init__(self, reason: str, code: str = "NET_BLOCKED"):
        super().__init__(reason)
        self.reason, self.code = reason, code


def _default_resolve(host: str, port: int) -> list[str]:
    return sorted({ai[4][0] for ai in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)})


def ip_is_public(ip: str) -> bool:
    """只有真正的公開網路位址才算（is_global）；IPv4-mapped IPv6 先還原成 IPv4 再判斷"""
    try:
        a = ipaddress.ip_address(ip.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(a, ipaddress.IPv6Address) and a.ipv4_mapped:
        a = a.ipv4_mapped
    return a.is_global and not a.is_multicast


def allowed_domains(kind: str) -> tuple:
    return sites.page_hosts() if kind == "page" else sites.page_hosts() + sites.media_hosts()


def check_host(host: str, port, *, kind: str = "any", resolve=_default_resolve) -> list[str]:
    """只看主機＋埠（socket 層用）：網域在清單、埠 443、解析出來全是公開 IP"""
    host = (host or "").lower().rstrip(".")
    if port not in (None, 443, "443", "https"):
        raise NetBlocked(f"只允許 443 埠（{host}:{port}）", "NET_PORT")
    try:
        ipaddress.ip_address(host.strip("[]"))
        raise NetBlocked("不能直接連 IP 位址，要用網站網域", "NET_IP_LITERAL")
    except ValueError:
        pass
    if not sites.host_matches(host, allowed_domains(kind)):
        raise NetBlocked(f"{host} 不在支援下載的網站清單裡", "NET_HOST")
    try:
        ips = resolve(host, 443)
    except OSError as e:
        raise NetBlocked(f"找不到 {host}（{type(e).__name__}）", "NET_DNS") from None
    if not ips:
        raise NetBlocked(f"找不到 {host}", "NET_DNS")
    bad = [ip for ip in ips if not ip_is_public(ip)]
    if bad:
        raise NetBlocked(f"{host} 解析到內網或保留位址（{bad[0]}），為了安全不連", "NET_PRIVATE_IP")
    return ips


def static_check(url: str, *, kind: str = "page") -> str:
    """不查 DNS 的檢查（Master 建任務時用）：通過回傳小寫網域，不通過丟 NetBlocked"""
    try:
        u = urllib.parse.urlsplit(str(url))
        host, port = (u.hostname or "").lower().rstrip("."), u.port
    except ValueError:
        raise NetBlocked("網址格式不對", "NET_BAD_URL") from None
    if u.scheme != "https":
        raise NetBlocked("只允許 https 網址", "NET_SCHEME")
    if u.username or u.password:
        raise NetBlocked("網址裡不能有帳號密碼", "NET_USERINFO")
    if port not in (None, 443):
        raise NetBlocked(f"只允許 443 埠（這個網址是 {port}）", "NET_PORT")
    if not host:
        raise NetBlocked("網址沒有網域", "NET_BAD_URL")
    try:                                                    # 直接寫 IP 的網址一律不准（清單裡只有網域）
        ipaddress.ip_address(host)
        raise NetBlocked("不能直接用 IP 位址，要用網站網域", "NET_IP_LITERAL")
    except ValueError:
        pass
    if not sites.host_matches(host, allowed_domains(kind)):
        raise NetBlocked(f"{host} 不在支援下載的網站清單裡", "NET_HOST")
    return host


def check_url(url: str, *, kind: str = "page", resolve=_default_resolve) -> list[str]:
    """
    檢查一個網址能不能連。kind="page" 只准影片頁網域；kind="any" 准頁面＋影片伺服器網域（下載過程用）。
    通過 → 回傳解析出來的 IP；不通過 → NetBlocked。
    """
    host = static_check(url, kind=kind)
    return check_host(host, 443, kind=kind, resolve=resolve)


class RedirectGuard:
    """一個任務用一個：每次轉址呼叫 follow()，超過次數或轉到清單外就擋"""

    def __init__(self, max_redirects: int = MAX_REDIRECTS, resolve=_default_resolve):
        self.count, self.max, self.resolve = 0, max_redirects, resolve

    def follow(self, base_url: str, location: str) -> str:
        self.count += 1
        if self.count > self.max:
            raise NetBlocked(f"轉址超過 {self.max} 次", "NET_REDIRECTS")
        nxt = urllib.parse.urljoin(base_url, location)
        check_url(nxt, kind="any", resolve=self.resolve)
        return nxt

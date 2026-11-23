"""V3 PR1：共用網站清單（sites.json）＋網址／IP 檢查（nexus.net.guard）"""
import json
import os

import pytest

from nexus.net import guard, sites
from nexus.net.guard import NetBlocked, RedirectGuard, check_url, ip_is_public

# v1.30.1 以前寫死在電腦版的兩份清單：抽成 sites.json 後內容和順序都要一模一樣（行為不變）
OLD_PAGE = ("pornhub.com", "xvideos.com", "xnxx.com", "youporn.com", "redtube.com",
            "xhamster.com", "xhamster.desi", "xhamster2.com", "xhamster3.com", "xhamster.one",
            "tube8.com", "youjizz.com", "spankbang.com")
OLD_MEDIA = ("phncdn.com", "pornhub.com", "xvideos-cdn.com", "xvideos.com", "xnxx-cdn.com", "xnxx.com",
             "ypncdn.com", "youporn.com", "rdtcdn.com", "redtube.com", "xhcdn.com", "xhamster.com",
             "t8cdn.com", "tube8.com", "youjizz.com", "sb-cd.com", "spankbang.com")
ADBLOCK_ONLY = ("missav", "jable", "hanime1", "eporner", "hqporner", "beeg", "fc2", "onlyfans")


def pub(*_):
    return ["93.184.216.34"]


def test_sites_json_same_as_old_desktop_lists():
    assert sites.page_hosts() == OLD_PAGE
    assert sites.media_hosts() == OLD_MEDIA


def test_sites_json_has_no_adblock_only_or_paid_sites():
    raw = open(sites.SITES_PATH, encoding="utf-8").read().lower()
    for bad in ADBLOCK_ONLY:
        assert bad not in raw, f"{bad} 只擋廣告／付費，不能出現在下載清單"
    data = json.loads(raw)
    assert len(data["sites"]) == 9


def test_host_matches_subdomain_but_not_lookalike():
    assert sites.host_matches("www.pornhub.com", sites.page_hosts())
    assert sites.host_matches("PORNHUB.com.", sites.page_hosts())
    assert not sites.host_matches("evilpornhub.com", sites.page_hosts())
    assert not sites.host_matches("pornhub.com.evil.net", sites.page_hosts())
    assert sites.site_name("ev-h.phncdn.com") == "Pornhub"
    assert sites.site_name("example.com") is None


def test_check_url_allows_supported_https_page():
    assert check_url("https://www.xvideos.com/video.abc/x", resolve=pub) == ["93.184.216.34"]
    assert check_url("https://www.xvideos.com:443/video.abc/x", resolve=pub)


@pytest.mark.parametrize("url,code", [
    ("http://www.xvideos.com/video.abc", "NET_SCHEME"),
    ("ftp://www.xvideos.com/x", "NET_SCHEME"),
    ("https://www.xvideos.com:8443/x", "NET_PORT"),
    ("https://user:pw@www.xvideos.com/x", "NET_USERINFO"),
    ("https://jable.tv/videos/x", "NET_HOST"),
    ("https://evilxvideos.com/x", "NET_HOST"),
    ("https://93.184.216.34/x", "NET_IP_LITERAL"),
    ("https://[::1]/x", "NET_IP_LITERAL"),
    ("https:///x", "NET_BAD_URL"),
])
def test_check_url_rejects(url, code):
    with pytest.raises(NetBlocked) as e:
        check_url(url, resolve=pub)
    assert e.value.code == code and e.value.reason


def test_media_host_only_allowed_with_kind_any():
    with pytest.raises(NetBlocked):
        check_url("https://ev-h.phncdn.com/v.mp4", resolve=pub)
    assert check_url("https://ev-h.phncdn.com/v.mp4", kind="any", resolve=pub)


@pytest.mark.parametrize("ip", ["127.0.0.1", "10.1.2.3", "192.168.68.86", "172.16.0.1", "169.254.169.254",
                                "100.64.0.1", "0.0.0.0", "224.0.0.1", "::1", "fe80::1", "fd00::1",
                                "::ffff:192.168.1.1", "::ffff:127.0.0.1", "not-an-ip"])
def test_private_and_reserved_ips_are_not_public(ip):
    assert not ip_is_public(ip)


def test_public_ips():
    assert ip_is_public("93.184.216.34") and ip_is_public("2606:2800:220:1:248:1893:25c8:1946")


def test_dns_rebinding_to_private_ip_is_blocked():
    with pytest.raises(NetBlocked) as e:          # 網域對、解析結果混一個內網位址也不行
        check_url("https://www.pornhub.com/view_video.php?viewkey=1", resolve=lambda h, p: ["93.184.216.34", "192.168.1.1"])
    assert e.value.code == "NET_PRIVATE_IP"


def test_dns_failure_is_blocked():
    def boom(h, p):
        raise OSError("nope")
    with pytest.raises(NetBlocked) as e:
        check_url("https://www.pornhub.com/x", resolve=boom)
    assert e.value.code == "NET_DNS"
    with pytest.raises(NetBlocked):
        check_url("https://www.pornhub.com/x", resolve=lambda h, p: [])


def test_redirects_checked_and_limited():
    g = RedirectGuard(max_redirects=2, resolve=pub)
    assert g.follow("https://www.pornhub.com/a", "/b") == "https://www.pornhub.com/b"
    assert g.follow("https://www.pornhub.com/b", "https://ev.phncdn.com/v.mp4") == "https://ev.phncdn.com/v.mp4"
    with pytest.raises(NetBlocked) as e:
        g.follow("https://ev.phncdn.com/v.mp4", "/c")
    assert e.value.code == "NET_REDIRECTS"
    g2 = RedirectGuard(resolve=pub)
    for loc in ("http://www.pornhub.com/x", "https://127.0.0.1/admin", "https://example.com/"):
        with pytest.raises(NetBlocked):
            g2.follow("https://www.pornhub.com/a", loc)


def test_sites_json_packaged_path_exists():
    assert os.path.isfile(sites.SITES_PATH)
    assert guard.MAX_REDIRECTS == 5

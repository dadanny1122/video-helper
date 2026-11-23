"""
V3.1 E4：真網站的「交給節點」測試（手動觸發：Actions → node-site-test → Run workflow）。

1. 無頭瀏覽器開影片頁（跟瀏覽器腳本一樣讀 Pornhub 的 flashvars → 影片真正的位址）
2. 在 Windows 沙箱裡用 video.download（帶 media_url）下載**最低畫質**
3. 只印大小和 SHA-256，**立刻刪掉、不上傳任何東西**
GitHub 的機器在美國；Pornhub 在美國部分地區會擋（410／年齡驗證）。拿不到影片位址時印「測試不成立」，不算通過也不算失敗。
"""
import hashlib
import os
import shutil
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))   # 讓 nexus 套件 import 得到

from playwright.sync_api import sync_playwright  # noqa: E402

from nexus.sandbox.runner import SandboxRunner  # noqa: E402

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"


def notice(t, m):
    print(f"::notice title={t}::{m}", flush=True)


def inconclusive(why):
    print(f"::warning title=測試不成立::{why}", flush=True)
    sys.exit(0)


def find_pornhub():
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(user_agent=UA, locale="en-US")
        r = pg.goto("https://www.pornhub.com/video?o=mr", wait_until="domcontentloaded", timeout=60000)
        if not r or r.status >= 400:
            inconclusive(f"Pornhub 列表頁回 {r.status if r else '沒回應'}（可能是 GitHub 機器所在地區被擋）")
        hrefs = pg.eval_on_selector_all("a[href*='view_video.php?viewkey=']", "els => els.map(e => e.href)")
        if not hrefs:
            inconclusive("列表頁找不到影片連結（可能要年齡驗證或地區限制）")
        page = hrefs[0].split("#")[0]
        r = pg.goto(page, wait_until="domcontentloaded", timeout=60000)
        if not r or r.status >= 400:
            inconclusive(f"影片頁回 {r.status if r else '沒回應'}")
        info = pg.evaluate("""() => {
            const k = Object.keys(window).find(k => k.startsWith('flashvars_'));
            if (!k) return null;
            const fv = window[k];
            const hls = (fv.mediaDefinitions || []).filter(d => d.format === 'hls' && d.videoUrl)
              .map(d => ({h: +(Array.isArray(d.quality) ? d.quality[0] : d.quality), url: d.videoUrl}))
              .filter(d => d.h).sort((a, b) => a.h - b.h);
            return {title: fv.video_title || document.title, hls};
        }""")
        if info and info["hls"]:
            # 診斷：同一個位址，瀏覽器自己抓得到嗎？（分辨「網站只給瀏覽器」還是「地區／IP 限制」）
            st = pg.evaluate("u => fetch(u, {credentials: 'include'}).then(r => r.status).catch(e => 'ERR ' + e)",
                             info["hls"][0]["url"])
            notice("診斷：瀏覽器自己抓影片位址", f"HTTP {st}")
        b.close()
    if not info or not info["hls"]:
        inconclusive("影片頁讀不到 flashvars（網站可能改版，或要年齡驗證）")
    low = info["hls"][0]
    notice("瀏覽器拿到影片位址", f"{low['h']}p HLS，網域 {low['url'].split('/')[2]}")
    return page, low["url"], info["title"]


def main():
    if sys.platform != "win32":
        print("::error::要在 Windows 跑（沙箱只支援 Windows）")
        return 1
    page, media, title = find_pornhub()
    import urllib.error
    import urllib.request
    for label, hdrs in (("Python＋瀏覽器標頭", {"User-Agent": UA, "Referer": page}), ("Python 沒有標頭", {})):
        try:
            with urllib.request.urlopen(urllib.request.Request(media, headers=hdrs), timeout=30) as r:
                st = r.status
        except urllib.error.HTTPError as e:
            st = e.code
        except Exception as e:
            st = type(e).__name__
        notice(f"診斷：{label}", f"HTTP {st}")
        if label.startswith("Python＋") and st == 410:
            # 2026-11-23 實測：GitHub 機器（美國）連一般 Python 帶瀏覽器標頭也拿 410 → 是這台機器被擋，不是節點的問題
            inconclusive("影片伺服器對 GitHub 的機器回 410（地區或 IP 限制），這裡測不出節點下載能不能用；要在台灣實機測")
    d = tempfile.mkdtemp()
    try:
        r = SandboxRunner(os.path.join(d, "nx"), os.path.join(d, "dl")).run(
            {"type": "video.download", "params": {"url": page, "media_url": media, "title": "vh-ci"}},
            timeout=1200, max_output_bytes=1 << 30)
        if not r.ok:
            print(f"::error title=節點下載失敗::{r.code} {r.message[:300]}")
            return 1
        for f in r.files:
            h = hashlib.sha256(open(f["path"], "rb").read()).hexdigest()
            assert h == f["sha256"], "雜湊對不起來"
            notice("節點下載成功（已刪除）", f"{f['size'] // 1024} KB、SHA-256 {h[:16]}…、沙箱清理 {r.cleanup_ok}")
        return 0
    finally:
        shutil.rmtree(d, ignore_errors=True)                 # 不留任何影片


if __name__ == "__main__":
    sys.exit(main())

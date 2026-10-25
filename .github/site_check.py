"""
每天檢查 Pornhub、XVideos 的網頁，還是不是腳本認得的寫法（網站改版警報）。
跟腳本用一樣的方式找影片：
  Pornhub：網頁裡有 flashvars_… 和 mediaDefinitions
  XVideos：網頁裡有 setVideoHLS / setVideoUrlHigh
狀態跟上次不一樣（壞了、或修好了）才貼到 Discord，正常的日子不吵人。

用法：python site_check.py 狀態檔.json
"""
import json
import os
import re
import sys
import urllib.error
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

# 地區封鎖、年齡驗證之類的頁面：代表「這台檢查用的主機看不到」，不是網站改版
BLOCKED_HINTS = re.compile(r"age verification|verify your age|not available in your (country|region|state)|"
                           r"access (is )?(restricted|denied)|unavailable for legal reasons", re.I)


def get(url, cookie=""):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8",
                                               **({"Cookie": cookie} if cookie else {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read(3_000_000).decode("utf-8", "ignore")


def check_pornhub():
    home = get("https://www.pornhub.com/", "age_verified=1; platform=pc")
    m = re.search(r'href="(/view_video\.php\?viewkey=[A-Za-z0-9]+)"', home)
    if not m:
        return "blocked" if BLOCKED_HINTS.search(home) else "broken", "首頁找不到影片連結"
    page = get("https://www.pornhub.com" + m.group(1), "age_verified=1; platform=pc")
    if "flashvars_" not in page:
        return "blocked" if BLOCKED_HINTS.search(page) else "broken", "影片頁找不到 flashvars（腳本靠它找影片）"
    if "mediaDefinitions" not in page:
        return "broken", "影片頁找不到 mediaDefinitions（下載會失效）"
    return "ok", ""


def check_xvideos():
    home = get("https://www.xvideos.com/")
    m = re.search(r'href="(/video\.?[a-z0-9]+/[^"]+)"', home)
    if not m:
        return "blocked" if BLOCKED_HINTS.search(home) else "broken", "首頁找不到影片連結"
    page = get("https://www.xvideos.com" + m.group(1))
    if not re.search(r"setVideo(UrlHigh|UrlLow|HLS)", page):
        return "blocked" if BLOCKED_HINTS.search(page) else "broken", "影片頁找不到 setVideoHLS（腳本靠它找影片）"
    return "ok", ""


def media_hosts(page):
    """影片網址放在哪些伺服器（給電腦版的允許清單用）"""
    hs = set()
    for u in re.findall(r'https?:(?:\\?/){2}([a-z0-9.-]+)[^"\'\s]*?\.(?:m3u8|mp4)', page, re.I):
        hs.add(".".join(u.lower().split(".")[-2:]))
    return sorted(hs)


def check_xnxx():
    """XNXX 跟 XVideos 同一家，播放器寫法一樣（setVideoHLS）
    2026-10 起首頁變成「分類頁」（縮圖框是空的，靠 JS 填），沒有影片連結 → 改去 /best、/hits 找"""
    for path in ("/", "/best", "/hits"):
        home = get("https://www.xnxx.com" + path)
        m = re.search(r'href="(/video-[a-z0-9]+/[^"]+)"', home)
        if m:
            break
    if not m:
        sample = sorted(set(re.findall(r'href=["\']?(/[^"\' >]*video[^"\' >]{0,30})', home, re.I)))[:8]
        return "blocked" if BLOCKED_HINTS.search(home) else "broken", f"首頁、/best、/hits 都找不到影片連結（{len(home)} 字，連結例：{sample}）"
    page = get("https://www.xnxx.com" + m.group(1))
    if not re.search(r"setVideo(UrlHigh|UrlLow|HLS)", page):
        return "blocked" if BLOCKED_HINTS.search(page) else "broken", "影片頁找不到 setVideoHLS"
    return "ok", "伺服器：" + ",".join(media_hosts(page))


def check_aylo(base, link_re):
    """YouPorn、RedTube 跟 Pornhub 同一家（Aylo），看影片清單是不是也叫 mediaDefinition(s)"""
    home = get(base, "age_verified=1")
    m = re.search(link_re, home)
    if not m:
        return "blocked" if BLOCKED_HINTS.search(home) else "broken", "首頁找不到影片連結"
    page = get(base.rstrip("/") + m.group(1), "age_verified=1")
    marks = [k for k in ("mediaDefinitions", "mediaDefinition", "flashvars_", "playervars", "videoUrl")
             if k in page]
    if not any(k.startswith("mediaDefinition") for k in marks):
        return "blocked" if BLOCKED_HINTS.search(page) else "broken", f"影片頁找不到 mediaDefinition（有：{marks}）"
    return "ok", f"有：{marks}｜伺服器：" + ",".join(media_hosts(page))


def run_checks():
    out = {}
    for name, fn in (("Pornhub", check_pornhub), ("XVideos", check_xvideos), ("XNXX", check_xnxx),
                     ("YouPorn", lambda: check_aylo("https://www.youporn.com/", r'href="(/watch/\d+/[^"]*)"')),
                     ("RedTube", lambda: check_aylo("https://www.redtube.com/", r'href="(/\d{4,})"'))):
        try:
            out[name] = fn()
        except urllib.error.HTTPError as e:
            out[name] = ("blocked", f"網站回應 {e.code}") if e.code in (403, 410, 451) else ("broken", f"網站回應 {e.code}")
        except Exception as e:
            out[name] = ("error", f"連不上（{type(e).__name__}）")
    return out


def notify(text):
    hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
    if not hook:
        print("沒有設定 DISCORD_WEBHOOK，略過通知")
        return
    payload = {"username": "Video Helper", "content": text[:1900]}
    req = urllib.request.Request(hook, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "VideoHelper-CI"})
    try:
        urllib.request.urlopen(req, timeout=20).read()
    except Exception as e:
        print(f"::warning title=Discord 通知失敗::{e}")


def probe_xnxx():
    """診斷模式：印出 XNXX 首頁的結構（標題、連結長相、有沒有用 JSON 放影片清單），不通知、不存狀態"""
    req = urllib.request.Request("https://www.xnxx.com/", headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.8"})
    with urllib.request.urlopen(req, timeout=30) as r:
        final, home = r.geturl(), r.read(3_000_000).decode("utf-8", "ignore")
    t = re.search(r"<title[^>]*>(.*?)</title>", home, re.S | re.I)
    hrefs = re.findall(r'href=["\']?([^"\' >]+)', home)
    shapes = {}
    for h in hrefs:                                   # 把數字、長字換掉，只看「連結長怎樣」
        k = re.sub(r"[0-9]+", "9", re.sub(r"/[^/?#]{25,}", "/…", h))[:60]
        shapes[k] = shapes.get(k, 0) + 1
    top = sorted(shapes.items(), key=lambda x: -x[1])[:25]
    print(f"::notice title=XNXX 首頁::網址 {final}｜{len(home)} 字｜標題 {t.group(1).strip()[:80] if t else '無'}｜連結 {len(hrefs)} 個")
    print("::notice title=XNXX 連結長相::" + "  ".join(f"{k}×{n}" for k, n in top))
    marks = {k: home.count(k) for k in ("thumb-block", "data-id", "data-eid", "/video-", "/video", "xv.", "__NEXT_DATA__",
                                        "application/json", "window.", "cf-chl", "captcha", "<noscript")}
    print("::notice title=XNXX 關鍵字::" + json.dumps(marks, ensure_ascii=False))
    i = home.find('class="thumb-block')
    print("::notice title=XNXX 縮圖框::" + re.sub(r"\s+", " ", home[i:i + 900] if i >= 0 else "找不到"))
    for path in ("/best", "/hits", "/search/hd"):
        try:
            pg = get("https://www.xnxx.com" + path)
            print(f"::notice title=XNXX {path}::{len(pg)} 字｜/video- 連結 {len(re.findall(r'href=\"/video-', pg))} 個｜thumb-block {pg.count('thumb-block')}")
        except Exception as e:
            print(f"::notice title=XNXX {path}::{e}")


def main(state_path):
    if os.environ.get("PROBE") == "xnxx":
        probe_xnxx()
        sys.exit(1)                                   # 故意失敗：後面「記住狀態」就不會跑
    try:
        old = json.load(open(state_path, encoding="utf-8"))
    except Exception:
        old = {}
    now = run_checks()
    lines = []
    for name, (st, why) in now.items():
        print(f"{name}: {st} {why}")
        print(f"::notice title={name}::{st} {why}")
        was = old.get(name, {}).get("status", "new")
        if st == was or was == "new":                    # 剛加入的網站第一次檢查不通知
            continue
        if st == "broken":
            lines.append(f"⚠ **{name} 可能改版了**：{why}\n　腳本可能會漏掉廣告或不能下載，作者會盡快更新。")
        elif st == "ok" and was == "broken":
            lines.append(f"✅ **{name} 恢復正常**：腳本又認得網頁了。")
        # blocked／error：檢查用的主機看不到網站（地區限制、暫時連不上），不算改版，不通知
    if lines:
        notify("🔔 **網站改版警報**\n" + "\n".join(lines))
    json.dump({k: {"status": s, "why": w} for k, (s, w) in now.items()},
              open(state_path, "w", encoding="utf-8"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main(sys.argv[1])

"""
Video Helper 電腦版
分頁 ①「安裝擋廣告」：一步一步帶使用者在 Chrome 裝好暴力猴和 Video Helper 腳本
分頁 ②「下載影片」：貼上網址 → 選畫質 → 加入下載佇列（一次排很多部，依序下載；可取消、可重試）
分頁 ③「下載紀錄」：完成／失敗／取消的任務，可重新下載、打開檔案位置
分頁 ⑤「統計」：每次下載的預估和實際結果（只存數字和網站名稱），並用來校正之後的預估
分頁 ④「設定」：下載資料夾、預設畫質、檔名格式、關閉視窗的行為、備份匯出／匯入、檢查更新、錯誤日誌
系統匣：關掉視窗時可以縮到右下角繼續下載，下載完成會跳通知
瀏覽器連動：配對後，瀏覽器腳本可以把支援網站的影片直接送進下載佇列（只聽本機 127.0.0.1）

執行前先安裝：
    pip install yt-dlp imageio-ffmpeg pystray pillow
    （打包成 EXE 時不用 imageio-ffmpeg，改附 .github/ffmpeg/build.sh 編出來的迷你 ffmpeg）
執行：
    python video_helper_app.py

程式結構：
    上半部是「不依賴視窗」的工具（設定、紀錄、日誌、下載），可以單獨測試
    下半部是視窗介面（class App），只負責顯示和按鈕
"""

import sys

# Nexus 沙箱子程序（V3）：VideoHelper.exe --nexus-run <工作資料夾>。
# 放在最上面：不載入下面的視窗、下載器、設定，子程序只跑 nexus/sandbox/child.py
if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] in ("--nexus-run", "--probe-sleeper"):
    from nexus.sandbox.child import entry as _nexus_child_entry
    sys.exit(_nexus_child_entry())

import os
import re
import json
import glob
import hmac
import secrets
import hashlib
import time
import queue
import logging
import threading
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime, timedelta
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:                                        # 用 Windows 自己信任的憑證清單（防毒軟體、公司網路會換憑證，Python 內建的清單不認得）
    import truststore
    truststore.inject_into_ssl()
except Exception:
    pass

import yt_dlp

try:                                        # 系統匣（沒裝的話就沒有這個功能，其他照常）
    import pystray
    from PIL import Image, ImageDraw
except Exception:
    pystray = None

def find_ffmpeg():
    """
    找 ffmpeg（用來合併影像聲音、換成 MP4 包裝），依序：
      1. 環境變數 VIDEO_HELPER_FFMPEG（測試用）
      2. EXE 裡附的迷你版（只會接檔、換包裝，約 2 MB）
      3. 原始碼資料夾旁邊的 ffmpeg_bin/
      4. 有裝 imageio-ffmpeg 的話用它的（用 Python 直接執行時）
      5. 電腦裡本來就有的 ffmpeg
    """
    env = os.environ.get("VIDEO_HELPER_FFMPEG")
    if env:
        return env
    exe = "ffmpeg.exe" if sys.platform.startswith("win") else "ffmpeg"
    here = os.path.dirname(os.path.abspath(__file__))
    for base in (getattr(sys, "_MEIPASS", None), os.path.dirname(sys.executable) if getattr(sys, "frozen", False) else None, here):
        if not base:
            continue
        for sub in ("ffmpeg", "ffmpeg_bin"):
            cand = os.path.join(base, sub, exe)
            if os.path.isfile(cand):
                return cand
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass
    import shutil
    return shutil.which("ffmpeg")


FFMPEG = find_ffmpeg()

VERSION = "1.43.0"
WEB_UNAVAILABLE = ""          # v1.36.0：設定要新介面但這次開不了的原因（main 填）
REPO = "dadanny1122/video-helper"

# 安裝精靈用到的網址
VM_ID = "jinjaccalgkegednnccohejagnlnfdag"   # 暴力猴在 Chrome 商店的編號
URL_VM_STORE = f"https://chromewebstore.google.com/detail/{VM_ID}"
URL_VM_SETTINGS = f"chrome://extensions/?id={VM_ID}"
URL_SCRIPT = f"https://raw.githubusercontent.com/{REPO}/main/video-helper.user.js"

QUALITY_CHOICES = ["最高", "2160", "1440", "1080", "720", "480", "360"]

# 檔名格式：選單上顯示的名字 → yt-dlp 的檔名模板
#   %(title).100B      標題（最多 100 位元組，避免檔名太長）
#   %(height& [{}p]|)s 有畫質就加「 [1080p]」，沒有就不加
#   %(extractor_key)s  網站名稱（Pornhub、XVideos…）
#   {date}             下載日期（程式自己填）
FILENAME_TEMPLATES = {
    "標題 [畫質]":            "%(title).100B%(height& [{}p]|)s.%(ext)s",
    "標題":                   "%(title).100B.%(ext)s",
    "下載日期 標題 [畫質]":   "{date} %(title).100B%(height& [{}p]|)s.%(ext)s",
    "網站 - 標題 [畫質]":     "%(extractor_key)s - %(title).100B%(height& [{}p]|)s.%(ext)s",
    "依網站分資料夾/標題 [畫質]": "%(extractor_key)s/%(title).100B%(height& [{}p]|)s.%(ext)s",
}
DEFAULT_TEMPLATE = "標題 [畫質]"


def build_outtmpl(name):
    """把選單上的檔名格式換成 yt-dlp 的模板"""
    t = FILENAME_TEMPLATES.get(name) or FILENAME_TEMPLATES[DEFAULT_TEMPLATE]
    return t.replace("{date}", datetime.now().strftime("%Y-%m-%d"))


def preview_filename(name, title="影片標題", height=1080, site="Pornhub"):
    """設定頁的範例檔名（不用 yt-dlp，自己照規則拼）"""
    t = build_outtmpl(name)
    t = t.replace("%(title).100B", title).replace("%(height& [{}p]|)s", f" [{height}p]" if height else "")
    return t.replace("%(extractor_key)s", site).replace("%(ext)s", "mp4")


# ======================================================================
# 資料放哪裡
# ======================================================================

def data_dir():
    """設定、紀錄、日誌都放這裡（Windows：%APPDATA%\\VideoHelper）"""
    base = os.environ.get("VIDEO_HELPER_HOME")          # 測試時可以指定別的位置
    if not base:
        if sys.platform.startswith("win") and os.environ.get("APPDATA"):
            base = os.path.join(os.environ["APPDATA"], "VideoHelper")
        else:
            base = os.path.join(os.path.expanduser("~"), ".video_helper")
    os.makedirs(base, exist_ok=True)
    return base


def default_download_dir():
    return os.path.join(os.path.expanduser("~"), "Downloads")


# ======================================================================
# 設定
# ======================================================================

DEFAULT_SETTINGS = {
    "download_dir": "",          # 空的 = 使用者的「下載」資料夾
    "default_quality": "最高",
    "rate_limit_mbps": 0,             # 下載速度上限（Mbps），0＝不限制
    "theme": "auto",                  # 外觀：auto 跟系統／dark 深色／light 淺色
    "show_home": True,                # 打開時先顯示首頁（大卡片選單）
    "skip_downloaded": True,          # 批次、瀏覽器送來的：下載過（檔案還在）就跳過
    "smart_segments": "observe",      # 智慧分段：auto＝自動調整／observe＝只觀察（記錄建議、照舊 6 段）／off＝關閉
    "seg_state": {},                  # 每個網站的分段狀態：網站 → {"level", "stable", "hold"}
    "keep_history": True,
    "keep_stats": True,               # 記錄下載統計（只存數字和網站名稱）
    "filename_template": DEFAULT_TEMPLATE,
    "close_action": "下載中才詢問",   # 關閉視窗時：下載中才詢問 / 一律縮到系統匣 / 直接結束
    "last_version": "",               # 上次執行的版本（版本變了就自動備份）
    "bridge_enabled": False,          # 瀏覽器連動（配對後才會打開）
    "bridge_tokens": [],              # 已配對的瀏覽器（只存雜湊值，不存原本的密碼）
    "bridge_meta": {},                # 雜湊值 → {"paired", "last", "uses", "ua"}（給「🔒 安全」分頁看）
    "ai_usage": {},                   # 月份 → {"classify"/"translate": {"calls", "in", "out"}}（只記次數和字數，不記內容）
    "log_keep_days": 14,              # 錯誤日誌超過幾天自動刪掉（0＝不刪）
    "drag_snapshot": True,
    "ui": "web",                      # 介面：v1.42.0 起只用新介面（classic 只在打不開時自動退回）
    "ui_chosen": False,               # v1.40.0 用過；v1.42.0 起不再看
    "ui_fail_shown": False,           # 新介面打不開的提示只講一次
    "tray_always": True,              # v1.35.0：系統匣小圖示一直都在（不只縮小時）            # v1.34.0：拖動主視窗時先蓋一張畫面照片（Windows 上比較順）
    "mbps": 0,                   # 上次量到的網速
    "setup_seen": False,         # 看過「安裝擋廣告」了（第一次打開才自動切過去）
    "ai_key": "",                # AI 判斷廣告網域用的 API 金鑰（只存在這台電腦，不會匯出）
    "ai_cache": {},              # AI 判斷過的網域：網域 → {verdict, reason, t}（問過就不再問）
    "sub_lang": "自動偵測",      # AI 字幕：影片說的語言
    "sub_model": "快（small，約 190 MB）",
    "sub_translate": True,       # 有 AI 金鑰就翻成中文
    "sub_format": "SRT（最通用）",
    "sub_dual": False,           # 另外做一份「中文＋原文」雙語字幕
    "sub_skip_done": True,       # 批次：已經有字幕的跳過
}


# ---- 外部連線紀錄（給「🔒 安全」分頁看）：在最底層記下這次開著時連過哪些網站（只記網域、次數、時間，不存檔）----
NET_SEEN = {}
_NET_LOCK = threading.Lock()


def _install_net_log():
    import socket
    if getattr(socket.getaddrinfo, "_vh", False):
        return
    orig = socket.getaddrinfo

    def getaddrinfo(host, *a, **k):
        try:
            h = str(host).lower().rstrip(".")
            if h and h not in ("localhost", "127.0.0.1", "::1"):
                now = time.time()
                with _NET_LOCK:
                    e = NET_SEEN.setdefault(h, {"n": 0, "first": now})
                    e["n"] += 1
                    e["last"] = now
        except Exception:
            pass
        return orig(host, *a, **k)
    getaddrinfo._vh = True
    socket.getaddrinfo = getaddrinfo


_install_net_log()


def browser_name(ua):
    """User-Agent → 「Chrome（Windows）」這種好認的名字（只存這個，不存整串）"""
    ua = ua or ""
    b = next((n for k, n in (("Edg/", "Edge"), ("OPR/", "Opera"), ("Firefox/", "Firefox"), ("Chrome/", "Chrome"),
                             ("Safari/", "Safari")) if k in ua), "瀏覽器")
    o = next((n for k, n in (("Windows", "Windows"), ("Android", "Android"), ("iPhone", "iPhone"), ("iPad", "iPad"),
                             ("Mac OS", "Mac"), ("Linux", "Linux")) if k in ua), "")
    return f"{b}（{o}）" if o else b


def clean_old_logs(days):
    """錯誤日誌超過 days 天沒更新就刪掉；回傳刪了幾個"""
    if not days:
        return 0
    n, base = 0, os.path.join(data_dir(), "video_helper.log")
    for p in [base] + [f"{base}.{i}" for i in range(1, 5)]:
        try:
            if os.path.isfile(p) and time.time() - os.path.getmtime(p) > days * 86400:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n


# ---- 祕密資料（AI 金鑰）加密：用 Windows 內建的 DPAPI，只有「這台電腦的這個 Windows 帳號」解得開 ----
#      設定檔被複製走、或同台電腦的其他帳號都讀不到；不是 Windows 時就照原樣存
SECRET_KEYS = ("ai_key",)


def _dpapi(data, encrypt):
    import ctypes
    from ctypes import wintypes

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]
    buf = ctypes.create_string_buffer(data, len(data))
    inp, out = BLOB(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char))), BLOB()
    fn = ctypes.windll.crypt32.CryptProtectData if encrypt else ctypes.windll.crypt32.CryptUnprotectData
    if not fn(ctypes.byref(inp), None, None, None, None, 0x1, ctypes.byref(out)):   # 0x1＝不跳任何視窗
        raise OSError("DPAPI 失敗")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(out.pbData)


def protect_secret(v):
    if not v or not sys.platform.startswith("win") or str(v).startswith("dpapi:"):
        return v
    try:
        import base64
        return "dpapi:" + base64.b64encode(_dpapi(str(v).encode("utf-8"), True)).decode("ascii")
    except Exception:
        log.warning("金鑰加密失敗，這次先用明文存（下次儲存會再試加密）")
        return v


def unprotect_secret(v):
    if not (isinstance(v, str) and v.startswith("dpapi:")):
        return v
    try:
        import base64
        return _dpapi(base64.b64decode(v[6:]), False).decode("utf-8")
    except Exception:
        log.warning("金鑰解不開（設定檔可能是從別台電腦複製來的），請重新輸入")
        return ""


class Settings:
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "settings.json")
        self.data = dict(DEFAULT_SETTINGS)
        self.load()
        for k in SECRET_KEYS:                       # 舊版存的是明文 → 換成加密的
            v = self.data.get(k)
            if v and not str(v).startswith("dpapi:") and protect_secret(v) != v:
                self.data[k] = protect_secret(v)
                self.save()

    def load(self):
        try:
            with open(self.path, encoding="utf-8") as f:
                self.data.update(json.load(f))
        except Exception:
            # 舊版（v1.3 以前）把網速存在 ~/.video_helper.json，搬過來
            try:
                with open(os.path.join(os.path.expanduser("~"), ".video_helper.json"), encoding="utf-8") as f:
                    self.data["mbps"] = float(json.load(f).get("mbps", 0))
            except Exception:
                pass

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)            # 先寫暫存檔再換掉，避免寫到一半當機把設定弄壞
        except Exception:
            log.exception("儲存設定失敗")

    def __getitem__(self, k):
        v = self.data.get(k, DEFAULT_SETTINGS.get(k))
        return unprotect_secret(v) if k in SECRET_KEYS else v

    def __setitem__(self, k, v):
        self.data[k] = protect_secret(v) if k in SECRET_KEYS else v

    @property
    def download_dir(self):
        d = self["download_dir"] or default_download_dir()
        os.makedirs(d, exist_ok=True)
        return d


# ======================================================================
# 下載紀錄
# ======================================================================

class History:
    MAX = 500

    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "history.json")
        self.items = []
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = json.load(f)
        except Exception:
            self.items = []

    def add(self, **rec):
        rec.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.items.insert(0, rec)                 # 最新的放最前面
        del self.items[self.MAX:]
        self.save()
        return rec

    def remove(self, index):
        if 0 <= index < len(self.items):
            del self.items[index]
            self.save()

    def clear(self):
        self.items = []
        self.save()

    def merge(self, items):
        """匯入別的紀錄：重複的不加，依時間排（新的在前），回傳新增幾筆"""
        key = lambda r: (r.get("time"), r.get("url"), r.get("height"), r.get("status"))
        have = {key(r) for r in self.items}
        new = [r for r in items if isinstance(r, dict) and key(r) not in have]
        self.items = sorted(self.items + new, key=lambda r: r.get("time") or "", reverse=True)[:self.MAX]
        self.save()
        return len(new)

    def save(self):
        self.version = getattr(self, "version", 0) + 1     # v1.37.0 新介面看這個號碼決定要不要重送紀錄
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("儲存下載紀錄失敗")


# ======================================================================
# 下載統計：預估 vs 實際，用來校正之後的預估
#   只存「網站名稱 + 數字」（大小、秒數、畫質），不存標題和網址
# ======================================================================

def site_of(url):
    h = (urllib.parse.urlparse(url or "").hostname or "").lower()
    return ".".join(h.split(".")[-2:]) if h else "?"


def median(xs):
    xs = sorted(xs)
    n = len(xs)
    if not n:
        return None
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2


class Stats:
    MAX = 300
    MIN_SAMPLES = 3          # 至少 3 次才拿來校正
    RECENT = 20              # 只看最近 20 次

    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "stats.json")
        try:
            with open(self.path, encoding="utf-8") as f:
                self.items = [r for r in json.load(f) if isinstance(r, dict)]
        except Exception:
            self.items = []

    def add(self, **rec):
        rec.setdefault("time", datetime.now().strftime("%Y-%m-%d %H:%M"))
        if rec.get("seconds") and rec.get("bytes"):
            rec["mbps"] = rec["bytes"] * 8 / rec["seconds"] / 1e6
        self.items.insert(0, rec)
        del self.items[self.MAX:]
        self.save()

    def clear(self):
        self.items = []
        self.save()

    def save(self):
        try:
            write_json(self.path, self.items)
        except Exception:
            log.exception("儲存統計失敗")

    def ratios(self, kind, site=None):
        """kind='time'：實際秒數 ÷ 預估秒數；kind='size'：實際大小 ÷ 預估大小（只看「約」的估算）"""
        out = []
        for r in self.items:
            if site and r.get("site") != site:
                continue
            if kind == "time" and r.get("est_seconds") and r.get("seconds"):
                out.append(r["seconds"] / r["est_seconds"])
            elif kind == "size" and not r.get("exact") and r.get("est_bytes") and r.get("bytes"):
                out.append(r["bytes"] / r["est_bytes"])
            if len(out) >= self.RECENT:
                break
        return out

    def factor(self, kind, site=None):
        """回傳 (校正倍數, 用了幾次)；樣本不夠回傳 (None, 次數)。同網站樣本不夠就用全部的"""
        rs = self.ratios(kind, site)
        if site and len(rs) < self.MIN_SAMPLES:
            rs = self.ratios(kind)
        if len(rs) < self.MIN_SAMPLES:
            return None, len(rs)
        return min(5.0, max(0.2, median(rs))), len(rs)

    def summary(self):
        """每個網站：次數、總量、平均速度、時間預估誤差、大小預估誤差（誤差＝跟實際差多少 %，取中位數）"""
        sites = {}
        for r in self.items:
            sites.setdefault(r.get("site", "?"), []).append(r)
        rows = []
        for site, rs in sorted(sites.items(), key=lambda kv: -len(kv[1])):
            tot = sum(r.get("bytes") or 0 for r in rs)
            secs = sum(r.get("seconds") or 0 for r in rs)
            terr = median([abs(x - 1) for x in self.ratios("time", site)])
            serr = median([abs(x - 1) for x in self.ratios("size", site)])
            rows.append({"site": site, "count": len(rs), "bytes": tot,
                         "mbps": tot * 8 / secs / 1e6 if secs else 0,
                         "time_err": terr, "size_err": serr})
        return rows


# ======================================================================
# 備份：匯出／匯入設定與紀錄；版本更新時自動備份
# ======================================================================

BACKUP_KIND = "VideoHelper-backup"
# 不放進備份檔的：自動產生的值，以及配對密碼（不能跟著備份檔到處跑）
NOT_EXPORTED = ("mbps", "last_version", "bridge_enabled", "bridge_tokens", "bridge_meta", "ai_key", "ai_cache", "ai_usage")
CLOSE_CHOICES = ["下載中才詢問", "一律縮到系統匣", "直接結束"]


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def export_backup(path, settings, history=None):
    """把設定（和紀錄）存成一個備份檔；網速、版本這種自動產生的不存"""
    data = {
        "kind": BACKUP_KIND,
        "version": VERSION,
        "exported": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "settings": {k: v for k, v in settings.data.items() if k in DEFAULT_SETTINGS and k not in NOT_EXPORTED},
    }
    if history is not None:
        data["history"] = history.items
    write_json(path, data)


def import_backup(path):
    """讀備份檔，回傳 (設定, 紀錄或 None, 警告清單)；不認得或不合理的值會被丟掉"""
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, dict) or data.get("kind") != BACKUP_KIND:
        raise ValueError("這不是 Video Helper 的備份檔")
    warns = []
    raw = data.get("settings") or {}
    s = {}
    for k, v in raw.items():
        if k not in DEFAULT_SETTINGS or k in NOT_EXPORTED:
            continue
        if k == "default_quality" and v not in QUALITY_CHOICES:
            warns.append(f"預設畫質「{v}」不認得，略過")
            continue
        if k == "filename_template" and v not in FILENAME_TEMPLATES:
            warns.append(f"檔名格式「{v}」不認得，略過")
            continue
        if k == "close_action" and v not in CLOSE_CHOICES:
            continue
        if k == "download_dir" and v and not os.path.isdir(v):
            warns.append(f"下載資料夾「{v}」在這台電腦上不存在，改用預設的「下載」資料夾")
            v = ""
        if k == "keep_history":
            v = bool(v)
        s[k] = v
    hist = data.get("history")
    if hist is not None and not isinstance(hist, list):
        warns.append("紀錄格式不對，略過")
        hist = None
    return s, hist, warns


def backups_dir():
    return os.path.join(data_dir(), "backups")


def auto_backup_on_upgrade(settings, history, keep=5):
    """版本變了 → 先把舊的設定和紀錄備份一份（最多留 5 份），回傳備份檔路徑或 None"""
    last = settings["last_version"]
    made = None
    if last and last != VERSION:
        made = os.path.join(backups_dir(), f"{datetime.now():%Y%m%d-%H%M%S}_v{last}.json")
        try:
            export_backup(made, settings, history)
            files = sorted(glob.glob(os.path.join(backups_dir(), "*.json")))
            for old in files[:-keep]:
                os.remove(old)
            log.info("版本 v%s → v%s，已自動備份：%s", last, VERSION, made)
        except Exception:
            log.exception("自動備份失敗")
            made = None
    if last != VERSION:
        settings["last_version"] = VERSION
        settings.save()
    return made


def make_icon_image(size=64):
    """系統匣圖示：紅底白色向下箭頭"""
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle([2, 2, size - 2, size - 2], radius=size // 5, fill=(229, 57, 53, 255))
    w = size
    d.rectangle([w * 0.42, w * 0.2, w * 0.58, w * 0.5], fill="white")
    d.polygon([(w * 0.26, w * 0.46), (w * 0.74, w * 0.46), (w * 0.5, w * 0.76)], fill="white")
    d.rectangle([w * 0.26, w * 0.8, w * 0.74, w * 0.86], fill="white")
    return img


# ======================================================================
# 下載佇列（只管「排隊」的邏輯，真正的下載由視窗那邊一個一個叫）
# ======================================================================

WAITING, RUNNING, DONE, FAILED, CANCELLED = "等待中", "下載中", "完成", "失敗", "取消"
PAUSED = "暫停"
RETRY_WAIT = "等重試"          # 失敗了，但程式判斷可以自動重試，正在等時間到
NEEDS_ACTION = "需要處理"      # 重試也沒用（影片被移除、要登入…），等使用者決定


# ---- 重複偵測：同一支影片（不管網址後面多了什麼參數）算同一個 ----
VIDEO_ID_RULES = (
    ("pornhub.com", r"[?&]viewkey=([A-Za-z0-9]+)"),
    ("xvideos.com", r"/video\.?([a-z0-9]+)/"),
    ("xnxx.com", r"/video-([a-z0-9]+)/"),
    ("youporn.com", r"/watch/(\d+)"),
    ("redtube.com", r"^/(\d+)"),
    ("xhamster.com", r"-([A-Za-z0-9]+)/?$"),
    ("tube8.com", r"/porn-video/(\d+)"),
    ("youjizz.com", r"-(\d+)\.html"),
    ("spankbang.com", r"^/([a-z0-9]+)/(?:video|play)/"),
)


def video_key(url):
    """網址 → 「網站:影片編號」；認不出來就用「網域＋路徑」"""
    try:
        u = urllib.parse.urlparse(str(url).strip())
    except Exception:
        return str(url)
    host = (u.hostname or "").lower()
    for dom, rx in VIDEO_ID_RULES:
        if host == dom or host.endswith("." + dom):
            m = re.search(rx, u.path + ("?" + u.query if u.query else "") if "viewkey" in rx else u.path)
            if m:
                return f"{dom}:{m.group(1)}"
            return f"{dom}:{u.path.rstrip('/')}"
    return host.removeprefix("www.") + u.path.rstrip("/")


def find_downloaded(history_items, url):
    """下載紀錄裡有沒有同一支影片「下載完成、檔案還在」的；有就回傳那一筆"""
    k = video_key(url)
    for rec in history_items:
        if rec.get("status") == DONE and rec.get("file") and video_key(rec.get("url", "")) == k and os.path.isfile(rec["file"]):
            return rec
    return None


class TaskQueue:
    def __init__(self):
        self.tasks = []
        self._next_id = 1

    def add(self, task):
        task = dict(task, id=self._next_id, status=WAITING, progress=0, note="")
        self._next_id += 1
        self.tasks.append(task)
        return task

    def get(self, tid):
        return next((t for t in self.tasks if t["id"] == tid), None)

    def running(self):
        return next((t for t in self.tasks if t["status"] == RUNNING), None)

    def next_waiting(self, skip=None):
        """下一個等待中的；skip(task) 回傳 True 的先跳過（例如網站在冷卻）"""
        return next((t for t in self.tasks if t["status"] == WAITING and not (skip and skip(t))), None)

    def count(self, *statuses):
        return sum(t["status"] in statuses for t in self.tasks)

    def is_duplicate(self, url, height):
        """同一支影片（網址參數不同也算）、同一個畫質已經在排隊、下載中或暫停"""
        k = video_key(url)
        return any(video_key(t["url"]) == k and t["fmt"]["height"] == height
                   and t["status"] in (WAITING, RUNNING, PAUSED, RETRY_WAIT)
                   for t in self.tasks)

    def remove(self, tid):
        """移除等待中或已結束的任務（下載中的要先取消）"""
        t = self.get(tid)
        if t and t["status"] != RUNNING:
            self.tasks.remove(t)
            return True
        return False

    def retry_failed(self):
        n = 0
        for t in self.tasks:
            if t["status"] in (FAILED, NEEDS_ACTION):
                t.update(status=WAITING, progress=0, note="", tries=0)
                n += 1
        return n

    def due_retries(self, now=None):
        """「等重試」時間到了 → 放回等待中；回傳放回幾個"""
        now = now or time.time()
        n = 0
        for t in self.tasks:
            if t["status"] == RETRY_WAIT and now >= t.get("retry_at", 0):
                t["status"] = WAITING
                n += 1
        return n

    def clear_finished(self):
        """清掉已結束的；暫停的留著（還要接著下載）"""
        self.tasks = [t for t in self.tasks if t["status"] in (WAITING, RUNNING, PAUSED, RETRY_WAIT)]

    def resume_paused(self):
        n = 0
        for t in self.tasks:
            if t["status"] == PAUSED:
                t["status"] = WAITING
                n += 1
        return n


# ======================================================================
# 錯誤日誌
# ======================================================================

def setup_log():
    lg = logging.getLogger("video_helper")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    path = os.path.join(data_dir(), "video_helper.log")
    try:                                         # 太舊的日誌先刪掉（「🔒 安全」可以設幾天）
        days = json.load(open(os.path.join(data_dir(), "settings.json"), encoding="utf-8")).get("log_keep_days", 14)
        clean_old_logs(int(days or 0))
    except Exception:
        clean_old_logs(14)
    h = RotatingFileHandler(path, maxBytes=1_000_000, backupCount=2, encoding="utf-8")   # 最多約 3 MB
    h.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(message)s"))
    lg.addHandler(h)
    lg.log_path = path
    return lg


log = setup_log()


# ======================================================================
# 瀏覽器連動：只聽本機（127.0.0.1）的小型連線服務
#
# 安全設計：
#   1. 只綁 127.0.0.1，別台電腦連不進來
#   2. 檢查 Host 標頭（防 DNS rebinding：壞網站把自己的網域指到 127.0.0.1）
#   3. 拒絕一般網頁送來的請求（Origin 是 http/https 開頭的一律拒絕），只收擴充功能（暴力猴）的
#   4. 一定要先配對：電腦版顯示 6 位數配對碼（5 分鐘內有效、猜錯 5 次就作廢），
#      配對成功給一組長密碼；之後每次請求都要帶密碼。電腦裡只存密碼的雜湊值
#   5. 只接受支援下載的網站（Pornhub、XVideos、XNXX、YouPorn、RedTube、xHamster、Tube8、YouJizz、SpankBang）的網址；只做「加入下載佇列」；每分鐘最多 30 次
# ======================================================================

BRIDGE_PORTS = list(range(47321, 47326))
BRIDGE_API = 1        # 腳本和電腦版溝通的「語言版本」：改了請求格式才 +1
# 支援下載的網站：唯一來源是 nexus/net/sites.json（電腦版、節點下載、安卓 vh_mobile.py 共用）
from nexus.net import sites as _sites  # noqa: E402, I001
BRIDGE_HOSTS = _sites.page_hosts()
# 瀏覽器送來的「影片真正的位址」只接受這些網站自己的影片伺服器
BRIDGE_MEDIA_HOSTS = _sites.media_hosts()
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
PAIR_SECONDS = 300
PAIR_MAX_FAILS = 5
RATE_PER_MIN = 30


def log_url(url):
    """寫進錯誤日誌時只記網站名稱，不記完整網址（日誌不該變成觀看紀錄）"""
    try:
        return (urllib.parse.urlparse(url).hostname or "?").removeprefix("www.") + "/…"
    except Exception:
        return "?"


def bridge_url_allowed(url, hosts=BRIDGE_HOSTS):
    try:
        u = urllib.parse.urlparse(str(url))
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    h = (u.hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in hosts)


# ----------------------------------------------------------------------
# AI 判斷廣告網域：只送「網域名稱」和網站名稱，不送影片標題、網址、觀看紀錄
# ----------------------------------------------------------------------

AI_API = "https://api.anthropic.com/v1/messages"
AI_MODEL = "claude-haiku-5-5"            # 便宜、快；判斷網域綽綽有餘
AI_MAX_DOMAINS = 30
AI_CACHE_DAYS = 30
AI_CACHE_MAX = 800
AI_VERDICTS = {"ad": "廣告", "tracker": "追蹤", "popup": "彈窗廣告", "site": "網站自己的", "cdn": "影片／圖片伺服器",
               "service": "一般服務", "unknown": "不確定"}
AI_BLOCK = ("ad", "tracker", "popup")    # 這幾種建議封鎖
DOMAIN_RE = __import__("re").compile(r"^(?=.{3,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def ai_prompt(site, domains):
    return (
        "你是廣告封鎖規則的專家。使用者在影片網站「" + site + "」上，這一頁連到了下面這些其他網域。\n"
        "請判斷每個網域是什麼，用這幾種之一：\n"
        "ad＝廣告網路、popup＝彈窗／跳轉廣告、tracker＝追蹤或統計、cdn＝這個網站自己的影片或圖片伺服器、"
        "site＝這個網站自己的其他網域、service＝一般服務（例如字型、驗證碼、播放器函式庫）、unknown＝不確定。\n"
        "封鎖 cdn、site、service 會讓網站壞掉，所以不確定時寧可回答 unknown。\n"
        "只回 JSON，不要其他文字，格式："
        '[{"domain":"…","verdict":"ad","reason":"一句繁體中文，20 字以內"}]\n\n'
        "網域：\n" + "\n".join(domains))


AI_USAGE_HOOK = None          # App 設定：AI 每次回答後呼叫 hook(種類, usage)（只記字數，不記內容）


def note_ai_usage(kind, data):
    try:
        if AI_USAGE_HOOK:
            AI_USAGE_HOOK(kind, data.get("usage") or {})
    except Exception:
        pass


def parse_ai_json(text):
    """從 AI 的回答裡挖出 JSON 陣列（有時候會多包一層 ```）"""
    i, j = text.find("["), text.rfind("]")
    if i < 0 or j <= i:
        raise ValueError("AI 的回答看不懂")
    return json.loads(text[i:j + 1])


def ai_classify(key, site, domains, timeout=40):
    """問 AI；回傳 {網域: {"verdict", "reason"}}。失敗丟出 RuntimeError（訊息給使用者看）"""
    body = json.dumps({"model": AI_MODEL, "max_tokens": 2000,
                       "messages": [{"role": "user", "content": ai_prompt(site, domains)}]}).encode("utf-8")
    req = urllib.request.Request(AI_API, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            note_ai_usage("classify", data)
    except urllib.error.HTTPError as e:
        msg = {401: "API 金鑰不對", 403: "這個金鑰沒有權限", 429: "問太快了或額度用完，等一下再試",
               529: "AI 伺服器太忙，等一下再試"}.get(e.code, f"AI 伺服器回應錯誤（{e.code}）")
        raise RuntimeError(msg) from None
    except Exception as e:
        raise RuntimeError(f"連不到 AI 伺服器（{e}）") from None
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    out = {}
    for it in parse_ai_json(text):
        d = str(it.get("domain", "")).lower().strip()
        v = str(it.get("verdict", "unknown")).lower().strip()
        if d in domains:
            out[d] = {"verdict": v if v in AI_VERDICTS else "unknown", "reason": str(it.get("reason", ""))[:60]}
    return out


# ----------------------------------------------------------------------
# 公開廣告清單（免費）：StevenBlack/hosts（MIT 授權），整理自多個社群維護的廣告、惡意網站清單
# 存在設定資料夾，7 天更新一次；查的時候只在自己電腦比對，不會送資料出去
# ----------------------------------------------------------------------

BLOCKLIST_URL = "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts"
BLOCKLIST_DAYS = 7


class BlockList:
    def __init__(self, path=None, url=BLOCKLIST_URL):
        self.path = path or os.path.join(data_dir(), "blocklist.txt")
        self.url = url
        self.hosts = None
        self.lock = threading.Lock()
        self.error = ""

    def age_days(self):
        try:
            return (time.time() - os.path.getmtime(self.path)) / 86400
        except OSError:
            return None

    def download(self, timeout=60):
        """下載新的清單；失敗就保留舊的"""
        req = urllib.request.Request(self.url, headers={"User-Agent": f"VideoHelper/{VERSION}"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = r.read(20_000_000)
        if data.count(b"\n0.0.0.0 ") < 1000:
            raise IOError("下載到的清單不對")
        tmp = self.path + ".tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, self.path)
        with self.lock:
            self.hosts = None                       # 下次查的時候重新讀
        log.info("廣告清單已更新（%.1f MB）", len(data) / 1e6)

    def load(self):
        """需要時才讀進記憶體；太舊或沒有就先下載"""
        age = self.age_days()
        if age is None or age > BLOCKLIST_DAYS:
            try:
                self.download()
                self.error = ""
            except Exception as e:
                self.error = f"清單下載失敗（{e}）"
                log.warning("廣告清單下載失敗：%s", e)
                if age is None:
                    return set()
        with self.lock:
            if self.hosts is None:
                hs = set()
                with open(self.path, encoding="utf-8", errors="ignore") as f:
                    for line in f:
                        if line.startswith("0.0.0.0 "):
                            h = line[8:].split("#", 1)[0].strip().lower()
                            if h and h != "0.0.0.0":
                                hs.add(h)
                self.hosts = hs
            return self.hosts

    def count(self):
        return len(self.hosts) if self.hosts is not None else 0

    def match(self, host):
        """這個網址（或它的上層網域）在清單裡 → 回傳清單裡的那一筆，不在回傳 None"""
        hs = self.load()
        parts = host.lower().split(".")
        for i in range(len(parts) - 1):
            cand = ".".join(parts[i:])
            if cand in hs:
                return cand
        return None


def hash_token(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


class Bridge:
    def __init__(self, settings, on_enqueue, get_status, on_classify=None, on_node=None):
        """
        on_enqueue(ticket, url, height)：收到「加入下載」時呼叫（在連線服務的執行緒裡，要自己轉回主畫面）
        get_status(ticket)：回傳 {"status":…, "progress":…, "title":…} 或 None
        """
        self.settings = settings
        self.on_enqueue = on_enqueue
        self.get_status = get_status
        self.on_classify = on_classify             # AI 判斷網域（可以沒有）
        self.on_node = on_node                     # V3.1：瀏覽器「交給節點」（可以沒有）
        self.server = None
        self.port = None
        self.pair_code = None
        self.pair_expires = 0
        self.pair_fails = 0
        self.pair_burned = False                    # 被猜錯太多次而作廢
        self.recent = []
        self.lock = threading.Lock()

    # ---- 開關 ----

    def start(self):
        if self.server:
            return self.port
        handler = self._make_handler()
        for port in BRIDGE_PORTS:
            try:
                srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
            except OSError:
                continue                                 # 這個埠被占用 → 試下一個
            srv.daemon_threads = True
            self.server, self.port = srv, port
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            log.info("瀏覽器連動服務啟動：127.0.0.1:%d", port)
            return port
        log.warning("瀏覽器連動服務無法啟動：%s 都被占用", BRIDGE_PORTS)
        return None

    def stop(self):
        if self.server:
            try:
                self.server.shutdown()
                self.server.server_close()
            except Exception:
                pass
            log.info("瀏覽器連動服務關閉")
        self.server, self.port = None, None

    # ---- 配對 ----

    def new_pair_code(self):
        with self.lock:
            self.pair_code = f"{secrets.randbelow(10 ** 6):06d}"
            self.pair_expires = time.time() + PAIR_SECONDS
            self.pair_fails = 0
            self.pair_burned = False
            return self.pair_code

    def try_pair(self, code, ua=""):
        with self.lock:
            if self.pair_burned:
                return None, "猜錯太多次，配對碼已作廢，請在電腦版重新按「配對」"
            if not self.pair_code or time.time() > self.pair_expires:
                return None, "配對碼已過期，請在電腦版重新按「配對」"
            if not hmac.compare_digest(str(code).strip(), self.pair_code):
                self.pair_fails += 1
                if self.pair_fails >= PAIR_MAX_FAILS:
                    self.pair_code = None
                    self.pair_burned = True
                    return None, "猜錯太多次，配對碼已作廢，請在電腦版重新按「配對」"
                return None, "配對碼不對"
            self.pair_code = None                       # 一個配對碼只能用一次
            token = secrets.token_urlsafe(32)
            tokens = list(self.settings["bridge_tokens"] or [])
            tokens.append(hash_token(token))
            self.settings["bridge_tokens"] = tokens[-10:]   # 最多記 10 個瀏覽器
            meta = {h: m for h, m in (self.settings["bridge_meta"] or {}).items() if h in self.settings["bridge_tokens"]}
            meta[hash_token(token)] = {"paired": time.time(), "last": 0, "uses": 0, "ua": browser_name(ua)}
            self.settings["bridge_meta"] = meta
            self.settings.save()
            log.info("瀏覽器配對成功（目前 %d 個）", len(self.settings["bridge_tokens"]))
            return token, None

    def token_ok(self, token):
        if not token:
            return False
        h = hash_token(token)
        ok = any(hmac.compare_digest(h, x) for x in (self.settings["bridge_tokens"] or []))
        if ok:                                           # 記「最後一次使用」（給「🔒 安全」分頁看；一分鐘最多存一次檔）
            meta = dict(self.settings["bridge_meta"] or {})
            m = dict(meta.get(h) or {"paired": 0, "ua": "（舊版配對）"})
            m["uses"] = m.get("uses", 0) + 1
            save = time.time() - m.get("last", 0) > 60
            m["last"] = time.time()
            meta[h] = m
            self.settings["bridge_meta"] = meta
            if save:
                self.settings.save()
        return ok

    def unpair(self, h):
        """取消單一個配對"""
        self.settings["bridge_tokens"] = [x for x in (self.settings["bridge_tokens"] or []) if x != h]
        self.settings["bridge_meta"] = {k: v for k, v in (self.settings["bridge_meta"] or {}).items() if k != h}
        self.settings.save()

    def unpair_all(self):
        self.settings["bridge_tokens"] = []
        self.settings["bridge_meta"] = {}
        self.settings.save()

    def rate_ok(self):
        now = time.time()
        with self.lock:
            self.recent = [t for t in self.recent if now - t < 60]
            if len(self.recent) >= RATE_PER_MIN:
                return False
            self.recent.append(now)
            return True

    # ---- 處理請求 ----

    def _make_handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def send(self, code, obj):
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def allowed(self):
                host = (self.headers.get("Host") or "").split(":")[0].lower()
                if host not in ("127.0.0.1", "localhost"):
                    return False                         # 防 DNS rebinding
                origin = self.headers.get("Origin")
                if origin and not origin.startswith(("chrome-extension://", "moz-extension://", "safari-web-extension://")):
                    return False                         # 一般網頁送來的 → 拒絕
                if not origin and self.headers.get("X-VH") != "1":
                    return False                         # 沒 Origin 也沒自訂標頭（網頁的 no-cors 請求）→ 拒絕，網站就測不出你有裝電腦版
                return True

            def body(self):
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    return None
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    return None
                if n <= 0 or n > 10_000:
                    return None
                try:
                    return json.loads(self.rfile.read(n).decode("utf-8"))
                except Exception:
                    return None

            def do_GET(self):
                if not self.allowed():
                    return self.send(403, {"error": "forbidden"})
                path, _, qs = self.path.partition("?")
                if path == "/ping":                 # 能力協商：告訴腳本「我是哪一版、會做哪些事、收哪些網站」
                    feats = (["enqueue", "media", "status"] + (["classify"] if bridge.on_classify else [])
                             + (["node"] if bridge.on_node else []))
                    return self.send(200, {"app": "VideoHelper", "version": VERSION, "api": BRIDGE_API,
                                           "features": feats, "sites": list(BRIDGE_HOSTS)})
                if path == "/status":
                    if not bridge.token_ok(self.headers.get("X-VH-Token")):
                        return self.send(401, {"error": "需要重新配對"})
                    ticket = urllib.parse.parse_qs(qs).get("ticket", [""])[0]
                    st = bridge.get_status(ticket)
                    return self.send(200, st) if st else self.send(404, {"error": "找不到這個任務"})
                self.send(404, {"error": "not found"})

            def do_POST(self):
                if not self.allowed():
                    return self.send(403, {"error": "forbidden"})
                data = self.body()
                if not isinstance(data, dict):
                    return self.send(400, {"error": "格式不對"})
                if self.path == "/pair":
                    token, err = bridge.try_pair(data.get("code", ""), self.headers.get("User-Agent", ""))
                    return self.send(200, {"token": token}) if token else self.send(403, {"error": err})
                if self.path == "/enqueue":
                    if not bridge.token_ok(self.headers.get("X-VH-Token")):
                        return self.send(401, {"error": "需要重新配對"})
                    url = data.get("url", "")
                    if not bridge_url_allowed(url):
                        return self.send(400, {"error": "只接受 Pornhub、XVideos、XNXX、YouPorn、RedTube、xHamster、Tube8、YouJizz、SpankBang 的影片網址"})
                    if not bridge.rate_ok():
                        return self.send(429, {"error": "太頻繁了，等一分鐘再試"})
                    h = data.get("height")
                    h = int(h) if isinstance(h, (int, float)) or (isinstance(h, str) and h.isdigit()) else None
                    media = data.get("media") or ""
                    if media:
                        if not (isinstance(media, str) and len(media) < 4000
                                and bridge_url_allowed(media, BRIDGE_MEDIA_HOSTS)):
                            return self.send(400, {"error": "影片位址不是這個網站的影片伺服器"})
                        media = {"url": media, "referer": url,
                                 "title": str(data.get("title") or "")[:300]}
                    ticket = secrets.token_hex(8)
                    bridge.on_enqueue(ticket, url, h, media or None)
                    log.info("瀏覽器送來：%s（%s）", log_url(url), quality_label(h) if h else "預設畫質")
                    return self.send(200, {"ok": True, "ticket": ticket})
                if self.path == "/node":            # V3.1：交給節點下載（瀏覽器把影片真正的位址一起給）
                    if not bridge.token_ok(self.headers.get("X-VH-Token")):
                        return self.send(401, {"error": "需要重新配對"})
                    if not bridge.on_node:
                        return self.send(404, {"error": "not found"})
                    url, media = data.get("url", ""), data.get("media", "")
                    if not bridge_url_allowed(url):
                        return self.send(400, {"error": "只接受支援下載的 9 個網站的影片網址"})
                    if not (isinstance(media, str) and media.startswith("https://") and len(media) < 4000
                            and bridge_url_allowed(media, BRIDGE_MEDIA_HOSTS)):
                        return self.send(400, {"error": "影片位址不是這個網站的影片伺服器（要 https）"})
                    if not bridge.rate_ok():
                        return self.send(429, {"error": "太頻繁了，等一分鐘再試"})
                    try:
                        task_id = bridge.on_node(url, media, str(data.get("title") or "")[:200])
                    except ValueError as e:              # 內容不合格（例如影片位址跟網頁不是同一個網站）
                        return self.send(400, {"error": str(e)})
                    except RuntimeError as e:            # Master 沒開
                        return self.send(409, {"error": str(e)})
                    log.info("瀏覽器交給節點：%s", log_url(url))
                    return self.send(200, {"ok": True, "task_id": task_id})
                if self.path == "/classify":
                    if not bridge.token_ok(self.headers.get("X-VH-Token")):
                        return self.send(401, {"error": "需要重新配對"})
                    if not bridge.on_classify:
                        return self.send(404, {"error": "not found"})
                    doms = data.get("domains")
                    if not isinstance(doms, list):
                        return self.send(400, {"error": "格式不對"})
                    doms = [str(d).lower().strip() for d in doms][:AI_MAX_DOMAINS]
                    doms = [d for d in dict.fromkeys(doms) if DOMAIN_RE.match(d)]
                    if not doms:
                        return self.send(400, {"error": "沒有可以判斷的網域"})
                    if not bridge.rate_ok():
                        return self.send(429, {"error": "太頻繁了，等一分鐘再試"})
                    site = str(data.get("site") or "")[:40]
                    # 每個網域實際連到的完整網址（例如 pemsrv.com → s.pemsrv.com），拿來比對清單
                    raw = data.get("hosts") if isinstance(data.get("hosts"), dict) else {}
                    hosts = {}
                    for d in doms:
                        hl = raw.get(d) if isinstance(raw.get(d), list) else []
                        hl = [str(h).lower().strip() for h in hl][:20]
                        hosts[d] = [h for h in dict.fromkeys(hl) if DOMAIN_RE.match(h) and (h == d or h.endswith("." + d))]
                    try:
                        return self.send(200, {"results": bridge.on_classify(site, doms, hosts)})
                    except RuntimeError as e:
                        return self.send(503, {"error": str(e)})
                self.send(404, {"error": "not found"})

        return Handler


# ======================================================================
# 小工具
# ======================================================================

def find_chrome():
    """找出電腦上 Chrome 的位置，找不到回傳 None"""
    cands = []
    for env in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA"):
        base = os.environ.get(env)
        if base:
            cands.append(os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"))
    cands += ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
    return next((c for c in cands if os.path.exists(c)), None)


def fmt_size(b):
    if not b:
        return "大小未知"
    return f"{b / 1e9:.2f} GB" if b >= 1e9 else f"{b / 1e6:.0f} MB"


def fmt_time(sec):
    if sec is None or sec < 0:
        return "時間未知"
    if sec < 60:
        return f"{sec:.0f} 秒"
    if sec < 3600:
        return f"{int(sec // 60)} 分 {int(sec % 60)} 秒"
    return f"{int(sec // 3600)} 時 {int(sec % 3600 // 60)} 分"


def fmt_duration(sec):
    """影片長度：1:05:20 或 12:30"""
    if not sec:
        return "長度未知"
    sec = int(sec)
    h, m, s = sec // 3600, sec % 3600 // 60, sec % 60
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def quality_label(h):
    return f"{h}p" if h else "原始畫質"


def pick_default(formats, default_quality):
    """依「預設畫質」選一個：不超過預設的最高畫質；都比預設高就選最低的"""
    if not formats:
        return 0
    if default_quality == "最高":
        return 0
    target = int(default_quality)
    for i, f in enumerate(formats):              # formats 已經是高到低排好
        if f["height"] <= target:
            return i
    return len(formats) - 1


def speed_test(url, headers, nbytes=3_000_000, timeout=10):
    """下載影片開頭約 3 MB 來量網速（Mbps），失敗回傳 0"""
    try:
        req = urllib.request.Request(url, headers={**headers, "Range": f"bytes=0-{nbytes - 1}"})
        t0 = time.time()
        got = 0
        with urllib.request.urlopen(req, timeout=timeout) as r:
            while got < nbytes:
                chunk = r.read(65536)
                if not chunk:
                    break
                got += len(chunk)
        dt = time.time() - t0
        return got * 8 / dt / 1e6 if dt > 0 and got > 100_000 else 0
    except Exception:
        return 0


HLS_MAX_BYTES = 2 << 20      # 清單最多讀 2 MB（正常的清單遠小於這個；不會把怪檔案整個吃進記憶體）


def first_segment_url(m3u8_url, headers, depth=0):
    """從切片清單找出第一個小片段的網址（拿來測速）；測試：.github/hls_test.py
    畫質總表 → 跳過只有聲音的項目、往下一層（最多 2 層，再多就放棄、不會把清單網址當片段）"""
    try:
        req = urllib.request.Request(m3u8_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read(HLS_MAX_BYTES).decode("utf-8", "ignore")
    except Exception:
        return None
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if any(l.startswith("#EXT-X-STREAM-INF") for l in lines):
        if depth >= 2:
            return None
        variants = []
        for i, l in enumerate(lines[:-1]):
            if l.startswith("#EXT-X-STREAM-INF") and not lines[i + 1].startswith("#"):
                variants.append(("RESOLUTION=" in l, lines[i + 1]))
        pick = next((u for has_video, u in variants if has_video), variants[0][1] if variants else None)
        return first_segment_url(urllib.parse.urljoin(m3u8_url, pick), headers, depth + 1) if pick else None
    uris = [l for l in lines if not l.startswith("#")]
    return urllib.parse.urljoin(m3u8_url, uris[0]) if uris else None


def check_update(q, manual=False):
    """問 GitHub 有沒有比現在更新的版本（連同更新內容）；manual=True 時沒有新版也會回報"""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": "VideoHelper"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
        tag = (data.get("tag_name") or "").lstrip("v")
        ver = lambda v: tuple(int(x) for x in v.split("."))
        if tag and ver(tag) > ver(VERSION):
            q.put(("update", (tag, data.get("html_url"), (data.get("body") or "").strip())))
        elif manual:
            q.put(("update_none", tag))
    except Exception as e:
        if manual:
            q.put(("update_failed", str(e)))


def _gh_json(url, timeout=20):
    """問 GitHub API（只有 api.github.com）；回 (狀態碼, 資料)。CI 可以用 VH_GITHUB_TOKEN 避免共用 IP 被限流"""
    h = {"User-Agent": f"VideoHelper/{VERSION}", "Accept": "application/vnd.github+json"}
    if os.environ.get("VH_GITHUB_TOKEN"):
        h["Authorization"] = "Bearer " + os.environ["VH_GITHUB_TOKEN"]
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=h), timeout=timeout) as r:
            return r.status, json.load(r)
    except urllib.error.HTTPError as e:
        return e.code, None


def _attest_info(att):
    """從簽章紀錄裡讀出是哪個流程、哪次打包（讀不到就算了，只是顯示用）"""
    try:
        import base64
        env = (att.get("bundle") or {}).get("dsseEnvelope") or {}
        st = json.loads(base64.b64decode(env.get("payload") or ""))
        pred = st.get("predicate") or {}
        wf = ((pred.get("buildDefinition") or {}).get("externalParameters") or {}).get("workflow") or {}
        run = ((pred.get("runDetails") or {}).get("metadata") or {}).get("invocationId") or ""
        return {"type": st.get("predicateType", ""), "workflow": wf.get("path", ""), "repo": wf.get("repository", ""), "run": run}
    except Exception:
        return {}


def verify_self(exe=None, gh=None):
    """v1.43.0「🔍 檢查這個 EXE 是不是官方打包的」（docs/security/01-一鍵驗證與whisper指紋規格.md）：
    ① 算自己的 SHA-256 → ② GitHub 有沒有本專案對這個指紋的打包簽章紀錄（attestation）→ ③ 跟這一版發布頁的指紋比對。
    只送出 SHA-256（公開資訊），不是完整的密碼學驗證（那要用 gh attestation verify）"""
    gh = gh or _gh_json
    exe = exe or (sys.executable if getattr(sys, "frozen", False) else None)
    if not exe:
        return {"state": "not_exe", "text": "這個功能只檢查打包好的 VideoHelper.exe（現在是直接跑 Python 原始碼）"}
    sha = file_sha256(exe)
    r = {"sha256": sha, "version": VERSION}
    code, data = gh(f"https://api.github.com/repos/{REPO}/attestations/sha256:{sha}")
    if code in (403, 429):
        return dict(r, state="error", text="GitHub 暫時限制查詢次數，請過一陣子再試")
    if code not in (200, 404) or (code == 200 and not isinstance(data, dict)):
        return dict(r, state="error", text=f"連不到 GitHub（{code}），請稍後再試")
    atts = (data or {}).get("attestations") or []
    infos = [_attest_info(a) for a in atts]
    prov = [i for i in infos if "provenance" in i.get("type", "") or "slsa" in i.get("type", "")]
    r["attestations"] = len(atts)
    if prov:
        r["run"] = prov[0].get("run", "")
        r["workflow"] = prov[0].get("workflow", "")
    code2, rel = gh(f"https://api.github.com/repos/{REPO}/releases/tags/v{VERSION}")
    asset = next((a for a in (rel or {}).get("assets", []) if a.get("name") == "VideoHelper.exe"), None) if code2 == 200 else None
    r["release_sha256"] = ((asset or {}).get("digest") or "").removeprefix("sha256:")
    if not atts:
        return dict(r, state="bad", text="❌ GitHub 查不到這個 EXE 的打包紀錄：可能被改過，或不是從官方發布頁下載的。建議重新從發布頁下載。")
    if r["release_sha256"] and r["release_sha256"] != sha:
        return dict(r, state="warn", text=f"⚠ 有打包紀錄，但跟 v{VERSION} 發布頁上的指紋不一樣（可能是同版本重新打包、或發布頁被動過）")
    return dict(r, state="ok", text="✅ 這個 EXE 是 GitHub Actions 從 dadanny1122/video-helper 原始碼打包的（GitHub 有簽章紀錄"
                + ("，也跟發布頁的指紋一樣" if r["release_sha256"] else "；這一版的發布頁還查不到") + "）")


# ======================================================================
# 讀取影片資訊
# ======================================================================

def collect_formats(info):
    """把 yt-dlp 給的格式整理成「每個畫質一個選項」"""
    dur = info.get("duration") or 0
    best = {}
    # 只有一個檔案的影片（例如直接的 .mp4 連結），yt-dlp 不會給 formats 清單
    formats = info.get("formats") or ([info] if info.get("url") else [])
    for f in formats:
        if f.get("vcodec") == "none":               # 只有聲音
            continue
        h = f.get("height") or 0                    # 沒寫高度 → 當成「原始畫質」
        size = f.get("filesize") or f.get("filesize_approx") or 0
        if not size and f.get("tbr") and dur:
            size = f["tbr"] * 1000 / 8 * dur
        proto = f.get("protocol") or ""
        cand = {
            "id": f["format_id"],
            "height": h,
            "size": size,
            "exact": bool(f.get("filesize")),
            "audio": f.get("acodec") != "none",
            "direct": proto.startswith("http") and "m3u8" not in proto,
            "url": f.get("url"),
            "headers": f.get("http_headers") or {},
        }
        old = best.get(h)
        score = lambda c: (c["audio"], c["direct"], c["size"])
        if old is None or score(cand) > score(old):
            best[h] = cand
    return sorted(best.values(), key=lambda c: -c["height"])


def analyze_url(url):
    """讀取影片資訊並量網速，回傳 (標題, 長度秒數, 畫質清單, 網速)"""
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
        info = ydl.extract_info(url, download=False)
    fmts = collect_formats(info)
    mbps = 0
    for f in fmts:                       # 先用 MP4 直連測速
        if f["direct"] and f["url"]:
            mbps = speed_test(f["url"], f["headers"])
            if mbps:
                break
    if not mbps:                         # 沒有直連 → 下載第一個切片測速
        for f in fmts:
            if not f["direct"] and f["url"]:
                seg = first_segment_url(f["url"], f["headers"])
                if seg:
                    mbps = speed_test(seg, f["headers"])
                    if mbps:
                        break
    return info.get("title") or "video", info.get("duration") or 0, fmts, mbps


def media_headers(referer):
    """跟瀏覽器一樣的請求標頭：有些影片伺服器會檢查是從哪個網頁來的"""
    return {"User-Agent": BROWSER_UA, "Referer": referer}


def analyze_media(media, height=None):
    """
    瀏覽器送來影片真正的位址（MP4 或 m3u8）時用這個：直接讀影片伺服器，不開網頁。
    media：{"url", "referer", "title"}；回傳跟 analyze_url 一樣
    """
    hdrs = media_headers(media["referer"])
    with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True,
                           "http_headers": hdrs}) as ydl:
        info = ydl.extract_info(media["url"], download=False)
    fmts = collect_formats(info)
    for f in fmts:
        if not f["height"] and height:          # 直接的 MP4 不知道高度 → 用網頁上那一行的畫質
            f["height"] = int(height)
        f["media"] = media                      # 下載時也走同一個位址
    mbps = 0
    for f in fmts:
        if f["url"]:
            u = f["url"] if f["direct"] else first_segment_url(f["url"], hdrs)
            mbps = speed_test(u, hdrs) if u else 0
            if mbps:
                break
    return media.get("title") or info.get("title") or "video", info.get("duration") or 0, fmts, mbps


# ======================================================================
# 下載（可取消）
# ======================================================================

# ======================================================================
# 自動化核心：錯誤分類器＋自適應重試
#   失敗時先弄清楚「是哪一種錯」，再決定要不要自動重試、等多久：
#   ・429（太頻繁）／5xx（伺服器掛了）／斷線逾時 → 自動重試：等的時間一次比一次久（指數退避）
#     並加一點隨機（抖動），避免大家同時重打；429 有 Retry-After 就照網站說的等
#   ・403／404／網址錯 → 重試也沒用，標成「需要處理」，不盲目重打
#   ・硬碟滿了 → 整個佇列暫停、保留已完成的，等使用者清出空間
#   每個決定都寫進 decisions.jsonl（只記網站名稱，不記網址）
# ======================================================================
RETRY_MAX = 3                 # 可重試的錯誤最多自動重試幾次
RETRY_CAP = 900               # 最長等 15 分鐘

ERROR_RULES = (
    # (代號, 比對規則, 顯示名稱, 能不能自動重試, 基本等待秒數, 給使用者的提示)
    ("disk_full", r"no space left|errno 28|空間不足|disk full",
     "硬碟空間不足", False, 0, "清出空間後按「▶ 繼續」"),
    ("http_429", r"\b429\b|too many requests",
     "請求太頻繁（429）", True, 90, ""),
    ("http_403", r"\b40[13]\b|\b410\b|forbidden|login required|需要登入",
     "網站拒絕存取（403）", False, 0, "可能是地區限制、要登入，或網站擋下載；稍後或換個網路再試"),
    ("http_404", r"\b404\b|not found|video unavailable|content unavailable|has been removed|private video|已被移除|影片不存在",
     "影片不存在（404）", False, 0, "影片可能被刪除或設為私人，確認網頁還開得開嗎"),
    ("invalid_url", r"unsupported url|is not a valid url|不支援的網站|找不到影片",
     "網址有問題", False, 0, "確認網址是支援的網站、開得出影片頁"),
    ("http_5xx", r"\b50[0-4]\b|internal server error|bad gateway|service unavailable|gateway timeout",
     "網站伺服器出錯（5xx）", True, 45, ""),
    ("timeout", r"timed? ?out|逾時|timeout",
     "連線逾時", True, 25, ""),
    ("network", r"connection (reset|refused|aborted)|getaddrinfo|name resolution|network is unreachable|"
                r"remote end closed|incomplete ?read|eof occurred|ssl|連不上",
     "網路不穩", True, 25, ""),
    ("ffmpeg", r"ffmpeg|轉檔",
     "影片處理失敗", False, 0, "原始檔已保留；把錯誤日誌回報給作者"),
)


def classify_error(msg):
    """錯誤訊息 → {kind, label, retry, base, hint}；認不出來的保守重試 1 次"""
    low = str(msg).lower()
    for kind, pat, label, retry, base, hint in ERROR_RULES:
        if re.search(pat, low):
            return {"kind": kind, "label": label, "retry": retry, "base": base, "hint": hint,
                    "max": RETRY_MAX}
    return {"kind": "unknown", "label": "不明錯誤", "retry": True, "base": 30, "hint": "", "max": 1}


def retry_wait_seconds(info, tries, msg=""):
    """第 tries 次重試要等幾秒：Retry-After 優先，否則 基本秒數 × 2^(次數-1) × 抖動"""
    import random
    m = re.search(r"retry[- ]after[:\s]+(\d+)", str(msg).lower())
    if info["kind"] == "http_429" and m:
        return min(RETRY_CAP, max(5, int(m.group(1)))) + random.uniform(0, 5)
    return min(RETRY_CAP, info["base"] * 2 ** (max(0, tries - 1)) * random.uniform(0.8, 1.2))


class NetHealth:
    """
    整體網路健康：連續好幾個任務都因為網路掛掉 → 暫緩開始新任務（冷卻），成功一次就歸零
    v1.24.0：有給網站名稱時，要「兩個以上不同網站」最近 10 分鐘都出錯，才算整體網路有問題；
    只有一個網站出錯 → 交給 HOSTS 只冷卻那個網站，其他網站照常下載
    """
    BAD_KINDS = ("timeout", "network", "http_5xx")
    NET_KINDS = ("timeout", "network")             # 5xx 是網站自己壞掉，不算整體網路
    WINDOW = 600

    def __init__(self):
        self.fails, self.hold_until, self.recent = 0, 0.0, []

    def ok(self):
        self.fails, self.hold_until, self.recent = 0, 0.0, []

    def bad(self, kind, site=None):
        if kind not in self.BAD_KINDS:
            return 0
        import random
        now = time.time()
        if site is not None:
            if kind not in self.NET_KINDS:
                return 0
            self.recent = [(t, s) for t, s in self.recent if now - t < self.WINDOW] + [(now, site)]
            if len({s for _, s in self.recent}) < 2:
                return 0
        self.fails += 1
        if self.fails < 3:
            return 0
        hold = min(600, 60 * 2 ** (self.fails - 3)) * random.uniform(0.8, 1.2)
        self.hold_until = now + hold
        return hold

    def holding(self):
        return max(0, self.hold_until - time.time())


class HostHealth:
    """
    各網站自己的健康：429（太頻繁）照網站說的時間冷卻；5xx／逾時／斷線連續 3 次 → 冷卻那個網站
    冷卻中的網站，後面排隊的任務先跳過，先下載其他網站的
    """

    def __init__(self):
        self.h = {}                                 # 網站 → {"fails", "until", "why"}

    def _get(self, site):
        return self.h.setdefault(site, {"fails": 0, "until": 0.0, "why": ""})

    def ok(self, site):
        self.h.pop(site, None)

    def bad(self, site, kind, wait=0):
        """回傳這個網站要冷卻幾秒（0＝不用）"""
        import random
        st = self._get(site)
        if kind == "http_429":                      # 網站叫我們慢一點：整個網站都等（不能用多開連線硬衝）
            st["until"], st["why"] = max(st["until"], time.time() + wait), "請求太頻繁（429）"
            return wait
        if kind not in NetHealth.BAD_KINDS:
            return 0
        st["fails"] += 1
        if st["fails"] < 3:
            return 0
        hold = min(600, 60 * 2 ** (st["fails"] - 3)) * random.uniform(0.8, 1.2)
        st["until"], st["why"] = time.time() + hold, "連續出錯"
        return hold

    def cooling(self, site):
        st = self.h.get(site)
        return max(0, st["until"] - time.time()) if st else 0


HOSTS = HostHealth()


NET = NetHealth()


# ======================================================================
# 智慧下載決策（v1.24.0）：看下載過程的實際狀況，決定「下一個任務」分幾段
#   ・分類器回答「發生什麼事」（classify_error）；這裡回答「下一步怎麼做」
#   ・只改下一個任務，不動正在下載的（續傳不會壞）
#   ・速度用 EWMA 平滑：S = αX + (1−α)S，α＝0.25；至少 5 個樣本才看趨勢
#   ・降級：一個任務裡分段出錯 ≥ 2 次 → 降一級；整個任務因逾時／斷線失敗 → 降兩級
#   ・升級：累積 3 個「30 秒都沒出錯」的穩定時段才升一級；剛改過要先等 2 個任務（防來回跳）
#   ・429／403／404 是網站的事，不改分段；網站不支援分段 → 本來就會改一般下載
#   ・模式：auto 自動調整／observe 只觀察（照舊 6 段，只記錄「如果是自動會怎麼做」）／off 關閉
# ======================================================================
SEG_LEVELS = (1, 2, 4, 6)
EWMA_ALPHA = 0.25
TREND_MIN_SAMPLES = 5
SAMPLE_EVERY = 3              # 每 3 秒取一個速度樣本
STABLE_WINDOW = 30            # 30 秒一個觀察時段
SEG_DOWN_ERRORS = 2
SEG_UP_WINDOWS = 3
SEG_HOLD_TASKS = 2
SMART_MODES = {"只觀察（記錄建議，照舊 6 段）": "observe", "自動調整": "auto", "關閉（固定 6 段）": "off"}


class Telemetry:
    """一個任務的下載觀察值：速度樣本、EWMA、分段出錯／逾時次數、穩定時段"""

    def __init__(self, parts=0):
        self.parts, self.samples, self.ewma = parts, 0, None
        self.seg_errors, self.seg_timeouts, self.http_status, self.no_range = 0, 0, None, False
        self.stable_windows, self._t, self._w, self._werr = 0, 0.0, None, 0
        self.lock = threading.Lock()

    def speed(self, bps, now=None):
        """收一個速度（位元組／秒）；太密就跳過"""
        now = now or time.time()
        if bps is None or now - self._t < SAMPLE_EVERY:
            return
        with self.lock:
            self._t = now
            x = bps * 8 / 1e6
            self.ewma = x if self.ewma is None else EWMA_ALPHA * x + (1 - EWMA_ALPHA) * self.ewma
            self.samples += 1
            if self._w is None:                     # 第一個樣本：開始第一個時段
                self._w = now
            elif now - self._w >= STABLE_WINDOW:      # 一個時段結束：這 30 秒都沒出錯就算穩定
                if self.seg_errors == self._werr:
                    self.stable_windows += 1
                self._w, self._werr = now, self.seg_errors

    def error(self, e):
        with self.lock:
            self.seg_errors += 1
            if re.search(r"timed? ?out|逾時", str(e).lower()):
                self.seg_timeouts += 1
            code = getattr(e, "code", None)
            if isinstance(code, int):
                self.http_status = code

    def summary(self):
        return {"parts": self.parts, "samples": self.samples,
                "speed_ewma_mbps": round(self.ewma, 2) if self.ewma is not None else None,
                "seg_errors": self.seg_errors, "seg_timeouts": self.seg_timeouts,
                "http_status": self.http_status, "no_range": self.no_range, "stable_windows": self.stable_windows}


class SegmentController:
    """每個網站一份分段狀態；回傳下一個任務該分幾段"""

    def __init__(self, state=None):
        self.state = state if state is not None else {}

    def _st(self, site):
        st = self.state.get(site)
        if not isinstance(st, dict) or st.get("level") not in range(len(SEG_LEVELS)):
            st = self.state[site] = {"level": len(SEG_LEVELS) - 1, "stable": 0, "hold": 0}
        return st

    def suggest(self, site):
        return SEG_LEVELS[self._st(site)["level"]]

    def learn(self, site, tele, kind=None):
        """
        任務結束時呼叫。tele：Telemetry.summary()；kind：失敗的錯誤種類（成功＝None）
        回傳 (decision, reason, before, after)；decision＝keep／reduce／increase
        """
        st = self._st(site)
        before = st["level"]
        if st["hold"] > 0:
            st["hold"] -= 1
        decision, reason = "keep", "stable"
        if kind in ("http_429", "http_403", "http_404", "invalid_url", "disk_full"):
            reason = "server_or_local"              # 網站限流／拒絕、本機問題：不是分段的錯，不改
        elif tele.get("no_range"):
            reason = "no_range"
        elif kind in ("timeout", "network"):
            st["level"], st["stable"], decision, reason = max(0, before - 2), 0, "reduce", "task_failed_" + kind
        elif tele.get("seg_errors", 0) >= SEG_DOWN_ERRORS:
            st["level"], st["stable"] = max(0, before - 1), 0
            decision, reason = "reduce", "repeated_timeout" if tele.get("seg_timeouts", 0) >= SEG_DOWN_ERRORS else "segment_errors"
        elif tele.get("seg_errors", 0) == 0 and tele.get("samples", 0) >= TREND_MIN_SAMPLES:
            st["stable"] += tele.get("stable_windows", 0)
            if st["stable"] >= SEG_UP_WINDOWS and before < len(SEG_LEVELS) - 1 and st["hold"] == 0:
                st["level"], st["stable"], decision, reason = before + 1, 0, "increase", "stable_windows"
        elif tele.get("samples", 0) < TREND_MIN_SAMPLES:
            reason = "few_samples"
        if st["level"] == before and decision != "keep":
            decision = "keep"                       # 已經在最低／最高了
        if st["level"] != before:
            st["hold"] = SEG_HOLD_TASKS
        return decision, reason, SEG_LEVELS[before], SEG_LEVELS[st["level"]]


def smart_report(path=None, days=30):
    """讀 decisions.jsonl，比較「固定分段」和「自動調整」：成功率、平均速度、平均重試、調整次數"""
    path = path or os.path.join(data_dir(), "decisions.jsonl")
    try:
        rows = [json.loads(x) for x in open(path, encoding="utf-8").read().splitlines() if x.strip()]
    except Exception:
        rows = []
    since = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    rows = [r for r in rows if str(r.get("time", "")) >= since]
    res = [r for r in rows if r.get("action") == "task_result"]
    out = [f"最近 {days} 天：完成或失敗的任務 {len(res)} 個"]
    for mode, name in (("fixed", "固定 6 段（基準）"), ("auto", "自動調整")):
        g = [r for r in res if r.get("mode") == mode]
        if not g:
            out.append(f"・{name}：還沒有資料")
            continue
        ok = [r for r in g if r.get("result") == "success"]
        sp = [r["mbps"] for r in ok if r.get("mbps")]
        out.append(f"・{name}：{len(g)} 個，成功 {len(ok) / len(g):.0%}，平均速度 "
                   + (f"{sum(sp) / len(sp):.1f} Mbps" if sp else "—")
                   + f"，平均重試 {sum(r.get('tries', 0) for r in g) / len(g):.2f} 次")
    seg = [r for r in rows if r.get("action") == "segments" and r.get("decision") != "keep"]
    auto = [r for r in seg if r.get("applied")]
    out.append(f"・分段調整：實際調整 {len(auto)} 次，只觀察的建議 {len(seg) - len(auto)} 次")
    for r in seg[-5:]:
        out.append(f"　{r['time'][5:16]} {r.get('site')}：{r.get('segment_count_before')} → "
                   f"{r.get('segment_count_after')} 段（{SEG_REASONS.get(r.get('reason'), r.get('reason'))}）"
                   + ("" if r.get("applied") else "［只觀察］"))
    cool = [r for r in rows if r.get("action") == "host_cooldown"]
    if cool:
        out.append(f"・網站冷卻 {len(cool)} 次（只停那個網站，其他網站照常下載）")
    out.append("\n兩種模式各有 10 個以上任務、在差不多的網路下比較，才看得出自動調整有沒有比較好。")
    return "\n".join(out)


SEG_REASONS = {"repeated_timeout": "分段一直逾時", "segment_errors": "分段一直出錯", "stable_windows": "連續穩定",
               "task_failed_timeout": "整個任務逾時", "task_failed_network": "整個任務斷線"}


def decision_log(**ev):
    """自動決策的結構化紀錄（JSON，一行一筆；只記網站名稱，不記網址）"""
    try:
        p = os.path.join(data_dir(), "decisions.jsonl")
        ev = {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), **ev}
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        if os.path.getsize(p) > 400_000:               # 太大就留後半
            lines = open(p, encoding="utf-8").read().splitlines()[-1500:]
            open(p, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    except Exception:
        pass


class Paused(Exception):
    """按了「暫停」：停下來但保留已下載的部分，之後可以接著下載"""


class Throttle:
    """下載速度上限（所有下載共用）：每秒最多放行 bps 位元組，超過就稍等一下"""

    def __init__(self):
        self.bps, self.allow, self.t = 0.0, 0.0, time.time()
        self.lock = threading.Lock()

    def set_mbps(self, mbps):
        self.bps = max(0.0, float(mbps or 0)) * 1e6 / 8
        self.allow, self.t = 0.0, time.time()

    def wait(self, n):
        if not self.bps:
            return
        with self.lock:
            now = time.time()
            self.allow = min(self.bps, self.allow + (now - self.t) * self.bps)   # 最多存 1 秒的額度
            self.t = now
            self.allow -= n
            delay = -self.allow / self.bps if self.allow < 0 else 0
        if delay > 0:
            time.sleep(min(delay, 5))


THROTTLE = Throttle()


class Cancelled(Exception):
    pass


def cleanup_partials(paths, include_complete=False):
    """
    刪掉留下的暫存檔（.part、.ytdl、片段暫存檔），回傳刪了幾個。
    include_complete=True（取消時）：連已經下載完的檔案也一起刪
    """
    removed = 0
    for p in set(paths):
        if not p:
            continue
        cands = (glob.glob(glob.escape(p) + ".part*") + glob.glob(glob.escape(p) + ".ytdl") +
                 glob.glob(glob.escape(p) + "-Frag*"))
        if include_complete:
            cands.append(p)
        for f in set(cands):
            try:
                if os.path.isfile(f):
                    os.remove(f)
                    removed += 1
            except Exception:
                log.exception("刪除殘檔失敗：%s", f)
    return removed


PARALLEL_PARTS = 6
PARALLEL_MIN_BYTES = 8_000_000           # 太小的檔案不值得分段


def parallel_ok(info):
    """yt-dlp 選好的是「一個完整的 http 檔案」（不是切片、不用合併影音）才分段下載"""
    return (bool(info.get("url")) and not info.get("requested_formats")
            and (info.get("protocol") or "") in ("http", "https"))


def _probe_size(url, headers):
    """問伺服器檔案多大、能不能分段；不能就回傳 0"""
    req = urllib.request.Request(url, headers={**headers, "Range": "bytes=0-0"})
    with urllib.request.urlopen(req, timeout=15) as r:
        cr = r.headers.get("Content-Range") or ""
        if r.status != 206 or "/" not in cr:
            return 0
        total = cr.rsplit("/", 1)[1].strip()
        return int(total) if total.isdigit() else 0


def parallel_download(url, headers, dest, parts=PARALLEL_PARTS, on_progress=None, cancel_event=None,
                      touched=None, keep_partials=None, tele=None):
    """
    把一個檔案切成幾段同時下載，再接回去（內容跟一條線下載的一模一樣）。
    成功回傳 {"bytes", "start", "end"}；伺服器不支援分段就回傳 None（改用一般下載）。
    取消會丟出 Cancelled；某一段一直失敗會丟出例外。
    """
    if parts <= 1:                                   # 智慧分段決定只用 1 段 → 直接用一般下載
        return None
    try:
        total = _probe_size(url, headers)
    except Exception as e:
        log.info("分段下載：問不到檔案大小，改用一般下載（%s）", e)
        if tele is not None:
            tele.no_range = True
        return None
    if total < PARALLEL_MIN_BYTES:
        if tele is not None and not total:
            tele.no_range = True
        return None
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    step = -(-total // parts)
    ranges = [(i, i * step, min(total, (i + 1) * step) - 1) for i in range(parts) if i * step < total]
    got = [0] * len(ranges)
    # 接著下載（暫停過）：分段的大小和段數一樣，才沿用上次已經下載的部分
    info_path = dest + ".parts.json"
    if touched is not None:
        touched.add(info_path)
    try:
        prev = json.load(open(info_path, encoding="utf-8"))
    except Exception:
        prev = None
    if prev == {"total": total, "parts": len(ranges)}:
        for i, a, b in ranges:
            p = f"{dest}.part{i}"
            if os.path.isfile(p):
                got[i] = min(os.path.getsize(p), b - a + 1)
        if sum(got):
            log.info("分段下載：接著上次的 %.1f MB 繼續", sum(got) / 1e6)
    else:
        for i, _, _ in ranges:
            try:
                os.remove(f"{dest}.part{i}")
            except OSError:
                pass
    json.dump({"total": total, "parts": len(ranges)}, open(info_path, "w", encoding="utf-8"))
    resumed = sum(got)
    lock = threading.Lock()
    errors = []
    start = time.time()
    last = {"t": 0, "st": start, "sd": resumed}

    def report():
        now = time.time()
        if tele is not None and now - last["st"] >= SAMPLE_EVERY:      # 這 3 秒的速度（不是平均）
            tele.speed((sum(got) - last["sd"]) / (now - last["st"]), now)
            last["st"], last["sd"] = now, sum(got)
        if not on_progress or now - last["t"] < 0.25:
            return
        last["t"] = now
        done = sum(got)
        speed = (done - resumed) / max(0.01, now - start)
        on_progress(done, total, speed, (total - done) / speed if speed else None)

    def worker(i, a, b):
        part = f"{dest}.part{i}"
        if touched is not None:
            touched.add(part)
        tries = 0
        with open(part, "ab" if got[i] else "wb") as f:
            f.truncate(got[i])
            while a + got[i] <= b:
                if cancel_event is not None and cancel_event.is_set():
                    return
                if errors:
                    return
                try:
                    req = urllib.request.Request(url, headers={**headers, "Range": f"bytes={a + got[i]}-{b}"})
                    with urllib.request.urlopen(req, timeout=30) as r:
                        if r.status != 206:
                            raise IOError(f"伺服器沒有照分段回應（{r.status}）")
                        while True:
                            if cancel_event is not None and cancel_event.is_set():
                                return
                            chunk = r.read(256 * 1024)
                            if not chunk:
                                break
                            f.write(chunk)
                            with lock:
                                got[i] += len(chunk)
                                report()
                            THROTTLE.wait(len(chunk))
                    tries = 0
                except Exception as e:
                    tries += 1
                    if tele is not None:
                        tele.error(e)
                    if tries > 8:
                        errors.append(e)
                        return
                    time.sleep(min(10, tries * 1.5))

    threads = [threading.Thread(target=worker, args=r, daemon=True) for r in ranges]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    parts_files = [f"{dest}.part{i}" for i, _, _ in ranges]
    if cancel_event is not None and cancel_event.is_set():
        if keep_partials and keep_partials():
            raise Paused()
        raise Cancelled()
    if errors or sum(got) != total:
        for p in parts_files:
            try:
                os.remove(p)
            except OSError:
                pass
        raise errors[0] if errors else IOError(f"下載不完整（{sum(got)}/{total}）")
    end = time.time()
    tmp = dest + ".joining"
    with open(tmp, "wb") as out:
        for p in parts_files:
            with open(p, "rb") as f:
                while True:
                    buf = f.read(4 * 1024 * 1024)
                    if not buf:
                        break
                    out.write(buf)
    os.replace(tmp, dest)
    for p in parts_files + [info_path]:
        try:
            os.remove(p)
        except OSError:
            pass
    if on_progress:
        on_progress(total, total, (total - resumed) / max(0.01, end - start), 0)
    log.info("分段下載完成：%d 段，%.1f MB，%.1f 秒", len(ranges), total / 1e6, end - start)
    return {"bytes": total, "start": start, "end": end}


def run_download(url, fmt, out_dir, on_progress=None, on_stage=None, cancel_event=None,
                 name_template=DEFAULT_TEMPLATE, stats=None, keep_partials=None):
    """
    下載一個影片，成功回傳 (檔案路徑, 警告)；警告平常是 None。
    fmt：collect_formats() 回傳的其中一個畫質
    cancel_event：設成 set() 就會停止，並丟出 Cancelled
    name_template：FILENAME_TEMPLATES 裡的名字
    stats：傳一個 dict 進來，下載完會填上實際的 bytes（總大小）和 seconds（下載花的秒數，不含轉檔）
    keep_partials：停下來時呼叫；回傳 True（暫停）就保留已下載的部分、丟出 Paused，下次接著下載
    """
    touched = set()          # 這次下載碰過的檔案（取消時要清掉）
    tele = None              # 下面設定好再換成 Telemetry（hook 先看到 None 沒關係）
    final = {"path": None}
    sizes = {}               # 每個檔案下載了多少（影像和聲音可能是分開的兩個檔）
    clock = {"start": None, "end": None}

    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        fn = d.get("filename")
        if fn:
            touched.add(fn)
        if d.get("tmpfilename"):
            touched.add(d["tmpfilename"])
        if clock["start"] is None:
            clock["start"] = time.time()
        if fn:
            got = d.get("total_bytes") if d["status"] == "finished" else d.get("downloaded_bytes")
            if got:
                sizes[fn] = max(sizes.get(fn, 0), got)
        if d["status"] == "downloading" and tele is not None:
            tele.speed(d.get("speed"))
        if d["status"] == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            on_progress(d.get("downloaded_bytes", 0), total, d.get("speed"), d.get("eta"))
        elif d["status"] == "finished":
            clock["end"] = time.time()
            if fn and fn not in sizes and os.path.isfile(fn):
                sizes[fn] = os.path.getsize(fn)
            final["path"] = fn
            if on_stage:
                on_stage("下載完成，處理中（轉成 MP4）…")

    def pp_hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        if d.get("status") == "finished":
            p = (d.get("info_dict") or {}).get("filepath")
            if p:
                final["path"] = p

    fmt_sel = fmt["id"] if fmt["audio"] else f"{fmt['id']}+bestaudio/{fmt['id']}"
    opts = {
        "format": fmt_sel,
        "outtmpl": os.path.join(out_dir, build_outtmpl(name_template)),
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "progress_hooks": [hook],
        "postprocessor_hooks": [pp_hook],
        "concurrent_fragment_downloads": 6,
        "continuedl": True,                     # 有暫停留下的 .part 就接著下載
        "retries": 10,
        "fragment_retries": 10,
        "merge_output_format": "mp4",
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]
    if THROTTLE.bps:
        opts["ratelimit"] = THROTTLE.bps                    # 速度上限（yt-dlp 自己下載的部分）
    parts = int((stats or {}).get("parts") or PARALLEL_PARTS)   # 智慧分段決定的段數（App 放進 stats）
    tele = Telemetry(parts)
    opts["concurrent_fragment_downloads"] = parts            # 切片影片：同時抓幾片也照這個
    if stats is not None:
        stats["touched"] = touched                          # 暫停的話，App 要知道留下哪些檔案
        stats["tele"] = tele

    media = fmt.get("media")
    if media:
        opts["http_headers"] = media_headers(media["referer"])
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            if media:
                # 直接下載影片伺服器上的檔案，檔名用網頁上的標題（不然會變成 index-v1-a1 之類）
                raw = ydl.extract_info(media["url"], download=False, process=False)
                if media.get("title"):
                    raw["title"] = media["title"]
                raw["extractor_key"] = {"pornhub.com": "PornHub", "xvideos.com": "XVideos", "xnxx.com": "XNXX",
                                        "youporn.com": "YouPorn", "redtube.com": "RedTube", "xhamster.com": "xHamster",
                                        "tube8.com": "Tube8", "youjizz.com": "YouJizz", "spankbang.com": "SpankBang"}.get(
                    site_of(url), site_of(url))         # 「網站 - 標題」的檔名格式要用
                if fmt.get("height"):                   # 影片伺服器沒寫高度 → 檔名一樣加上 [1080p]
                    for fm in raw.get("formats") or [raw]:
                        if not fm.get("height"):
                            fm["height"] = fmt["height"]
                info = ydl.process_ie_result(raw, download=False)
            else:
                info = ydl.extract_info(url, download=False)
            # 單一個 MP4 檔：自己分 6 段同時下載（網站常限制每條連線的速度）
            done = None
            if parallel_ok(info):
                path = ydl.prepare_filename(info)
                touched.add(path)
                done = parallel_download(info["url"], {**(info.get("http_headers") or {})}, path, parts=parts,
                                         on_progress=on_progress, cancel_event=cancel_event,
                                         touched=touched, keep_partials=keep_partials, tele=tele)
                if done:
                    clock["start"], clock["end"] = done["start"], done["end"]
                    sizes[path] = done["bytes"]
                    final["path"] = path
            if not done:
                ydl.process_ie_result(info, download=True)
    except BaseException as e:
        # yt-dlp 會把進度回呼裡丟出的例外包起來，所以也要檢查 cancel_event
        cancelled = isinstance(e, (Cancelled, Paused)) or (cancel_event is not None and cancel_event.is_set())
        if cancelled and keep_partials and keep_partials():
            log.info("已暫停：%s（保留已下載的部分）", log_url(url))
            raise Paused() from None
        if cancelled:
            n = cleanup_partials(touched, include_complete=True)
            log.info("已取消：%s（清掉 %d 個殘檔）", log_url(url), n)
            raise Cancelled() from None
        # 影片已經下載完，只是最後的修正／轉檔失敗 → 保留檔案，不要讓使用者白等
        if "Postprocessing" in str(e) and final["path"] and os.path.isfile(final["path"]):
            cleanup_partials(touched)
            if stats is not None and clock["start"] and clock["end"]:
                stats.update(bytes=sum(sizes.values()), seconds=max(0.01, clock["end"] - clock["start"]))
            log.warning("下載完成但轉檔失敗，保留原檔：%s（%s）", log_url(url), e)
            return final["path"], "下載完成，但轉檔失敗；檔案還是可以播放（建議用 VLC）"
        n = cleanup_partials(touched)
        log.exception("下載失敗：%s（%s，清掉 %d 個暫存檔）", log_url(url), quality_label(fmt.get("height")), n)
        raise
    if cancel_event is not None and cancel_event.is_set() and not (keep_partials and keep_partials()):
        cleanup_partials(touched, include_complete=True)
        raise Cancelled()
    if stats is not None and clock["start"] and clock["end"]:
        stats.update(bytes=sum(sizes.values()), seconds=max(0.01, clock["end"] - clock["start"]))
    log.info("下載完成：%s", log_url(url))
    return final["path"], None


def open_path(path, select=False):
    """打開資料夾；select=True 時打開資料夾並選取那個檔案"""
    try:
        if sys.platform.startswith("win"):
            if select and os.path.isfile(path):
                subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
            else:
                os.startfile(path if os.path.isdir(path) else os.path.dirname(path))
        elif sys.platform == "darwin":
            subprocess.run(["open", "-R", path] if select else ["open", path])
        else:
            subprocess.run(["xdg-open", path if os.path.isdir(path) else os.path.dirname(path)])
    except Exception:
        log.exception("打開檔案位置失敗：%s", path)


def play_file(f):
    """用電腦預設的播放器打開影片（open_path 給檔案只會打開它所在的資料夾）"""
    try:
        if sys.platform.startswith("win"):
            os.startfile(f)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", f])
        else:
            subprocess.Popen(["xdg-open", f])
    except Exception:
        log.exception("播放失敗：%s", f)


# ======================================================================
# 視窗介面
# ======================================================================

import tkinter as tk                                   # noqa: E402
from tkinter import ttk, messagebox, filedialog        # noqa: E402


# ======================================================================
# 外觀（v1.25.0）：Windows 11 的 Sun Valley（Fluent）主題＋深色標題列＋品牌色
#   三個介面（電腦版、即時介面、腳本面板）共用同一組品牌色：青藍 #57C8FF → 紫 #8B7CFF
# ======================================================================
THEMES = {"跟系統": "auto", "深色": "dark", "淺色": "light"}
PALETTE = {
    "dark": {"bg": "#1c1c1c", "surface": "#2b2b2b", "text": "#f3f3f3", "muted": "#a0a7b4", "accent": "#57c8ff",
             "accent2": "#8b7cff", "ok": "#3ddc97", "bad": "#ff6b6b", "warn": "#ffb547", "line": "#3a3a3a",
             "head1": "#0f2a44", "head2": "#2c1f5c"},
    "light": {"bg": "#fafafa", "surface": "#ffffff", "text": "#1b1b1b", "muted": "#5f6773", "accent": "#005fb8",
              "accent2": "#6a4cff", "ok": "#1e8e5a", "bad": "#c62828", "warn": "#b26a00", "line": "#e5e5e5",
              "head1": "#1d6fd1", "head2": "#6a4cff"},
}
C = dict(PALETTE["light"])        # 目前的顏色（apply_theme 會換）；介面用 C["muted"] 這種寫法


def system_dark():
    """Windows「應用程式模式」是不是深色"""
    if sys.platform != "win32":
        return False
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize") as k:
            return winreg.QueryValueEx(k, "AppsUseLightTheme")[0] == 0
    except Exception:
        return False


def dark_titlebar(win, dark):
    """Windows 10/11：標題列跟著變深色（不支援的系統就算了）"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        win.update_idletasks()
        hwnd = ctypes.windll.user32.GetParent(win.winfo_id()) or win.winfo_id()
        v = ctypes.c_int(1 if dark else 0)
        for attr in (20, 19):                       # 20＝新版 Win10/11，19＝舊版 Win10
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) == 0:
                break
    except Exception:
        pass


_SCALED_FONTS = set()


def apply_theme(root, mode="auto"):
    """套用主題；回傳實際用的 dark／light"""
    from tkinter import font as tkfont
    m = ("dark" if system_dark() else "light") if mode == "auto" else ("dark" if mode == "dark" else "light")
    C.clear()
    C.update(PALETTE[m])
    style = ttk.Style(root)
    try:
        import sv_ttk
        sv_ttk.set_theme(m, root)
        # 主題的文字／底色是靠 <<ThemeChanged>> 事件套上去的，有些 Windows 上事件沒生效 →
        # 底色變預設淺灰、字變黑，分頁和表格標題黑字壓在深底上看不到。所以自己再套一次
        try:
            root.tk.call("configure_colors")
        except tk.TclError:
            pass
        style.configure(".", background=C["bg"], foreground=C["text"], fieldbackground=C["bg"])
        root.tk_setPalette(background=C["bg"], foreground=C["text"])
    except Exception as e:                          # 沒有主題檔就用系統內建的
        log.warning("Sun Valley 主題載入失敗，改用內建：%s", e)
        for t in ("vista", "clam"):
            if t in style.theme_names():
                style.theme_use(t)
                break
    fams = set(tkfont.families(root))
    fam = next((f for f in ("Microsoft JhengHei UI", "Microsoft JhengHei", "Noto Sans CJK TC", "PingFang TC") if f in fams), None)
    for name in tkfont.names(root):
        try:
            f = tkfont.nametofont(name, root)
            if fam:
                f.configure(family=fam)
            size = int(f.cget("size"))
            # 主題的字體用「像素」設大小（負數），Windows 螢幕縮放時不會自己放大 → 照縮放比例放大一次
            if size < 0 and UI_S != 1 and name not in _SCALED_FONTS:
                f.configure(size=int(round(size * UI_S)))
                _SCALED_FONTS.add(name)
        except Exception:
            pass
    style.configure("Muted.TLabel", foreground=C["muted"])
    style.configure("Ok.TLabel", foreground=C["ok"])
    style.configure("Bad.TLabel", foreground=C["bad"])
    style.configure("Accent.TLabel", foreground=C["accent"])
    style.configure("H1.TLabel", font=(fam or "", 15, "bold"))
    style.configure("H2.TLabel", font=(fam or "", 11, "bold"))
    style.configure("Treeview", rowheight=px(30))
    style.layout("Hidden.TNotebook.Tab", [])          # 主視窗的分頁籤藏起來，改用上面的選單（每次換主題都要重設）
    style.configure("Treeview.Heading", font=(fam or "", 10, "bold"))
    root.configure(background=C["bg"])
    dark_titlebar(root, m == "dark")
    if not getattr(root, "_vh_tb_hook", False):     # 之後打開的小視窗也換深色標題列
        root.bind_class("Toplevel", "<Map>", lambda e: (dark_titlebar(e.widget, C["bg"] == PALETTE["dark"]["bg"]),
                                                        fade_in(e.widget)) if isinstance(e.widget, tk.Toplevel) else None, add="+")
        root.bind_class("Toplevel", "<Configure>", mark_ui_busy, add="+")
        root.bind("<Configure>", mark_ui_busy, add="+")
        root._vh_tb_hook = True
    return m


UI_S = 1.0     # 螢幕縮放（Windows 設 150% → 1.5）；自己畫的元件用「100% 的座標」畫，最後再乘上它


def px(n):
    """100% 時的像素 → 現在螢幕縮放下的像素"""
    return int(round(n * UI_S))


def enable_dpi_awareness():
    """
    告訴 Windows「我自己會處理高解析度螢幕」。不說的話，螢幕縮放 125%／150% 時，
    Windows 會把整個視窗當圖片放大 → 中文字糊糊的，而且放大也拖慢畫面
    要在建立視窗（tk.Tk()）之前呼叫
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)     # 系統 DPI：Tk 8.6 支援得最好
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def mix(a, b, t):
    """兩個顏色中間的顏色（t＝0 是 a、1 是 b）；用來做淡入淡出"""
    a, b = int(a[1:], 16), int(b[1:], 16)
    t = max(0.0, min(1.0, t))
    return "#%02x%02x%02x" % tuple(int(((a >> k) & 255) * (1 - t) + ((b >> k) & 255) * t) for k in (16, 8, 0))


def ease_out(t):
    return 1 - (1 - max(0.0, min(1.0, t))) ** 3


def round_rect(cv, x0, y0, x1, y1, r, **kw):
    pts = [x0 + r, y0, x1 - r, y0, x1, y0, x1, y0 + r, x1, y1 - r, x1, y1, x1 - r, y1, x0 + r, y1, x0, y1, x0, y1 - r, x0, y0 + r, x0, y0]
    return cv.create_polygon(pts, smooth=True, **kw)


def fade_in(win, ms=220):
    """視窗慢慢浮現（Windows 支援透明度；不支援的系統就直接出現）"""
    if getattr(win, "_vh_faded", False):
        return
    win._vh_faded = True
    try:
        win.attributes("-alpha", 0.0)
    except tk.TclError:
        return
    t0 = time.time()

    def step():
        t = (time.time() - t0) * 1000 / ms
        try:
            win.attributes("-alpha", ease_out(t))
        except tk.TclError:
            return
        if t < 1:
            win.after(15, step)
    win.after(10, step)

    def done():
        if win.winfo_exists():
            win.attributes("-alpha", 1.0)      # 保險：一定會完全出現
            unlayer(win)
    win.after(ms + 400, done)


def unlayer(win):
    """v1.33.2：淡入用的透明度會把視窗變成 Windows 的「分層視窗」（layered），之後一直都是——
    分層視窗每次移動、放大都要整張重新合成，大視窗（進階設定）會卡、放大時一瞬間灰色／黑色。
    淡入結束就把這個屬性拿掉，變回一般視窗（主視窗本來就沒淡入，見 v1.28.2）"""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        u = ctypes.windll.user32
        u.GetParent.argtypes = [ctypes.c_void_p]
        u.GetParent.restype = ctypes.c_void_p
        u.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
        u.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
        GWL_EXSTYLE, WS_EX_LAYERED = -20, 0x80000
        st = u.GetWindowLongW(hwnd, GWL_EXSTYLE)
        if st & WS_EX_LAYERED:
            u.SetWindowLongW(hwnd, GWL_EXSTYLE, st & ~WS_EX_LAYERED)
            # SWP_NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED：只是讓 Windows 重新套用樣式
            u.SetWindowPos(hwnd, None, 0, 0, 0, 0, 0x1 | 0x2 | 0x4 | 0x10 | 0x20)
    except Exception:
        pass


def grab_window_client(win):
    """把視窗裡面現在的樣子拍成一張圖。v1.34.1 改快：直接 BitBlt 視窗自己的畫面（不再用 PrintWindow 請 Windows 重算一次），
    也不經過 PIL，自己把 BGRA 排成 PPM（Tk 內建就讀得懂）。拍不到（全黑）才退回 PrintWindow。失敗回 None。"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as w
    u, g = ctypes.windll.user32, ctypes.windll.gdi32
    for fn, args, res in ((u.GetParent, [w.HWND], w.HWND), (u.GetClientRect, [w.HWND, ctypes.POINTER(w.RECT)], w.BOOL),
                          (u.GetDC, [w.HWND], w.HDC), (u.ReleaseDC, [w.HWND, w.HDC], ctypes.c_int),
                          (u.PrintWindow, [w.HWND, w.HDC, w.UINT], w.BOOL),
                          (g.BitBlt, [w.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HDC, ctypes.c_int,
                                      ctypes.c_int, w.DWORD], w.BOOL),
                          (g.CreateCompatibleDC, [w.HDC], w.HDC), (g.CreateCompatibleBitmap, [w.HDC, ctypes.c_int, ctypes.c_int], w.HBITMAP),
                          (g.SelectObject, [w.HDC, w.HGDIOBJ], w.HGDIOBJ), (g.DeleteObject, [w.HGDIOBJ], w.BOOL),
                          (g.DeleteDC, [w.HDC], w.BOOL),
                          (g.GetDIBits, [w.HDC, w.HBITMAP, w.UINT, w.UINT, ctypes.c_void_p, ctypes.c_void_p, w.UINT], ctypes.c_int)):
        fn.argtypes, fn.restype = args, res
    hwnd = u.GetParent(win.winfo_id()) or win.winfo_id()
    r = w.RECT()
    u.GetClientRect(hwnd, ctypes.byref(r))
    wd, ht = r.right - r.left, r.bottom - r.top
    if wd <= 0 or ht <= 0:
        return None

    class BIH(ctypes.Structure):
        _fields_ = [("biSize", w.DWORD), ("biWidth", w.LONG), ("biHeight", w.LONG), ("biPlanes", w.WORD),
                    ("biBitCount", w.WORD), ("biCompression", w.DWORD), ("biSizeImage", w.DWORD),
                    ("biXPelsPerMeter", w.LONG), ("biYPelsPerMeter", w.LONG), ("biClrUsed", w.DWORD),
                    ("biClrImportant", w.DWORD)]
    sdc = u.GetDC(hwnd)
    mdc = g.CreateCompatibleDC(sdc)
    bmp = g.CreateCompatibleBitmap(sdc, wd, ht)
    old = g.SelectObject(mdc, bmp)
    buf = ctypes.create_string_buffer(wd * ht * 4)
    try:
        def read():
            g.SelectObject(mdc, old)
            bih = BIH(ctypes.sizeof(BIH), wd, -ht, 1, 32, 0, 0, 0, 0, 0, 0)       # 負的高度＝由上往下
            ok = g.GetDIBits(mdc, bmp, 0, ht, buf, ctypes.byref(bih), 0) == ht
            g.SelectObject(mdc, bmp)
            raw = buf.raw
            return raw if ok and any(raw[::40961]) else None                    # 抽樣看是不是全黑
        g.BitBlt(mdc, 0, 0, wd, ht, sdc, 0, 0, 0x00CC0020)                     # SRCCOPY：複製視窗現在的畫面
        raw = read()
        if raw is None and u.PrintWindow(hwnd, mdc, 0x1 | 0x2):               # 退回：PW_CLIENTONLY | PW_RENDERFULLCONTENT
            raw = read()
        if raw is None:
            return None
        rgb = bytearray(wd * ht * 3)                                           # BGRA → RGB（切片在 C 裡做，很快）
        rgb[0::3], rgb[1::3], rgb[2::3] = raw[2::4], raw[1::4], raw[0::4]
        return tk.PhotoImage(master=win, data=b"P6 %d %d 255\n" % (wd, ht) + bytes(rgb), format="ppm")
    finally:
        g.SelectObject(mdc, old)
        g.DeleteObject(bmp)
        g.DeleteDC(mdc)
        u.ReleaseDC(hwnd, sdc)


# v1.33.2：視窗正在被拖動／改大小時，動畫（跑馬燈、首頁數字）先停一下，把時間讓給 Windows 重畫視窗
UI_BUSY = {"until": 0.0}


def ui_busy() -> bool:
    return time.time() < UI_BUSY["until"]


def mark_ui_busy(e=None):
    if e is None or isinstance(getattr(e, "widget", None), (tk.Tk, tk.Toplevel)):
        if not ui_busy() and UI_BUSY.get("on_start"):
            try:
                UI_BUSY["on_start"](getattr(e, "widget", None))
            except Exception:
                pass
        UI_BUSY["until"] = time.time() + 0.35


def debounced(widget, fn, ms=60):
    """改大小時 <Configure> 一秒來幾十次；只在停下來 60ms 後重畫一次（自己畫的 Canvas 元件用）"""
    def handler(e=None):
        job = getattr(widget, "_vh_redraw", None)
        if job:
            try:
                widget.after_cancel(job)
            except Exception:
                pass
        widget._vh_redraw = widget.after(ms, fn)
    return handler


HOME_CARDS = (   # (圖示, 標題, 說明, 去哪個分頁)
    ("⬇", "下載影片", "貼上網址、選畫質，排隊一部一部下載", 0),
    ("🗂", "下載紀錄", "找回下載過的影片、做 AI 字幕", 1),
    ("⚙", "設定", "下載資料夾、速度上限、智慧分段、外觀", 2),
    ("🛡", "安裝擋廣告", "瀏覽器腳本、和電腦版配對", 3),
)


class Home(tk.Canvas):
    """
    進場首頁：歡迎標題＋四張大卡片，一張一張從下面浮上來（顏色從底色慢慢變出來＝淡入）
    滑鼠移上去卡片會亮起來、往上浮一點；點了卡片 → 卡片淡出 → 進到那個分頁
    """
    DUR, GAP = 480, 90           # 每張卡片動畫多久、下一張晚多久開始（毫秒）

    def __init__(self, master, on_pick, status=lambda: "", top=None, below=None, kpis=None):
        super().__init__(master, highlightthickness=0, bd=0, bg=C["bg"])
        self.on_pick, self.status, self.topw, self.below, self.kpis = on_pick, status, top, below, kpis
        self.top, self.tiles = 0, []
        self.t0, self.hover, self.lift, self.leaving, self.anim = 0.0, None, [0.0] * len(HOME_CARDS), None, None
        self.bind("<Configure>", debounced(self, self.draw))
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)

    def _font(self, size, bold=False):
        """字體只建一次、記起來（每一格動畫都去問系統字體會很慢）"""
        from tkinter import font as tkfont
        if not hasattr(self, "_fonts"):
            self._fonts = {}
        key = (size, bold)
        if key not in self._fonts:
            fam = tkfont.nametofont("TkDefaultFont").actual("family")
            self._fonts[key] = tkfont.Font(self, family=fam, size=size, weight="bold" if bold else "normal")
        return self._fonts[key]

    def show(self):
        self._kpi_t = 0                              # 打開首頁時重新算一次數字
        self.configure(bg=C["bg"])
        self.t0, self.leaving = None, None
        self.lift = [0.0] * len(HOME_CARDS)
        y = sum(max(wd.winfo_height(), wd.winfo_reqheight()) for wd in (self.topw, self.below) if wd is not None)
        self.place(x=0, y=y, relwidth=1, relheight=1, height=-y)
        self.lift_()
        self._run()

    def lift_(self):
        tk.Misc.lift(self)

    def hide(self):
        self.place_forget()
        if self.anim:
            self.after_cancel(self.anim)
            self.anim = None

    def visible(self):
        return bool(self.winfo_manager())

    def _run(self):
        if self.t0 is None:                          # 第一格才開始計時（打開首頁那一下的排版停頓不算）
            self.t0 = time.time()
        self.draw()
        busy = (time.time() - self.t0) * 1000 < 2400 or self.leaving \
            or any(abs(v - (1.0 if i == self.hover else 0.0)) > 0.02 for i, v in enumerate(self.lift))
        if self.leaving and (time.time() - self.leaving[1]) * 1000 > 260:
            idx = self.leaving[0]
            self.hide()
            self.on_pick(idx)
            return
        self.anim = self.after(16, self._run) if busy else None

    def _layout(self):
        w, h = max(self.winfo_width() / UI_S, 400), max(self.winfo_height() / UI_S, 300)
        cols = 2
        cw = min(380, (w - 80) // cols)
        ch = 118
        gx, gy = 22, 18
        total_w = cols * cw + (cols - 1) * gx
        x0 = (w - total_w) // 2
        self.top = max(8, (h - 560) // 2)          # 整塊內容垂直置中
        y0 = self.top + 236
        tw = (total_w - 3 * 14) / 4                 # 數據卡：一排 4 張
        self.tiles = [(x0 + i * (tw + 14), self.top + 124, tw, 88) for i in range(4)]
        return [(x0 + (i % cols) * (cw + gx), y0 + (i // cols) * (ch + gy), cw, ch) for i in range(len(HOME_CARDS))], x0, y0

    def draw(self):
        """用 100% 的座標畫，最後整個乘上螢幕縮放（字體 Windows 會自己放大）"""
        self._draw()
        if UI_S != 1:
            self.scale("all", 0, 0, UI_S, UI_S)

    def _draw(self):
        self.delete("all")
        dark = C["bg"] == PALETTE["dark"]["bg"]
        card = "#262a33" if dark else "#ffffff"
        card_hi = "#2f3542" if dark else "#f2f6ff"
        line = "#363b46" if dark else "#e3e7ee"
        now = (time.time() - (self.t0 or time.time())) * 1000
        out = 0.0
        if self.leaving:
            out = ease_out((time.time() - self.leaving[1]) * 1000 / 240)
        boxes, x0, y0 = self._layout()
        w = max(self.winfo_width() / UI_S, 400)
        # 標題（先淡入）
        a = ease_out(now / 420) * (1 - out)
        top = self.top
        ty = top + 34 + (1 - a) * 12
        self.create_text(w // 2, ty, text="歡迎回來", font=self._font(22, True), fill=mix(C["bg"], C["text"], a))
        ln = ease_out((now - 200) / 500) * (1 - out) * 70      # 標題兩邊的細線（往外長出來）
        for sgn in (-1, 1):
            x0 = w // 2 + sgn * 78
            self.create_line(x0, ty, x0 + sgn * ln, ty, fill=mix(C["bg"], C["accent"], a), width=1)
        typed = "安全・乾淨・穩定 —— 你的影片小幫手"           # 打字機效果：一個字一個字出現
        k = int(max(0, now - 350) / 45)
        cursor = "▍" if k < len(typed) or int(now / 500) % 2 else " "
        self.create_text(w // 2, top + 68, text=typed[:k] + (cursor if not self.leaving else ""),
                         font=self._font(11), fill=mix(C["bg"], C["accent"], a))
        self.create_text(w // 2, top + 94, text=self.status() or "想做什麼？點一張卡片開始",
                         font=self._font(9), fill=mix(C["bg"], C["muted"], ease_out((now - 900) / 400) * (1 - out)))
        # 數據卡（參考 Behance 上的金融儀表板）：大數字從 0 跳上去、今天下載附 7 天迷你長條圖
        if time.time() - getattr(self, "_kpi_t", 0) > 0.9:     # 數字每秒算一次就好，不用每一格都算
            try:
                self._kpi = self.kpis() if self.kpis else []
            except Exception:
                self._kpi = []
            self._kpi_t = time.time()
        kpis = self._kpi
        for i, ((x, y, tw, th), kp) in enumerate(zip(self.tiles, kpis)):
            p = ease_out((now - 250 - i * 70) / 420) * (1 - out)
            cnt = ease_out((now - 300 - i * 70) / 900)                # 數字往上跳的進度
            dy = (1 - p) * 16
            bg = mix(C["bg"], card, p)
            round_rect(self, x, y + dy, x + tw, y + th + dy, 16, fill=bg, outline=mix(C["bg"], line, p), width=1)
            self.create_text(x + 16, y + dy + 20, text=kp["label"], anchor="w", font=self._font(9), fill=mix(bg, C["muted"], p))
            val = kp["fmt"].format(kp["value"] * cnt) if kp["value"] is not None else "—"
            vid = self.create_text(x + 16, y + dy + 54, text=val, anchor="w", font=self._font(20, True), fill=mix(bg, C["text"], p))
            if kp.get("unit"):
                self.create_text(x + 16 + self._font(20, True).measure(val) / UI_S + 5, y + dy + 59, text=kp["unit"], anchor="w", font=self._font(9), fill=mix(bg, C["muted"], p))
            spark = kp.get("spark")
            if spark:                                                  # 迷你長條圖：最近 7 天
                m = max(spark) or 1
                bw, gap = 6, 4
                sx = x + tw - 16 - len(spark) * (bw + gap) + gap
                for j, v in enumerate(spark):
                    hgt = max(2, 34 * v / m * cnt)
                    bx0 = sx + j * (bw + gap)
                    self.create_rectangle(bx0, y + dy + 70 - hgt, bx0 + bw, y + dy + 70, outline="",
                                          fill=mix(bg, mix(C["accent"], C["accent2"], j / 6), p * (0.55 + 0.45 * (j == len(spark) - 1))))
            elif kp.get("dot"):
                self.create_oval(x + tw - 24, y + dy + 16, x + tw - 16, y + dy + 24, outline="", fill=mix(bg, kp["dot"], p))
        for i, ((x, y, cw, ch), (icon, title, desc, _)) in enumerate(zip(boxes, HOME_CARDS)):
            p = ease_out((now - 450 - i * self.GAP) / self.DUR)     # 這張卡片進場進度
            if self.leaving:
                p *= 1 - out
            self.lift[i] += ((1.0 if i == self.hover and not self.leaving else 0.0) - self.lift[i]) * 0.25
            hv = self.lift[i]
            dy = (1 - p) * 34 - hv * 4
            bg = mix(C["bg"], mix(card, card_hi, hv), p)
            round_rect(self, x + 2, y + dy + 6, x + cw + 2, y + ch + dy + 6, 18, fill=mix(C["bg"], "#000000" if dark else "#d5dbe5", p * (0.35 + hv * 0.3)), outline="")
            round_rect(self, x, y + dy, x + cw, y + ch + dy, 18, fill=bg,
                       outline=mix(C["bg"], mix(line, C["accent"], hv), p), width=1.5, tags=(f"c{i}",))
            seg = (cw - 36) / 10                    # 卡片上緣品牌色光帶（10 段就夠順，畫太多段會卡）
            for k in range(10):
                self.create_line(x + 18 + k * seg, y + dy + 1, x + 18 + (k + 1) * seg, y + dy + 1,
                                 fill=mix(bg, mix(C["accent"], C["accent2"], k / 9), p * (0.45 + 0.55 * hv)))
            # 圖示圓
            cx, cy = x + 46, y + dy + ch / 2
            self.create_oval(cx - 26, cy - 26, cx + 26, cy + 26, outline="",
                             fill=mix(bg, mix(C["accent"], C["accent2"], i / 3), p * (0.22 + 0.25 * hv)))
            self.create_text(cx, cy, text=icon, font=self._font(20), fill=mix(bg, C["text"], p))
            self.create_text(x + 88, cy - 14, text=title, anchor="w", font=self._font(14, True), fill=mix(bg, C["text"], p))
            self.create_text(x + 88, cy + 14, text=desc, anchor="w", width=(cw - 108) * UI_S, font=self._font(9), fill=mix(bg, C["muted"], p))
            self.create_text(x + cw - 22, cy - 14, text="›", font=self._font(18, True),
                             fill=mix(bg, C["accent"], p * (0.3 + 0.7 * hv)))
        tip = ease_out((now - 700) / 400) * (1 - out)
        self.create_text(w // 2, boxes[-1][1] + boxes[-1][3] + 44, text="之後按右上角的「首頁」可以回到這裡",
                         font=self._font(9), fill=mix(C["bg"], C["muted"], tip * 0.8))

    def _hit(self, ev):
        for i, (x, y, cw, ch) in enumerate(self._layout()[0]):
            if x <= ev.x / UI_S <= x + cw and y <= ev.y / UI_S <= y + ch:
                return i
        return None

    def _set_hover(self, i):
        if i != self.hover:
            self.hover = i
            self.configure(cursor="hand2" if i is not None else "")
            if not self.anim:
                self._run()

    def _motion(self, ev):
        self._set_hover(self._hit(ev))

    def _click(self, ev):
        i = self._hit(ev)
        if i is not None and not self.leaving:
            self.leaving = (HOME_CARDS[i][3], time.time())
            if not self.anim:
                self._run()


TIPS = ("小技巧：瀏覽器按兩下 Esc＝老闆鍵", "下載佇列按右鍵有更多選項", "③ 設定可以切換深色／淺色",
        "② 下載紀錄可以一次選很多部做 AI 字幕", "全部在你的電腦上跑，影片不會上傳")
NAV_ITEMS = (("首頁", -1), ("下載影片", 0), ("下載紀錄", 1), ("設定", 2), ("擋廣告", 3))   # (名稱, 分頁；-1＝首頁)


def ease_out_circ(t):
    """先快後慢（跟 UX Design Awards 網站的選單動畫一樣的曲線感覺）"""
    t = max(0.0, min(1.0, t))
    return (1 - (1 - t) ** 2) ** 0.5


class Header(tk.Canvas):
    """
    最上面的品牌列：漸層底＋logo＋名稱＋右邊的頂部選單
    選單：滑鼠移上去字變亮、出現淡淡底線；選中的那一項有漸層底線，切換時底線「滑過去」
    """

    def __init__(self, master, **kw):
        super().__init__(master, height=px(68), highlightthickness=0, bd=0, **kw)
        self.on_nav, self.active, self.hover = None, 0, None
        self.bar = None                               # 底線目前位置 (x0, x1)
        self.bar_from = self.bar_to = None
        self.bar_t0, self.anim = 0.0, None
        self.items = []                               # [(x0, x1, idx)]
        self.bind("<Configure>", debounced(self, self.draw))
        self.bind("<Motion>", self._motion)
        self.bind("<Leave>", lambda e: self._set_hover(None))
        self.bind("<Button-1>", self._click)

    @staticmethod
    def _mix(a, b, t):
        return mix(a, b, t)

    def _font(self, size=10, bold=False):
        from tkinter import font as tkfont
        key = (size, bold)
        if not hasattr(self, "_fonts"):
            self._fonts = {}
        if key not in self._fonts:
            fam = tkfont.nametofont("TkDefaultFont").actual("family")
            self._fonts[key] = tkfont.Font(self, family=fam, size=size, weight="bold" if bold else "normal")
        return self._fonts[key]

    def _pill_font(self):
        return self._font(9, True)

    def _layout(self):
        w = max(self.winfo_width() / UI_S, 400)
        if getattr(self, "_widths", None) is None:      # 字寬量一次就好（每格都量會拖慢動畫）
            f = self._font(10, True)
            self._widths = [f.measure(name) / UI_S + 28 for name, _ in NAV_ITEMS]
        widths = self._widths
        x = w - 18 - sum(widths)
        out = []
        for (name, idx), iw in zip(NAV_ITEMS, widths):
            out.append((x, x + iw, idx, name))
            x += iw
        return out

    def set_active(self, idx):
        """切換選中的項目；底線用 0.35 秒滑過去"""
        if idx == self.active and self.bar is not None:
            return
        self.active = idx
        target = next(((x0 + 12, x1 - 12) for x0, x1, i, _ in self._layout() if i == idx), None)
        if target is None:
            return
        if self.bar is None:
            self.bar = target
            self.draw_nav()
            return
        self.bar_from, self.bar_to, self.bar_t0 = self.bar, target, None
        if not self.anim:
            self.anim = self.after_idle(self._tick)

    def _tick(self):
        if self.bar_t0 is None:                      # 第一格：現在才開始計時
            self.bar_t0 = time.time()
        t = (time.time() - self.bar_t0) / 0.35
        e = ease_out_circ(t)
        (a0, a1), (b0, b1) = self.bar_from, self.bar_to
        self.bar = (a0 + (b0 - a0) * e, a1 + (b1 - a1) * e)
        self.anim = self.after(16, self._tick) if t < 1 else None
        self.draw_nav()

    def _hit(self, ev):
        x, y = ev.x / UI_S, ev.y / UI_S
        if 14 <= y <= 56:
            for x0, x1, idx, _ in self._layout():
                if x0 <= x <= x1:
                    return idx
        return None

    def _set_hover(self, idx):
        if idx != self.hover:
            self.hover = idx
            self.configure(cursor="hand2" if idx is not None else "")
            self.draw_nav()

    def _motion(self, ev):
        self._set_hover(self._hit(ev))

    def _click(self, ev):
        idx = self._hit(ev)
        if idx is not None and self.on_nav:
            self.on_nav(idx)

    def draw(self):
        """整個重畫（視窗大小、主題變了才需要）"""
        self.delete("all")
        w, h = int(max(self.winfo_width() / UI_S, 200)), 68
        for x in range(0, w, 8):                     # 橫向漸層
            self.create_rectangle(x, 0, x + 8, h, fill=mix(C["head1"], C["head2"], x / w), width=0)
        self.create_line(0, h - 1, w, h - 1, fill=mix(C["accent"], C["accent2"], .5))
        # logo：圓角方塊＋播放三角形
        for i in range(0, 36, 2):                    # logo 漸層（用小方塊，放大後才不會有縫）
            self.create_rectangle(18 + i, 16, 20 + i, 52, width=0, fill=mix("#57c8ff", "#8b7cff", i / 36))
        round_rect(self, 16, 14, 56, 54, 12, fill="", outline="#ffffff", width=2)
        self.create_polygon(31, 24, 31, 44, 46, 34, fill="#ffffff", outline="")
        self.create_text(70, 25, text="Video Helper", anchor="w", fill="#ffffff", font=self._font(15, True))
        self.create_text(70, 47, text="安全・乾淨・穩定", anchor="w", fill="#c9d6ff", font=self._font(9))
        if UI_S != 1:
            self.scale("all", 0, 0, UI_S, UI_S)
        self.draw_nav()

    def draw_nav(self):
        """只重畫選單（滑過、底線滑動時用；背景漸層不用重畫）"""
        self.delete("nav")
        if not self.on_nav:
            return
        lay = self._layout()
        if not self.anim:                            # 沒在滑動：底線直接對準選中的項目（視窗大小變了也會跟著）
            self.bar = next(((x0 + 12, x1 - 12) for x0, x1, i, _ in lay if i == self.active), None)
        for x0, x1, idx, name in lay:
            on, hv = idx == self.active, idx == self.hover
            self.create_text((x0 + x1) / 2, 34, text=name, font=self._font(10, True), tags=("nav",),
                             fill="#ffffff" if on or hv else "#b9c6e8")
            if hv and not on:                        # 滑過：淡淡的底線
                self.create_line(x0 + 12, 50, x1 - 12, 50, fill="#8ea3d6", width=2, tags=("nav",))
        if self.bar:                                 # 選中：漸層底線（會滑動；6 段漸層）
            b0, b1 = self.bar
            n = (b1 - b0) / 6
            for k in range(6):
                self.create_line(b0 + k * n, 50, b0 + (k + 1) * n, 50, width=px(3), tags=("nav",),
                                 fill=mix("#57c8ff", "#8b7cff", k / 5))
        if UI_S != 1:
            self.scale("nav", 0, 0, UI_S, UI_S)


class Ticker(tk.Canvas):
    """品牌列下面的跑馬燈：一直往左捲，顯示狀態和小技巧（參考 UX Design Awards 網站的公告列）"""
    SPEED = 1                                         # 每次移 1 像素（整數才不會一頓一頓）

    def __init__(self, master):
        super().__init__(master, height=px(26), highlightthickness=0, bd=0)
        self.text, self.x, self.anim = "", 0.0, None
        self.SPEED = max(1, round(UI_S))           # 螢幕放大時每次多移一點，看起來速度一樣
        self.bind("<Configure>", debounced(self, self.draw))

    def set_items(self, items):
        text = "     ✦     ".join(items) + "     ✦     "
        if text != self.text:
            self.text = text
            self._tw = None
            self.draw()
        if not self.anim:
            self._tick()

    def _font(self):
        from tkinter import font as tkfont
        if not hasattr(self, "_f"):
            self._f = tkfont.Font(self, family=tkfont.nametofont("TkDefaultFont").actual("family"), size=9)
        return self._f

    def _text_width(self):
        if getattr(self, "_tw", None) is None:            # 量字寬很花時間：文字沒變就用上次的
            self._tw = self._font().measure(self.text) or 1
        return self._tw

    def _tick(self):
        if not self.winfo_exists():
            return
        if ui_busy():                                      # 視窗正在拖動／改大小：先不動
            self.anim = self.after(100, self._tick)
            return
        self.x -= self.SPEED
        tw = self._text_width()
        if self.x <= -tw:
            self.x += tw
        if self.find_withtag("run") and abs(self.x) > self.SPEED:   # 平常只移動文字（省電）
            self.move("run", -self.SPEED, 0)
        else:                                                      # 繞回開頭時整個重畫
            self.draw()
        self.anim = self.after(25, self._tick)       # 每秒 40 像素

    def draw(self):
        self.delete("all")
        dark = C["bg"] == PALETTE["dark"]["bg"]
        self.configure(bg="#14171e" if dark else "#eef2fa")
        w = max(self.winfo_width(), 200)
        tw = self._text_width()
        x = self.x
        while x < w:
            self.create_text(x, px(13), text=self.text, anchor="w", font=self._font(), tags=("run",),
                             fill=C["muted"])
            x += tw
        self.create_line(0, px(26) - 1, w, px(26) - 1, fill=C["line"])


# ======================================================================
# AI 字幕（全部在自己電腦上跑）：whisper.cpp 聽寫 → .srt；想要中文的話，用 AI 金鑰只把「字幕文字」拿去翻譯
# 第一次用才下載：whisper.cpp 程式（約 5 MB，GitHub 官方發布）＋ 模型（190～550 MB，Hugging Face）
# ======================================================================

WHISPER_ASSET = "whisper-bin-x64.zip"           # 一般電腦（CPU）版
# v1.43.0 寫死版本和指紋（跟 AI 模型一樣）：2026-11-24 用 .github/workflows/whisper-pin.yml 實際下載、核對過 GitHub 的指紋。
# 要換版本：手動跑那個 workflow，把印出來的 JSON 貼到這裡（不再每次問 GitHub「最新版是哪個」）
WHISPER_PIN = {"tag": "b4938", "url": "https://github.com/ggml-org/whisper.cpp/releases/download/b4938/whisper-bin-x64.zip",
               "sha256": "c2a4b60edb11f7e11a9191ffb50929535527d4d91c9903dbe3e554583bbbc63d", "size": 8361840}
SUB_MODELS = {"快（small，約 190 MB）": "small-q5_1", "準（large-v3-turbo，約 550 MB）": "large-v3-turbo-q5_0"}
MODEL_URLS = ("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin",
              "https://hf-mirror.com/ggerganov/whisper.cpp/resolve/main/ggml-{}.bin")
# 模型的官方指紋（Hugging Face 公布的 sha256、大小；2026-11-07 用 GitHub Actions 實際下載核對過）
# 下載後對不上就刪掉、不用 → 檔案被換掉或下載壞掉都擋得住
MODEL_SHA256 = {
    "small-q5_1": ("ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb", 190085487),
    "large-v3-turbo-q5_0": ("394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2", 574041195),
    "tiny": ("be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21", 77691713),   # 自動測試用
    # 人聲偵測（VAD，Silero）：先找出「真的有人在說話」的段落，喘息、音樂、沒人說話的地方不給 Whisper 聽 → 不會幻聽
    "vad": ("29940d98d42b91fbd05ce489f3ecf7c72f0a42f027e4875919a28fb4c04ea2cf", 885098),
}
MODEL_SOURCES = {   # 不在 ggerganov/whisper.cpp 的模型：名稱 → (檔名, 網址們)
    "vad": ("ggml-silero-v5.1.2.bin", ("https://huggingface.co/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin",
                                       "https://hf-mirror.com/ggml-org/whisper-vad/resolve/main/ggml-silero-v5.1.2.bin")),
}


def model_path(name):
    return os.path.join(whisper_dir(), MODEL_SOURCES[name][0] if name in MODEL_SOURCES else f"ggml-{name}.bin")
SUB_LANGS = {"自動偵測": "auto", "日文": "ja", "英文": "en", "韓文": "ko", "中文": "zh"}
SUB_BATCH = 80                                  # 翻譯一次送幾句


def whisper_dir():
    d = os.path.join(data_dir(), "whisper")
    os.makedirs(d, exist_ok=True)
    return d


def find_whisper():
    """找 whisper-cli：環境變數（測試用）→ 下載過的 → 電腦裡本來就有的"""
    env = os.environ.get("VIDEO_HELPER_WHISPER")
    if env:
        return env
    names = ("whisper-cli.exe", "whisper-cli", "main.exe")
    for root, _, files in os.walk(whisper_dir()):
        for n in names:
            if n in files:
                return os.path.join(root, n)
    import shutil
    return shutil.which("whisper-cli")


def fetch_file(url, dest, on_progress=None, cancel_event=None, headers=None):
    """下載到 dest（先寫 .part，完整了才改名，中斷不會留下壞檔）"""
    part = dest + ".part"
    req = urllib.request.Request(url, headers={"User-Agent": f"VideoHelper/{VERSION}", **(headers or {})})
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
        total, got = int(r.headers.get("Content-Length") or 0), 0
        while True:
            if cancel_event is not None and cancel_event.is_set():
                f.close()
                os.remove(part)
                raise Cancelled()
            b = r.read(1 << 20)
            if not b:
                break
            f.write(b)
            got += len(b)
            if on_progress:
                on_progress(got, total)
    if total and got < total:
        os.remove(part)
        raise RuntimeError("下載不完整，請再試一次")
    os.replace(part, dest)
    return dest


def install_whisper(on_stage=None, on_progress=None, cancel_event=None):
    exe = find_whisper()
    if exe:
        return exe
    if not sys.platform.startswith("win"):
        raise RuntimeError("找不到 whisper-cli。Windows 會自動下載；其他系統請先自己安裝 whisper.cpp。")
    if on_stage:
        on_stage("第一次使用：下載 whisper.cpp 聽寫程式…")
    pin = WHISPER_PIN
    z = fetch_file(pin["url"], os.path.join(whisper_dir(), WHISPER_ASSET), on_progress, cancel_event)
    if os.path.getsize(z) != pin["size"] or file_sha256(z) != pin["sha256"]:
        os.remove(z)
        raise RuntimeError("下載的聽寫程式指紋對不上（可能被竄改或下載壞掉），已刪除，請再試一次")
    log.info("whisper.cpp %s 指紋相符（寫死的）：%s", pin["tag"], pin["sha256"][:16])
    import zipfile
    with zipfile.ZipFile(z) as zf:
        zf.extractall(os.path.join(whisper_dir(), pin["tag"]))
    os.remove(z)
    exe = find_whisper()
    if not exe:
        raise RuntimeError("whisper.cpp 解壓縮後找不到 whisper-cli.exe，請回報作者")
    return exe


def file_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def ensure_model(name, on_stage=None, on_progress=None, cancel_event=None):
    if name not in MODEL_SHA256:
        raise RuntimeError(f"不認得的模型：{name}")
    want, size = MODEL_SHA256[name]
    path = model_path(name)
    mark = path + ".ok"                                 # 核對過的記號：大小＋修改時間；檔案一動就重新核對
    if os.path.isfile(path):
        st = os.stat(path)
        stamp = f"{want}:{st.st_size}:{int(st.st_mtime)}"
        try:
            if open(mark, encoding="utf-8").read() == stamp:
                return path
        except OSError:
            pass
        if on_stage:
            on_stage("核對模型指紋…")
        if st.st_size == size and file_sha256(path) == want:
            open(mark, "w", encoding="utf-8").write(stamp)
            return path
        os.remove(path)                                 # 指紋不對＝壞掉或被換掉 → 重新下載
    last = None
    urls = MODEL_SOURCES[name][1] if name in MODEL_SOURCES else [t.format(name) for t in MODEL_URLS]
    for url in urls:
        if on_stage:
            on_stage(f"第一次使用這個模型：下載 {name}（只要下載一次）…")
        try:
            fetch_file(url, path, on_progress, cancel_event)
            if on_stage:
                on_stage("核對模型指紋…")
            if file_sha256(path) != want:
                os.remove(path)
                raise RuntimeError("模型指紋對不上（可能被竄改或下載壞掉），已刪除")
            st = os.stat(path)
            open(mark, "w", encoding="utf-8").write(f"{want}:{st.st_size}:{int(st.st_mtime)}")
            log.info("模型 %s 指紋相符", name)
            return path
        except Cancelled:
            raise
        except Exception as e:
            last = e
            log.warning("模型下載失敗 %s：%s", url, e)
    raise RuntimeError(f"模型下載失敗（{last}）")


def _popen_kw():
    return {"creationflags": 0x08000000} if sys.platform.startswith("win") else {}   # Windows 不要跳出黑色視窗


def extract_audio(video, wav, ffmpeg=None):
    """影片 → 16kHz 單聲道 wav（whisper 只吃這種）"""
    ff = ffmpeg or FFMPEG
    if not ff:
        raise RuntimeError("找不到 ffmpeg")
    p = subprocess.run([ff, "-y", "-v", "error", "-i", video, "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", wav],
                       capture_output=True, text=True, encoding="utf-8", errors="ignore", **_popen_kw())
    if p.returncode != 0 or not os.path.isfile(wav):
        raise RuntimeError("讀不出影片的聲音：" + (p.stderr.strip().splitlines() or ["未知錯誤"])[-1][:200])
    return wav


WHISPER_FALLBACKS = [0]       # 防幻聽選項失敗、退回基本模式的次數（自動測試檢查要是 0）


def _mmss(sec):
    sec = int(sec)
    return f"{sec // 3600}:{sec % 3600 // 60:02d}:{sec % 60:02d}" if sec >= 3600 else f"{sec // 60}:{sec % 60:02d}"


def run_whisper(exe, model, wav, lang, out_base, on_progress=None, cancel_event=None, on_text=None, fast=False,
                vad=None, careful=True):
    """回傳 (srt 路徑, 偵測到的語言)。進度用「聽到第幾秒」算（whisper 每聽完一句就印一行 [時間 --> 時間] 文字）"""
    import re
    import time
    threads = str(max(1, min(8, (os.cpu_count() or 4) - 1)))
    cmd = [exe, "-m", model, "-f", wav, "-l", lang, "-osrt", "-of", out_base, "-t", threads]
    if fast:
        cmd += ["-bs", "1", "-bo", "1"]                  # 只想一種答案（不比較 5 種）：快約 2 倍，準度差一點點
    if careful:                                          # 防幻聽：不讓前一句帶著下一句跑、不輸出「非說話」的記號
        cmd += ["-mc", "0", "-sns"]
    if vad:                                              # 只聽有人說話的段落
        cmd += ["--vad", "--vad-model", vad]
    total = max(1.0, (os.path.getsize(wav) - 44) / 32000)   # 16kHz × 2 位元組 ＝ 每秒 32000
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                         errors="ignore", **_popen_kw())

    def watch():                                         # 很久沒印東西時也能馬上取消
        while p.poll() is None:
            if cancel_event is not None and cancel_event.wait(0.5):
                p.kill()
                return
    if cancel_event is not None:
        threading.Thread(target=watch, daemon=True).start()
    detected, tail, t0 = lang, [], time.time()
    for line in p.stdout:
        tail = (tail + [line.strip()])[-5:]
        m = re.search(r"auto-detected language:\s*([a-z]{2})", line)
        if m:
            detected = m.group(1)
        m = re.match(r"\s*\[(\d+):(\d\d):(\d\d)[.,]\d+ --> (\d+):(\d\d):(\d\d)[.,]\d+\]\s*(.*)", line)
        if m:
            end = int(m.group(4)) * 3600 + int(m.group(5)) * 60 + int(m.group(6))
            if on_progress:
                on_progress(int(min(end, total) / total * 1000), 1000)
            if on_text:
                spent = time.time() - t0
                left = spent / max(end, 1) * max(total - end, 0)
                on_text(f"聽到 {_mmss(end)} / {_mmss(total)}｜大約還要 {max(1, round(left / 60))} 分鐘\n最新：{m.group(7).strip()[:60]}")
    p.wait()
    if cancel_event is not None and cancel_event.is_set():
        raise Cancelled()
    if p.returncode != 0 or not os.path.isfile(out_base + ".srt"):
        if careful or vad:                               # 舊版 whisper.cpp 不認得新選項 → 不加選項再試一次
            WHISPER_FALLBACKS[0] += 1
            log.warning("whisper 加防幻聽選項失敗，改用基本模式：%s", " ".join(tail)[-200:])
            return run_whisper(exe, model, wav, lang, out_base, on_progress, cancel_event, on_text, fast, None, False)
        raise RuntimeError("AI 聽寫失敗：" + " ".join(tail)[-200:])
    return out_base + ".srt", detected


# Whisper 很常見的幻聽（訓練資料裡的影片片尾字幕）：句子裡有這些就刪
HALLU_PHRASES = ("amara.org", "字幕由", "字幕提供", "請不吝點贊", "请不吝点赞", "訂閱轉發", "订阅转发", "打賞支持", "打赏支持",
                 "明鏡與點點", "明镜与点点", "優優獨播", "优优独播", "thanks for watching", "thank you for watching",
                 "please subscribe", "ご視聴ありがとうございました", "チャンネル登録", "시청해주셔서")
HALLU_EXACT = ("謝謝觀看", "谢谢观看", "謝謝大家", "谢谢大家", "中文字幕", "字幕", "you", "thank you", "bye")


def _norm(t):
    return re.sub(r"[\s\W_]+", "", t.lower())


def _tm_join(a, b):
    return a.split(" --> ")[0] + " --> " + b.split(" --> ")[1]


def clean_hallucinations(entries):
    """去掉 Whisper 的幻聽：片尾字幕型的句子、一個詞一直重複、同一句連續重複、某一句佔了大半"""
    from collections import Counter
    exact = {_norm(x) for x in HALLU_EXACT}
    out = []
    for tm, txt in entries:
        t = re.sub(r"(\S{1,6}?)(?:[\s，,、。]*\1){3,}", r"\1\1", txt.strip())   # 「小小小小小小」→「小小」
        n = _norm(t)
        if not n or n in exact or any(p in t.lower() for p in HALLU_PHRASES):
            continue
        if out and _norm(out[-1][1]) == n:                  # 同一句連續出現 → 合併成一條（時間拉長）
            out[-1] = (_tm_join(out[-1][0], tm), out[-1][1])
            continue
        out.append((tm, t))
    if len(out) >= 8:                                       # 某一句佔了 4 成以上 → 幾乎一定是幻聽
        top, cnt = Counter(_norm(t) for _, t in out).most_common(1)[0]
        if cnt / len(out) > 0.4:
            out = [e for e in out if _norm(e[1]) != top]
    return out


def parse_srt(text):
    """→ [(時間那一行, 字幕文字)]"""
    out = []
    for block in text.replace("\r\n", "\n").strip().split("\n\n"):
        lines = block.strip().split("\n")
        i = next((k for k, l in enumerate(lines) if "-->" in l), None)
        if i is None:
            continue
        out.append((lines[i].strip(), " ".join(l.strip() for l in lines[i + 1:]).strip()))
    return out


def write_srt(entries, path):
    with open(path, "w", encoding="utf-8-sig") as f:              # 有 BOM：舊播放器才不會亂碼
        for n, (tm, txt) in enumerate(entries, 1):
            f.write(f"{n}\n{tm}\n{txt}\n\n")
    return path


def write_vtt(entries, path):
    """WebVTT：網頁播放器、手機常用；時間的逗號換成點"""
    with open(path, "w", encoding="utf-8") as f:
        f.write("WEBVTT\n\n")
        for n, (tm, txt) in enumerate(entries, 1):
            f.write(f"{n}\n{tm.replace(',', '.')}\n{txt}\n\n")
    return path


SUB_FORMATS = {"SRT（最通用）": ("srt",), "VTT（網頁、手機）": ("vtt",), "兩種都要": ("srt", "vtt")}


def write_subs(entries, base, fmts=("srt",)):
    """依格式寫出字幕；回傳第一個（主要的）檔案路徑"""
    out = []
    for ext in fmts:
        out.append(write_srt(entries, base + ".srt") if ext == "srt" else write_vtt(entries, base + ".vtt"))
    return out[0]


def bilingual(zh, orig):
    """雙語：上面中文、下面原文（時間用同一份）"""
    return [(tm, f"{z}\n{o}") for (tm, z), (_, o) in zip(zh, orig)]


def has_subtitles(video):
    base = os.path.splitext(video)[0]
    return any(os.path.isfile(base + ext) for ext in (".srt", ".vtt")) or bool(glob.glob(glob.escape(base) + ".*.srt"))


def ai_translate(key, lines, src, timeout=90):
    """一批字幕 → 繁體中文（只送文字，不送影片、聲音）"""
    numbered = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(lines))
    prompt = ("把下面的影片字幕翻成台灣用語的繁體中文，口語、簡短，保留語氣。原文語言：" + src + "。\n"
              "每一行都要翻，行數要完全一樣；聽不清楚的雜音（例如 [音樂]）照樣翻成中文括號。\n"
              '只回 JSON 字串陣列，不要其他文字，例如 ["第一句","第二句"]。\n\n' + numbered)
    body = json.dumps({"model": AI_MODEL, "max_tokens": 8000,
                       "messages": [{"role": "user", "content": prompt}]}).encode("utf-8")
    req = urllib.request.Request(AI_API, data=body, method="POST", headers={
        "x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
            note_ai_usage("translate", data)
    except urllib.error.HTTPError as e:
        raise RuntimeError({401: "API 金鑰不對", 429: "AI 額度用完或問太快"}.get(e.code, f"AI 伺服器錯誤（{e.code}）")) from None
    text = "".join(c.get("text", "") for c in data.get("content", []) if c.get("type") == "text")
    out = [str(x) for x in parse_ai_json(text)]
    if len(out) != len(lines):
        raise ValueError("行數對不上")
    return out


def translate_entries(key, entries, src, on_progress=None, cancel_event=None, translate_fn=None):
    fn = translate_fn or ai_translate
    out = []
    for i in range(0, len(entries), SUB_BATCH):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        chunk = entries[i:i + SUB_BATCH]
        texts = [t for _, t in chunk]
        try:
            zh = fn(key, texts, src)
        except ValueError:                                         # 行數對不上：拆小一點再試一次
            zh = []
            for j in range(0, len(texts), 10):
                try:
                    zh += fn(key, texts[j:j + 10], src)
                except ValueError:
                    zh += texts[j:j + 10]                          # 還是不行就保留原文
        out += [(tm, z) for (tm, _), z in zip(chunk, zh)]
        if on_progress:
            on_progress(min(i + SUB_BATCH, len(entries)), len(entries))
    return out


def make_subtitles(video, lang="auto", model="small-q5_1", key="", on_stage=None, on_progress=None,
                   cancel_event=None, translate_fn=None, on_text=None, fmts=("srt",), dual=False):
    """影片 → 同資料夾的字幕檔。回傳 {"orig": 原文字幕, "zh": 中文字幕或 None, "dual": 雙語或 None, "lang": 語言}
    中文字幕取名「影片名.srt」：VLC、PotPlayer、電影與電視 打開影片會自動載入
    fmts：("srt",)／("vtt",)／("srt", "vtt")；dual：有翻譯時另外做一份「中文＋原文」的雙語字幕"""
    if not os.path.isfile(video):
        raise RuntimeError("找不到影片檔")
    exe = install_whisper(on_stage, on_progress, cancel_event)
    mpath = ensure_model(model, on_stage, on_progress, cancel_event)
    try:
        vad = ensure_model("vad", on_stage, on_progress, cancel_event)
    except Cancelled:
        raise
    except Exception as e:                                  # 下載不到就算了，照樣聽寫（只是比較容易幻聽）
        log.warning("人聲偵測模型下載失敗：%s", e)
        vad = None
    base = os.path.splitext(video)[0]
    tmp = os.path.join(whisper_dir(), f"audio-{os.getpid()}-{threading.get_ident()}")
    try:
        if on_stage:
            on_stage("讀取影片的聲音…")
        extract_audio(video, tmp + ".wav")
        if on_stage:
            on_stage("AI 聽寫中（在你的電腦上跑，影片越長越久）…")
        srt, detected = run_whisper(exe, mpath, tmp + ".wav", lang, tmp, on_progress, cancel_event,
                                    on_text, fast=model.startswith("small"), vad=vad)
        raw = parse_srt(open(srt, encoding="utf-8", errors="ignore").read())
        entries = clean_hallucinations(raw)
        if len(raw) != len(entries):
            log.info("AI 字幕：清掉 %d 條幻聽／重複", len(raw) - len(entries))
    finally:
        for ext in (".wav", ".srt"):
            try:
                os.remove(tmp + ext)
            except OSError:
                pass
    if not entries:
        raise RuntimeError("影片裡幾乎沒有人說話（或只有喘息、音樂），所以沒有做出字幕")
    res = {"lang": detected, "zh": None, "dual": None, "count": len(entries)}
    if detected == "zh":
        res["orig"] = write_subs(entries, base, fmts)
        return res
    res["orig"] = write_subs(entries, f"{base}.{detected}", fmts)
    if key:
        if on_stage:
            on_stage(f"翻成中文（只送字幕文字，共 {len(entries)} 句）…")
        zh = translate_entries(key, entries, detected, on_progress, cancel_event, translate_fn)
        res["zh"] = write_subs(zh, base, fmts)
        if dual:
            res["dual"] = write_subs(bilingual(zh, entries), f"{base}.雙語", fmts)
    return res


def component_report():
    """「🔒 安全」分頁的元件清單：版本＋SHA-256（在背景算）"""
    rows = []

    def add(name, ver, path=None):
        sha = ""
        try:
            if path and os.path.isfile(path):
                sha = file_sha256(path)
        except OSError:
            pass
        rows.append(f"{name:<16}{ver}" + (f"\n{'':<16}{sha}" if sha else ""))
    add("Video Helper", VERSION + ("（EXE）" if getattr(sys, "frozen", False) else "（原始碼執行）"),
        sys.executable if getattr(sys, "frozen", False) else None)
    add("Python", sys.version.split()[0])
    add("yt-dlp", getattr(getattr(yt_dlp, "version", None), "__version__", "?"))
    try:
        import truststore
        add("truststore", getattr(truststore, "__version__", "?"))
    except Exception:
        add("truststore", "沒有")
    if FFMPEG:
        try:
            v = subprocess.run([FFMPEG, "-version"], capture_output=True, text=True, timeout=10, **_popen_kw()).stdout.split()[2]
        except Exception:
            v = "?"
        add("ffmpeg", v, FFMPEG)
    else:
        add("ffmpeg", "沒有")
    exe = find_whisper() if os.path.isdir(os.path.join(data_dir(), "whisper")) or os.environ.get("VIDEO_HELPER_WHISPER") else None
    add("whisper.cpp", os.path.basename(os.path.dirname(os.path.dirname(exe))) if exe else "還沒下載", exe)
    for name, (want, size) in MODEL_SHA256.items():
        p = model_path(name) if os.path.isdir(os.path.join(data_dir(), "whisper")) else ""
        if p and os.path.isfile(p):
            ok = os.path.isfile(p + ".ok") and open(p + ".ok", encoding="utf-8").read().startswith(want)
            rows.append(f"{'模型 ' + name:<16}{'✅ 指紋核對過' if ok else '⚠ 下次使用時會核對'}\n{'':<16}{want}")
    return "\n".join(rows)


class App:
    def __init__(self, root, web=None):
        self.root = root
        self.web = web                 # v1.36.0 新介面（webui.Host）；None＝只有原本的視窗
        self.web_fallback = False      # 新介面打不開 → 改用原本的視窗
        self.notices = []              # 新介面的通知中心（最新的在前）
        self.update_info = None        # 有新版本：(版本, 網址)
        self._web_ask = None           # 新介面要問使用者的事（例如還在下載時關視窗）
        self._web_an = None            # 新介面「新增下載」讀取一個網址的結果（畫質清單）
        self._web_urls = {}            # 新介面紀錄編號 → 網址
        self.q = queue.Queue()
        self.settings = Settings()
        global AI_USAGE_HOOK
        AI_USAGE_HOOK = self.note_ai_usage
        self.history = History()
        self._start_time = time.time()
        self.stats = Stats()
        self.auto_backup = auto_backup_on_upgrade(self.settings, self.history)
        self.tray = None               # 系統匣圖示（v1.35.0 起預設一打開就建立）
        self.tray_status = "沒有在下載"   # 系統匣選單第一行（主執行緒更新，系統匣執行緒只讀）
        self.tray_paused = False
        self.tray_hinted = False       # 「在背景繼續下載」的提示只講一次
        self.quitting = False
        self.formats = []
        self.title = ""
        self.duration = 0
        self.url = ""
        self.mbps = float(self.settings["mbps"] or 0)
        self.speed_src = "上次實測" if self.mbps else ""
        self.analyzing = False         # 正在讀取單一網址
        self.batch_running = False     # 正在讀取多個網址
        self.queue = TaskQueue()
        self.cancel_event = None       # 目前下載的「取消開關」；None = 沒有在下載
        self.stop_mode = "cancel"      # 停下來的原因：cancel（刪掉已下載的）／pause（保留，之後接著下載）
        self.queue_paused = False      # 按了暫停：不自動開始下一個
        THROTTLE.set_mbps(self.settings["rate_limit_mbps"])
        self.worker = None
        self.want_height = None        # 從紀錄重新下載時，想要的畫質
        # 真正做事的函式（測試時可以換成假的）
        self.analyze_fn = analyze_url
        self.analyze_media_fn = analyze_media
        self.download_fn = run_download
        self.live = None                                  # (時間, 位元組／秒)：這一秒的下載速度
        self.settings["seg_state"] = dict(self.settings["seg_state"] or {})   # 不要改到預設值那份
        self.segments = SegmentController(self.settings["seg_state"])   # 直接改設定裡那份，存設定就一起存
        # 瀏覽器連動
        self.remote = {}               # 配對的瀏覽器送來的任務：ticket → 狀態
        self.last_clip = ""
        self.bridge = Bridge(self.settings,
                             on_enqueue=self.remote_register,
                             get_status=self.remote_status,
                             on_classify=self.classify_domains,
                             on_node=self.node_enqueue)
        self.ai_fn = ai_classify
        self.blocklist = BlockList()

        root.title(f"Video Helper v{VERSION}")
        global UI_S
        try:
            UI_S = max(1.0, min(3.0, root.winfo_fpixels("1i") / 96))
        except Exception:
            UI_S = 1.0
        root.geometry(f"{px(900)}x{px(780)}")
        root.minsize(px(760), px(660))
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.theme = apply_theme(root, self.settings["theme"])

        self.header = Header(root)
        self.header.pack(fill="x")
        self.ticker = Ticker(root)
        self.ticker.pack(fill="x")
        self.nb = ttk.Notebook(root, style="Hidden.TNotebook")
        self.nb.pack(fill="both", expand=True, padx=10, pady=(8, 10))
        tabs = {}
        for key, text in (("dl", "① 下載影片"), ("hist", "② 下載紀錄"), ("set", "③ 設定"),
                          ("setup", "④ 安裝擋廣告")):
            tabs[key] = ttk.Frame(self.nb)
            self.nb.add(tabs[key], text=f"  {text}  ")
        self.tab_dl, self.tab_setup = tabs["dl"], tabs["setup"]
        self.build_setup(tabs["setup"])
        self.build_download(tabs["dl"])
        self.build_history(tabs["hist"])
        self.build_settings(tabs["set"])
        self.build_advanced()                 # 進階設定（另一個視窗，平常藏起來）
        first_run = self._first_run = not self.settings["setup_seen"]
        if not self.settings["setup_seen"]:   # 第一次打開：先帶去裝擋廣告
            self.nb.select(self.tab_setup)
            self.settings["setup_seen"] = True
            self.settings.save()

        if self.settings["bridge_enabled"]:
            self.bridge.start()
            self.refresh_bridge_label()
        root.bind("<FocusIn>", lambda e: self.check_clipboard() if e.widget is root else None)

        self.home = Home(root, self.pick_home, self.home_status, top=self.header, below=self.ticker, kpis=self.home_kpis)
        UI_BUSY["on_start"] = self._drag_start             # v1.33.3 拖動測試
        if self.settings["tray_always"]:                    # v1.35.0 系統匣小圖示一打開就在
            root.after(800, self.start_tray)
        root.bind("<Destroy>", lambda e: self._stop_tray() if e.widget is root else None, add="+")
        root.bind("<Leave>", self._preshoot, add="+")       # v1.34.1 往標題列移動時先拍好
        root.bind_all("<Expose>", self._count_expose, add="+")
        self.header.on_nav = self.nav
        self.header.active = self.nb.index("current")
        self.header.draw()
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.home.visible() or self.header.set_active(self.nb.index("current")), add="+")
        # 程式自己切分頁（例如瀏覽器送影片來）→ 首頁讓開；剛出現的 0.5 秒內不算（那是之前排隊的切換事件）
        self.nb.bind("<<NotebookTabChanged>>", lambda e: self.home.visible() and not self.home.leaving
                     and not getattr(self, "_warming", False)
                     and time.time() - (self.home.t0 or time.time()) > 0.5 and self.home.hide(), add="+")
        if self.web:
            self.web_attach()
        elif self.settings["show_home"] and self.settings["setup_seen"] and not first_run:
            root.after(80, self.show_home)
        self.refresh_header()
        self.poll()
        threading.Thread(target=check_update, args=(self.q,), daemon=True).start()
        if WEB_UNAVAILABLE and not self.web and self.settings["ui"] == "web" and not self.settings["ui_fail_shown"]:
            self.settings["ui_fail_shown"] = True                 # 只講一次（沒有 WebView2 的電腦每次都會退回原本的視窗）
            self.settings.save()
            root.after(1500, lambda: messagebox.showinfo("新介面", f"新介面不能用：\n{WEB_UNAVAILABLE}\n\n會一直用原本的視窗。"))
        log.info("啟動 v%s", VERSION)

    # 讓背景工作把結果丟回主畫面
    # ---- v1.33.3 拖動測試：拖動／改大小時記下重畫了哪些元件、程式有沒有被卡住，放開後寫一行到日誌 ----
    def _drag_start(self, win):
        if getattr(self, "_drag", None):
            return
        cur = self.nb.index("current")
        page = "首頁" if self.home.visible() else next((n for n, i in NAV_ITEMS if i == cur), str(cur))
        now = time.time()
        self._drag = {"t0": now, "last": now, "beats": 0, "maxgap": 0.0, "expose": {},
                      "page": page, "win": "主視窗" if win is self.root else "小視窗",
                      "size": (self.root.winfo_width(), self.root.winfo_height()), "freeze": ""}
        self.root.after(15, self._drag_beat)                  # 先排好心跳：拍照出錯也不會卡在「拖動中」
        if win is self.root and page != "首頁" and self.settings["drag_snapshot"]:
            try:
                self._drag_freeze()
            except Exception as e:
                self._drag_unfreeze()
                self._drag["freeze"] = "出錯：" + type(e).__name__

    def _drag_freeze(self):
        """v1.34.0：拖動主視窗時，先拍一張現在畫面的照片蓋上去——Windows 拖動時會一直重畫整個視窗，
        sv-ttk 的按鈕、框框都是圖片，重畫很慢；蓋上照片後只要重畫一張圖（跟首頁一樣順），放開再拿掉"""
        t = time.time()
        pre = getattr(self, "_preshot", None)
        self._preshot = None
        size = (self.root.winfo_width(), self.root.winfo_height())
        if pre and t - pre[0] < 3 and pre[1] == size and pre[3] == self.nb.index("current"):
            img, how = pre[2], "先拍好的"                     # 滑鼠移到標題列時就拍好了，按下去不用等
        else:
            try:
                img = grab_window_client(self.root)
            except Exception as e:
                img = None
                log.info("拖動快照失敗：%s", type(e).__name__)
            how = "現拍"
        if img is None:
            self._drag["freeze"] = "沒拍到"
            return
        c = tk.Canvas(self.root, highlightthickness=0, bd=0)
        c.create_image(0, 0, image=img, anchor="nw")
        c._img = img
        c.place(x=0, y=0, relwidth=1, relheight=1)
        tk.Misc.lift(c)                                   # Canvas.lift() 是「排圖形」，要用 Misc 的才是蓋到最上層
        self._frozen = c
        self._drag["freeze"] = f"蓋照片（{how}，{int((time.time() - t) * 1000)} ms）"

    def _preshoot(self, e):
        """v1.34.1：滑鼠從視窗上緣移出去（往標題列走）→ 先拍好，等一下按住拖動時直接用"""
        try:
            if (e.widget is not self.root or not self.settings["drag_snapshot"] or self.home.visible()
                    or ui_busy() or e.y_root >= self.root.winfo_rooty()):
                return
            t = time.time()
            img = grab_window_client(self.root)
            if img is not None:
                self._preshot = (time.time(), (self.root.winfo_width(), self.root.winfo_height()), img,
                                 self.nb.index("current"))
                self._preshot_ms = int((time.time() - t) * 1000)
        except Exception:
            self._preshot = None

    def _drag_unfreeze(self):
        c = getattr(self, "_frozen", None)
        self._frozen = None
        if c is not None:
            try:
                c.destroy()
            except tk.TclError:
                pass

    def _drag_beat(self):
        d = self._drag
        if not d:
            return
        now = time.time()
        d["maxgap"] = max(d["maxgap"], now - d["last"])
        d["last"] = now
        d["beats"] += 1
        if getattr(self, "_frozen", None) and (self.root.winfo_width(), self.root.winfo_height()) != d["size"]:
            self._drag_unfreeze()                             # 其實是在改大小：照片會不對，馬上拿掉
            d["freeze"] += "→改大小，拿掉"
        if ui_busy():
            self.root.after(15, self._drag_beat)
            return
        self._drag_unfreeze()
        self._drag = None
        dur = now - d["t0"] - 0.35                           # 扣掉放開後等的 0.35 秒
        if dur < 0.3:
            return
        ex = sorted(d["expose"].items(), key=lambda x: -x[1])
        total = sum(d["expose"].values())
        log.info("拖動測試：%s／%s，%.1f 秒｜重畫 %d 次（%s）｜程式每秒能處理 %.0f 次、最久卡 %d ms｜%s",
                 d["win"], d["page"], dur, total, "、".join(f"{k} {v}" for k, v in ex[:6]) or "沒有",
                 d["beats"] / max(now - d["t0"], 0.01), d["maxgap"] * 1000, d["freeze"] or "沒蓋照片")

    def _count_expose(self, e):
        d = getattr(self, "_drag", None)
        if d is not None:
            try:
                k = e.widget.winfo_class()
            except Exception:
                k = "?"
            d["expose"][k] = d["expose"].get(k, 0) + 1

    def poll(self):
        now = time.time()
        gap = now - getattr(self, "_last_poll", now)
        self._last_poll = now
        if gap > 0.5 and not ui_busy():                    # 畫面卡住超過 0.5 秒：記在日誌，方便找原因
            log.warning("畫面卡住約 %d ms（分頁 %s）", gap * 1000 - 100, self.nb.index("current") if hasattr(self, "nb") else "?")
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "on_" + kind)(data)
        except queue.Empty:
            pass
        self._tick = getattr(self, "_tick", 0) + 1
        if self.web and self._tick % 5 == 0:              # 新介面：每 0.5 秒換一份狀態給網頁
            try:
                self.web_publish()
            except Exception:
                log.exception("新介面狀態")
        if self._tick % 10 == 0:                       # 約每 1 秒：把「等重試」時間到的放回佇列
            self.update_tray()
            if not ui_busy():                            # 拖動視窗時先不重畫
                self.refresh_header()
                if self.home.visible() and not self.home.anim:   # 首頁開著：數字每秒更新
                    self.home.draw()
            if not self.queue_paused and self.queue.due_retries():
                self.refresh_queue()
            if not self.queue.running() and (self.queue.next_waiting() or None) is not None:
                self.pump()
        self.root.after(100, self.poll)

    # ------------------------------------------------------------------
    # ① 安裝精靈
    # ------------------------------------------------------------------

    def build_setup(self, parent):
        self.chrome = find_chrome()
        f = ttk.Frame(parent, padding=16)
        f.pack(fill="both", expand=True)

        ttk.Label(f, text="在 Chrome 裝好擋廣告（約 2 分鐘，只要做一次）",
                  font=("", 13, "bold")).pack(anchor="w")
        ttk.Label(f, text="照順序按下面 3 個按鈕，每個都會在 Chrome 打開正確的頁面。",
                  foreground=C["muted"]).pack(anchor="w", pady=(2, 12))

        steps = [
            ("1", "安裝暴力猴（Violentmonkey）",
             "打開後按「加到 Chrome」→「新增擴充功能」", URL_VM_STORE),
            ("2", "打開暴力猴的必要設定",
             "把「允許使用者指令碼」打開；\n有用無痕視窗的話，「在無痕模式下允許」也打開", URL_VM_SETTINGS),
            ("3", "安裝 Video Helper 腳本",
             "打開後按「安裝」，之後有新版本會自動更新", URL_SCRIPT),
        ]
        self.step_marks = []
        for num, title, hint, url in steps:
            box = ttk.Frame(f, padding=(0, 6))
            box.pack(fill="x")
            ttk.Label(box, text=num, font=("", 18, "bold"), width=2).pack(side="left", anchor="n")
            txt = ttk.Frame(box)
            txt.pack(side="left", fill="x", expand=True)
            ttk.Label(txt, text=title, font=("", 11, "bold")).pack(anchor="w")
            ttk.Label(txt, text=hint, foreground=C["muted"], justify="left").pack(anchor="w")
            mark = ttk.Label(box, text="", foreground=C["ok"], width=4)
            mark.pack(side="right")
            ttk.Button(box, text="打開", command=lambda u=url, m=mark: self.open_in_chrome(u, m)).pack(side="right", padx=6)
            self.step_marks.append(mark)

        ttk.Separator(f).pack(fill="x", pady=12)
        ttk.Label(f, text="✅ 完成後：打開影片網站，左下角出現綠色盾牌「🛡 已攔截 N 次」就成功了。",
                  wraplength=640, justify="left").pack(anchor="w")
        if not self.chrome:
            ttk.Label(f, text="⚠ 找不到 Chrome，會改用預設瀏覽器打開。這個工具是為 Chrome 設計的。",
                      foreground=C["bad"], wraplength=640).pack(anchor="w", pady=(8, 0))

    def open_in_chrome(self, url, mark):
        # chrome:// 開頭的設定頁，只有 Chrome 自己打得開；同時複製網址當備案
        if url.startswith("chrome://"):
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
        try:
            if self.chrome:
                subprocess.Popen([self.chrome, url])
            else:
                webbrowser.open(url)
        except Exception:
            webbrowser.open(url)
        mark.config(text="✓")
        if url.startswith("chrome://"):
            messagebox.showinfo("提示", "如果 Chrome 沒有自動打開設定頁：\n在 Chrome 網址列按 Ctrl+V 貼上，再按 Enter。\n（網址已經幫你複製好了）")

    def on_update_none(self, tag):
        if self.web and not self.web_fallback:
            self.add_notice("✅", f"目前已經是最新版本（v{VERSION}）")
            return
        messagebox.showinfo("檢查更新", f"目前已經是最新版本（v{VERSION}）。")

    def on_update_failed(self, msg):
        messagebox.showwarning("檢查更新", f"無法連到 GitHub 檢查更新：\n{msg[:200]}")

    def on_update(self, data):
        tag, url, notes = data
        self.update_info = (tag, url)
        if self.web and not self.web_fallback:            # 新介面：放在「最新消息」最上面＋通知，不跳視窗
            self.add_notice("✨", f"有新版本 v{tag}（目前 v{VERSION}），在首頁「最新消息」按一下前往下載", "home")
            return
        notes = notes[:600] + ("…" if len(notes) > 600 else "")
        msg = f"有新版本 v{tag}（目前 v{VERSION}）。\n\n" + (f"更新內容：\n{notes}\n\n" if notes else "") + "要前往下載嗎？"
        if messagebox.askyesno("有新版本", msg):
            webbrowser.open(url)

    # ------------------------------------------------------------------
    # ② 下載影片 + 下載佇列
    # ------------------------------------------------------------------

    def build_download(self, dl):
        pad = {"padx": 10, "pady": 4}

        top = ttk.Frame(dl)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="影片網址：").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.analyze())
        self.btn_read = ttk.Button(top, text="讀取", command=self.analyze, style="Accent.TButton")
        self.btn_read.pack(side="left")
        self.btn_batch = ttk.Button(top, text="📋 多個網址…", command=self.open_batch)
        self.btn_batch.pack(side="left", padx=(6, 0))

        # 剪貼簿裡有影片網址時，出現這一列（不會自動做任何事，要你按）
        # 放在分頁的上面，不管現在在哪個分頁都看得到
        self.clip_bar = ttk.Frame(self.root)
        self.clip_lbl = ttk.Label(self.clip_bar, text="", foreground=C["accent"])
        self.clip_lbl.pack(side="left")
        ttk.Button(self.clip_bar, text="讀取這個網址", command=self.use_clipboard).pack(side="left", padx=6)
        ttk.Button(self.clip_bar, text="✕", width=3, command=lambda: self.clip_bar.pack_forget()).pack(side="left")
        self.clip_anchor = ttk.Frame(dl)                # 提示列要插在這裡
        self.clip_anchor.pack(fill="x")

        self.title_lbl = ttk.Label(dl, text="貼上網址後按「讀取」；要一次下載很多部，按「📋 多個網址…」", wraplength=740)
        self.title_lbl.pack(fill="x", **pad)

        cols = ("quality", "size", "time", "type")
        self.tree = ttk.Treeview(dl, columns=cols, show="headings", height=5, selectmode="browse")
        for c, name, w in zip(cols, ("畫質", "檔案大小", "預估下載時間", "類型"), (100, 150, 170, 120)):
            self.tree.heading(c, text=name)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="x", **pad)
        self.style_tree(self.tree)
        self.tree.bind("<Double-1>", lambda e: self.download())

        mid = ttk.Frame(dl)
        mid.pack(fill="x", **pad)
        self.speed_lbl = ttk.Label(mid, text=f"網速：{self.mbps:.1f} Mbps（上次實測）" if self.mbps else "網速：尚未量測")
        self.speed_lbl.pack(side="left")
        self.btn_dl = ttk.Button(mid, text="⬇ 加入下載", command=self.download, state="disabled", style="Accent.TButton")
        self.btn_dl.pack(side="right")

        # 佇列
        ttk.Separator(dl).pack(fill="x", padx=10, pady=(6, 2))
        self.qhead = ttk.Label(dl, text="下載佇列（依序下載；按右鍵有更多選項）", style="H2.TLabel")
        self.qhead.pack(anchor="w", padx=10)
        qcols = ("title", "quality", "status")
        self.qtree = ttk.Treeview(dl, columns=qcols, show="headings", height=5, selectmode="browse")
        for c, name, w, anchor in (("title", "標題", 470, "w"), ("quality", "畫質", 80, "center"),
                                   ("status", "狀態", 150, "center")):
            self.qtree.heading(c, text=name)
            self.qtree.column(c, width=w, anchor=anchor)
        self.qtree.pack(fill="both", expand=True, **pad)
        self.style_tree(self.qtree)

        qbar = ttk.Frame(dl)
        qbar.pack(fill="x", **pad)
        self.btn_cancel = ttk.Button(qbar, text="■ 取消目前下載", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left")
        self.btn_pause = ttk.Button(qbar, text="⏸ 暫停", command=self.toggle_pause, state="disabled")
        self.btn_pause.pack(side="left", padx=6)
        # 有失敗的才出現
        self.btn_retry = ttk.Button(qbar, text="↻ 重試失敗的", command=self.retry, state="disabled")
        # 不常用的放右鍵選單
        self.add_menu(self.qtree, [("移除這一筆", self.queue_remove), ("清除已結束的", self.queue_clear), None,
                                   ("↻ 重試失敗的", self.retry),
                                   ("📂 打開下載資料夾", lambda: open_path(self.settings.download_dir))])

        self.prog = ttk.Progressbar(dl, maximum=100)
        self.prog.pack(fill="x", **pad)
        self.status = ttk.Label(dl, text="" if FFMPEG else "⚠ 找不到 ffmpeg，部分影片可能無法轉成 MP4")
        self.status.pack(fill="x", **pad)

    def style_tree(self, tree):
        """表格：斑馬紋、狀態上色"""
        stripe = "#262626" if self.theme == "dark" else "#f5f7fa"
        tree.tag_configure("odd", background=stripe)
        tree.tag_configure("ok", foreground=C["ok"])
        tree.tag_configure("bad", foreground=C["bad"])
        tree.tag_configure("warn", foreground=C["warn"])
        tree.tag_configure("run", foreground=C["accent"])

    def set_buttons(self):
        """依目前狀態決定按鈕能不能按"""
        self.btn_read.config(state="disabled" if self.analyzing else "normal")
        self.btn_batch.config(state="disabled" if self.batch_running else "normal")
        self.btn_dl.config(state="normal" if (self.formats and not self.analyzing) else "disabled")
        self.btn_cancel.config(state="normal" if self.cancel_event else "disabled")
        paused = self.queue_paused or self.queue.count(PAUSED)
        self.btn_pause.config(text="▶ 繼續" if paused else "⏸ 暫停",
                              state="normal" if (paused or self.cancel_event or self.queue.count(WAITING)) else "disabled")
        failed = self.queue.count(FAILED, NEEDS_ACTION)
        self.btn_retry.config(state="normal" if failed else "disabled")
        if failed and not self.btn_retry.winfo_manager():
            self.btn_retry.pack(side="left", padx=6)
        elif not failed and self.btn_retry.winfo_manager():
            self.btn_retry.pack_forget()

    def add_menu(self, tree, items):
        """右鍵選單：先選到按的那一行，再跳出選單。items 裡的 None 是分隔線"""
        menu = tk.Menu(tree, tearoff=0)
        for it in items:
            if it is None:
                menu.add_separator()
            else:
                menu.add_command(label=it[0], command=it[1])

        def popup(e):
            row = tree.identify_row(e.y)
            if row:
                tree.selection_set(row)
                tree.focus(row)
            try:
                menu.tk_popup(e.x_root, e.y_root)
            finally:
                menu.grab_release()
        tree.bind("<Button-3>", popup)
        tree.bind("<Button-2>", popup)            # Mac 的觸控板
        return menu

    # ----- 讀取單一網址 -----

    def analyze(self):
        url = self.url_var.get().strip()
        if not url or self.analyzing:
            return
        self.analyzing = True
        self.formats = []
        self.tree.delete(*self.tree.get_children())
        self.title_lbl.config(text="讀取中…")
        self.set_buttons()
        threading.Thread(target=self._analyze_worker, args=(url,), daemon=True).start()

    def _analyze_worker(self, url):
        try:
            self.q.put(("analyzed", (url,) + self.analyze_fn(url)))
        except Exception as e:
            log.exception("讀取失敗：%s", log_url(url))
            self.q.put(("analyze_error", f"讀取失敗：{e}"))

    def remember_speed(self, mbps):
        if mbps:
            self.mbps = mbps
            self.speed_src = "實測"
            self.settings["mbps"] = mbps
            self.settings.save()
            self.speed_lbl.config(text=f"網速：{self.mbps:.1f} Mbps（實測）")

    def on_analyzed(self, data):
        self.url, self.title, self.duration, self.formats, mbps = data
        self.tree.delete(*self.tree.get_children())     # 不管怎麼被叫到，先清掉舊的清單
        self.remember_speed(mbps)
        self.analyzing = False
        self.title_lbl.config(text=f"{self.title}　（長度 {fmt_duration(self.duration)}）")
        if not self.mbps:
            self.speed_lbl.config(text="網速：無法量測（下載時會顯示實際速度）")
        if not self.formats:
            self.title_lbl.config(text=f"{self.title}：找不到可下載的畫質")
            self.set_buttons()
            return
        site = site_of(self.url)
        tf, tn = self.stats.factor("time", site)
        sf, sn = self.stats.factor("size", site)
        for i, f in enumerate(self.formats):
            show_size = f["size"] * (sf if (sf and not f["exact"]) else 1)
            size = ("" if f["exact"] or not f["size"] else "約 ") + fmt_size(show_size)
            raw_t = f["size"] * 8 / (self.mbps * 1e6) if f["size"] and self.mbps else None
            t = fmt_time(raw_t * (tf or 1)) if raw_t else "時間未知"
            kind = "MP4 直連" if f["direct"] else "切片"
            self.tree.insert("", "end", iid=str(i), values=(quality_label(f["height"]), size, t, kind))
        if tf or sf:
            n = max(tn, sn)
            self.speed_lbl.config(text=self.speed_lbl.cget("text").split("｜")[0] + f"｜預估已依過去 {n} 次下載校正")

        # 預設選哪個畫質：從紀錄重新下載 → 同畫質；否則 → 設定裡的預設畫質
        if self.want_height is not None:
            idx = pick_default(self.formats, str(self.want_height) if self.want_height else "最高")
            self.want_height = None
        else:
            idx = pick_default(self.formats, self.settings["default_quality"])
        self.tree.selection_set(str(idx))
        self.tree.see(str(idx))
        self.set_buttons()

    def on_analyze_error(self, msg):
        self.analyzing = False
        self.title_lbl.config(text="❌ " + msg[:200] + "　（詳細原因在「③ 設定」→ 進階設定 → 問題排查 → 打開錯誤日誌）")
        self.set_buttons()

    def download(self):
        """把選取的畫質加進佇列"""
        sel = self.tree.selection()
        if not sel or self.analyzing:
            return
        f = self.formats[int(sel[0])]
        if self.queue.is_duplicate(self.url, f["height"]):
            messagebox.showinfo("已經在佇列裡", "這部影片的這個畫質已經在排隊、下載中或暫停了。")
            return
        old = find_downloaded(self.history.items, self.url)
        if old and not messagebox.askyesno(
                "已經下載過了", f"這支影片 {old.get('time', '')} 下載過（{quality_label(old.get('height'))}）：\n"
                                f"{old['file']}\n\n還要再下載一次嗎？"):
            return
        vals = self.tree.item(sel[0], "values")
        out = self.settings.download_dir
        running = self.queue.running() is not None
        if not messagebox.askyesno("確定要下載嗎？",
                                   f"{self.title}\n\n畫質：{vals[0]}\n大小：{vals[1]}\n時間：{vals[2]}\n"
                                   f"長度：{fmt_duration(self.duration)}\n\n儲存到：{out}"
                                   + ("\n\n（目前有其他影片在下載，會排在後面）" if running else "")):
            return
        self.enqueue({"url": self.url, "title": self.title, "fmt": f, "size": f["size"], "out": out})

    # ----- 一次加入很多網址 -----

    def open_batch(self):
        win = tk.Toplevel(self.root)
        win.title("一次加入多個網址")
        win.geometry(f"{px(560)}x{px(360)}")
        win.transient(self.root)
        ttk.Label(win, text="一行貼一個網址。每部都會用「預設畫質」"
                            f"（目前：{self.settings['default_quality']}）加入佇列。",
                  wraplength=520).pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(win, height=12, bg=C["surface"], fg=C["text"], insertbackground=C["text"], relief="flat",
                      highlightthickness=1, highlightbackground=C["line"], padx=8, pady=6)
        txt.pack(fill="both", expand=True, padx=10)
        try:
            clip = self.root.clipboard_get()
            if "http" in clip:
                txt.insert("1.0", clip.strip() + "\n")        # 剪貼簿裡有網址就先幫忙貼上
        except Exception:
            pass

        def go():
            urls = []
            for line in txt.get("1.0", "end").splitlines():
                u = line.strip()
                if u.startswith("http") and u not in urls:
                    urls.append(u)
            if not urls:
                messagebox.showinfo("沒有網址", "請一行貼一個 http 開頭的網址。", parent=win)
                return
            win.destroy()
            self.start_batch(urls)

        ttk.Button(win, text="加入佇列", command=go).pack(pady=8)
        self.batch_win = win

    def start_batch(self, urls, quality=None):
        self.batch_running = True
        self.set_buttons()
        self.status.config(text=f"讀取網址中 0/{len(urls)}…")
        q = quality if quality in QUALITY_CHOICES else self.settings["default_quality"]
        threading.Thread(target=self._batch_worker, args=(urls, q), daemon=True).start()

    def _batch_worker(self, urls, quality):
        ok = fail = 0
        for i, url in enumerate(urls, 1):
            self.q.put(("batch_status", f"讀取網址中 {i}/{len(urls)}…"))
            try:
                title, dur, fmts, mbps = self.analyze_fn(url)
                if not fmts:
                    raise RuntimeError("找不到可下載的畫質")
                f = fmts[pick_default(fmts, quality)]
                self.q.put(("batch_item", ({"url": url, "title": title, "fmt": f, "size": f["size"]}, mbps)))
                ok += 1
            except Exception as e:
                log.exception("批次讀取失敗：%s", log_url(url))
                self.q.put(("batch_item_error", (url, str(e))))
                fail += 1
        self.q.put(("batch_done", (ok, fail)))

    def on_batch_status(self, text):
        if not self.queue.running():
            self.status.config(text=text)

    def on_batch_item(self, data):
        task, mbps = data
        self.remember_speed(mbps)
        if self.settings["skip_downloaded"] and find_downloaded(self.history.items, task["url"]):
            self.status.config(text=f"略過（已經下載過）：{task['title'][:40]}")
            return
        if not self.queue.is_duplicate(task["url"], task["fmt"]["height"]):
            self.enqueue(dict(task, out=self.settings.download_dir))

    def on_batch_item_error(self, data):
        url, err = data
        if self.settings["keep_history"]:
            self.history.add(title=url, url=url, height=None, status=FAILED, file="", size=0,
                             error=f"讀取失敗：{err}"[:500])
            self.refresh_history()

    def on_batch_done(self, data):
        ok, fail = data
        self.batch_running = False
        self.set_buttons()
        msg = f"已加入 {ok} 部" + (f"，{fail} 部讀取失敗（原因在「② 下載紀錄」）" if fail else "")
        # 不跳視窗打斷使用者，寫在佇列標題旁邊
        self.refresh_queue()
        self.qhead.config(text=self.qhead.cget("text") + f"　｜　批次：{msg}")

    # ----- 佇列 -----

    def enqueue(self, task):
        if getattr(self, "home", None) is not None and self.home.visible():   # 有新下載 → 離開首頁，看得到佇列
            self.home.hide()
            self.nb.select(0)
        f = task["fmt"]
        task.setdefault("est_bytes", f.get("size") or 0)       # 原始預估（沒校正過的），之後拿來跟實際比
        task.setdefault("est_exact", bool(f.get("exact")))
        task.setdefault("est_seconds", (f.get("size") or 0) * 8 / (self.mbps * 1e6) if self.mbps and f.get("size") else 0)
        t = self.queue.add(task)
        log.info("加入佇列 #%d：%s（%s）", t["id"], t["title"], quality_label(t["fmt"]["height"]))
        self.refresh_queue()
        self.pump()
        return t

    def pump(self):
        """沒有東西在下載、又有人在排隊 → 開始下一個（按了暫停、或網路連續出錯在冷卻中就不開始）"""
        if self.queue.running() or self.queue_paused:
            self.set_buttons()
            return
        t = self.queue.next_waiting(skip=lambda x: HOSTS.cooling(site_of(x["url"])) > 0)
        if not t:
            cool = [x for x in self.queue.tasks if x["status"] == WAITING]
            if cool:                                    # 有人在排隊，但他們的網站都在冷卻
                s0 = site_of(cool[0]["url"])
                self.status.config(text=f"🧊 {s0} 冷卻中（{HOSTS.h.get(s0, {}).get('why', '')}），"
                                        f"{int(HOSTS.cooling(s0))} 秒後再下載這個網站")
            self.set_buttons()
            return
        hold = NET.holding()
        if hold > 0:
            self.status.config(text=f"🌐 網路連續出錯，暫緩 {int(hold)} 秒再繼續（成功一次就恢復正常）")
            self.set_buttons()
            return
        t["status"] = RUNNING
        self.cancel_event = threading.Event()
        self.stop_mode = "cancel"
        self.prog["value"] = 0
        self.status.config(text=f"準備下載：{t['title']}")
        self.refresh_queue()
        self.set_buttons()
        self.worker = threading.Thread(target=self._download_worker, args=(t, self.cancel_event), daemon=True)
        self.worker.start()

    def _download_worker(self, task, cancel_event):
        tid = task["id"]
        mode = self.settings["smart_segments"]
        site = site_of(task["url"])
        parts = self.segments.suggest(site) if mode == "auto" else PARALLEL_PARTS
        measured = {"parts": parts}
        task["seg"] = {"mode": "auto" if mode == "auto" else "fixed", "parts": parts}
        try:
            result = self.download_fn(task["url"], task["fmt"], task["out"],
                                      on_progress=lambda *a: self.q.put(("progress", (tid,) + a)),
                                      on_stage=lambda t: self.q.put(("progress_text", (tid, t))),
                                      cancel_event=cancel_event,
                                      name_template=self.settings["filename_template"],
                                      stats=measured, keep_partials=lambda: self.stop_mode == "pause")
            task["measured"] = measured
            self.q.put(("done", (tid, result)))
        except Paused:
            task["partials"] = sorted(measured.get("touched") or [])
            self.q.put(("paused", tid))
        except Cancelled:
            self.q.put(("cancelled", tid))
        except Exception as e:
            task["measured"] = measured
            self.q.put(("download_error", (tid, f"{e}")))

    def refresh_queue(self):
        sel = self.qtree.selection()
        self.qtree.delete(*self.qtree.get_children())
        icon = {WAITING: "⏳ 等待中", DONE: "✅ 完成", FAILED: "❌ 失敗", CANCELLED: "■ 取消", PAUSED: "⏸ 暫停",
                NEEDS_ACTION: "✋ 需要處理"}
        for t in self.queue.tasks:
            st = f"⬇ {t['progress']:.0f}%" if t["status"] == RUNNING else icon.get(t["status"], t["status"])
            if t["status"] == RETRY_WAIT:
                st = f"🔁 {max(0, int(t.get('retry_at', 0) - time.time()))} 秒後重試（{t.get('tries', 1)}/{RETRY_MAX}）"
            if t.get("note") and t["status"] == DONE:
                st = "⚠ 完成（轉檔失敗）"
            tag = {FAILED: "bad", NEEDS_ACTION: "bad", RUNNING: "run", RETRY_WAIT: "warn"}.get(t["status"], "")
            self.qtree.insert("", "end", iid=str(t["id"]), values=(t["title"], quality_label(t["fmt"]["height"]), st),
                              tags=(tag, "odd" if len(self.qtree.get_children()) % 2 else "even"))
        if sel and self.qtree.exists(sel[0]):
            self.qtree.selection_set(sel[0])
        w, r, p = self.queue.count(WAITING), self.queue.count(RUNNING), self.queue.count(PAUSED)
        lim = self.settings["rate_limit_mbps"]
        self.qhead.config(text=f"下載佇列（依序下載）　下載中 {r}　排隊中 {w}" + (f"　暫停 {p}" if p else "")
                          + ("　⏸ 已暫停" if self.queue_paused else "") + (f"　速度上限 {lim:g} Mbps" if lim else ""))

    def cancel(self):
        t = self.queue.running()
        if t and self.cancel_event and messagebox.askyesno(
                "取消下載", f"確定要取消「{t['title']}」嗎？\n已下載的部分會被刪除；排在後面的會繼續下載。"):
            self.cancel_event.set()
            self.status.config(text="取消中…")

    def retry(self):
        n = self.queue.retry_failed()
        if n:
            log.info("重試 %d 個失敗的任務", n)
            self.refresh_queue()
            self.pump()

    def queue_remove(self):
        sel = self.qtree.selection()
        if not sel:
            return
        t = self.queue.get(int(sel[0]))
        if t and t["status"] == RUNNING:
            messagebox.showinfo("下載中", "這個正在下載，要停止請按「■ 取消目前下載」。")
            return
        if t and t["status"] == PAUSED and t.get("partials"):
            cleanup_partials(t["partials"], include_complete=True)      # 暫停留下的半成品一起刪
        self.queue.remove(int(sel[0]))
        self.refresh_queue()
        self.set_buttons()

    def toggle_pause(self):
        """⏸ 暫停：停下目前的下載（保留已下載的部分）、不開始下一個；▶ 繼續：從停下的地方接著下載"""
        if self.queue_paused or self.queue.count(PAUSED):
            self.queue_paused = False
            self.queue.resume_paused()
            self.status.config(text="▶ 繼續下載")
            self.refresh_queue()
            self.pump()
            return
        self.queue_paused = True
        if self.cancel_event:
            self.stop_mode = "pause"
            self.cancel_event.set()
            self.status.config(text="暫停中…（已下載的部分會保留）")
        else:
            self.status.config(text="⏸ 已暫停：排隊中的不會開始，按「▶ 繼續」恢復")
        self.refresh_queue()
        self.set_buttons()

    def on_paused(self, tid):
        t = self.queue.get(tid)
        if t:
            t["status"] = PAUSED
        self.cancel_event = None
        self.worker = None
        self.status.config(text="⏸ 已暫停，按「▶ 繼續」會從停下的地方接著下載")
        self.refresh_queue()
        self.set_buttons()
        if self.tray:
            self.tray.title = "Video Helper：已暫停"

    def queue_clear(self):
        self.queue.clear_finished()
        self.refresh_queue()
        self.set_buttons()

    def on_progress(self, data):
        tid, done, total, speed, eta = data
        t = self.queue.get(tid)
        if not t:
            return
        if total:
            t["progress"] = done / total * 100
            self.prog["value"] = t["progress"]
        parts = [f"{fmt_size(done)} / {fmt_size(total)}" if total else fmt_size(done)]
        now = time.time()                               # 每秒量一次「這一秒」下載了多少（不是從頭平均）
        lt, ld = t.get("_lt"), t.get("_ld")
        if lt is None or done < (ld or 0):
            t["_lt"], t["_ld"] = now, done
        elif now - lt >= 1.0:
            inst = (done - ld) / (now - lt)
            prev = self.live[1] if self.live and now - self.live[0] < 3 else inst
            self.live = (now, 0.5 * inst + 0.5 * prev)  # 輕微平滑，數字不會亂跳
            t["_lt"], t["_ld"] = now, done
        if self.live and now - self.live[0] < 3:
            parts.append(f"{self.live[1] * 8 / 1e6:.1f} Mbps")
            self.mbps = self.live[1] * 8 / 1e6
        elif speed:
            parts.append(f"{speed * 8 / 1e6:.1f} Mbps")
        if eta is not None:
            parts.append(f"剩約 {fmt_time(eta)}")
        w = self.queue.count(WAITING)
        self.status.config(text=f"下載中「{t['title'][:30]}」：" + "｜".join(parts) + (f"｜後面還有 {w} 部" if w else ""))
        # 佇列表格不用每次都整個重畫，只改這一行的狀態
        if self.qtree.exists(str(tid)):
            self.qtree.set(str(tid), "status", f"⬇ {t['progress']:.0f}%")
        if self.tray:
            self.tray.title = f"Video Helper：下載中 {t['progress']:.0f}%" + (f"（還有 {w} 部）" if w else "")

    def on_progress_text(self, data):
        tid, text = data
        self.status.config(text=text)

    def finish_task(self, tid, status, file=None, error=None, history_status=None):
        t = self.queue.get(tid)
        for rec in self.remote.values():           # 瀏覽器那邊查得到最後的結果
            if rec.get("task_id") == tid:
                rec.update(status=status, progress=100 if status == DONE else rec.get("progress", 0), error=error or "")
        if t:
            t["status"] = status
            if status == DONE:
                t["progress"] = 100
            if self.settings["keep_history"]:
                self.history.add(title=t["title"], url=t["url"], height=t["fmt"]["height"],
                                 status=history_status or status, file=file or "", size=t.get("size") or 0,
                                 error=error or "")
                self.refresh_history()
        if self.web and t and status in (DONE, FAILED, NEEDS_ACTION):
            self.add_notice("✅" if status == DONE else "❌", ("下載完成：" if status == DONE else "下載失敗：") + t["title"][:60], "dl")
        self.cancel_event = None
        self.worker = None
        self.refresh_queue()
        self.pump()                   # 換下一個
        self.set_buttons()
        if self.is_hidden() and t and status in (DONE, FAILED, NEEDS_ACTION) and not self.quitting:
            self.notify(("✅ 下載完成：" if status == DONE else "❌ 下載失敗：") + t["title"][:60])
            if not self.queue.running() and not self.queue.next_waiting():
                self.notify("佇列裡的影片都處理完了")
        if self.tray:
            self.tray.title = "Video Helper" + ("：下載中" if self.queue.running() else "")
        if not self.queue.running():
            self.prog["value"] = 100 if status == DONE else 0

    def on_done(self, data):
        tid, (path, warning) = data
        if self.mbps:
            self.settings["mbps"] = self.mbps
            self.settings.save()
        t = self.queue.get(tid)
        NET.ok()
        if t:
            HOSTS.ok(site_of(t["url"]))
            self.smart_learn(t, None)
            t["tries"] = 0
        if t and warning:
            t["note"] = warning
        m = (t or {}).get("measured") or {}
        if t and self.settings["keep_stats"] and m.get("bytes") and m.get("seconds", 0) > 0.5:
            self.stats.add(site=site_of(t["url"]), height=t["fmt"]["height"], exact=t.get("est_exact"),
                           est_bytes=t.get("est_bytes"), est_seconds=t.get("est_seconds"),
                           bytes=m["bytes"], seconds=m["seconds"])
            self.refresh_stats()
        name = os.path.basename(path) if path else "檔案在下載資料夾"
        self.status.config(text=(f"⚠ {warning}：{name}" if warning else f"✅ 下載完成：{name}"))
        self.finish_task(tid, DONE, file=path, error=warning)

    def on_cancelled(self, tid):
        self.status.config(text="已取消，殘留的暫存檔已清除")
        self.finish_task(tid, CANCELLED)

    def smart_learn(self, t, kind):
        """任務結束（成功或失敗）：記錄結果、讓分段控制器學習下一個任務要分幾段"""
        m = t.get("measured") or {}
        tele = m.get("tele")
        tele = tele.summary() if isinstance(tele, Telemetry) else {}
        seg = t.get("seg") or {"mode": "fixed", "parts": PARALLEL_PARTS}
        site = site_of(t["url"])
        mbps = round(m["bytes"] * 8 / m["seconds"] / 1e6, 2) if m.get("bytes") and m.get("seconds") else None
        decision_log(site=site, action="task_result", mode=seg["mode"], parts=seg["parts"], result="success" if kind is None else "fail",
                     kind=kind, mbps=mbps, tries=t.get("tries", 0), **{k: tele.get(k) for k in ("speed_ewma_mbps", "seg_errors", "seg_timeouts")})
        mode = self.settings["smart_segments"]
        if mode == "off" or not tele:
            return
        decision, reason, before, after = self.segments.learn(site, tele, kind)
        if decision != "keep":
            decision_log(site=site, action="segments", applied=mode == "auto", speed_ewma_mbps=tele.get("speed_ewma_mbps"),
                         segment_count_before=before, decision=decision + "_segments", segment_count_after=after, reason=reason,
                         result="success" if kind is None else "fail")
            log.info("智慧分段（%s）：%s %d → %d 段（%s）", "自動" if mode == "auto" else "只觀察", site, before, after, reason)
        self.settings.save()

    def on_download_error(self, data):
        tid, msg = data
        t = self.queue.get(tid)
        info = classify_error(msg)
        site = site_of(t["url"]) if t else "?"
        hold = NET.bad(info["kind"], site)
        if t:
            self.smart_learn(t, info["kind"])

        # 硬碟滿了：整個佇列暫停、任務放回等待中，等使用者清空間
        if info["kind"] == "disk_full":
            if t:
                t.update(status=WAITING, progress=0, note="")
            self.queue_paused = True
            self.cancel_event = None
            self.worker = None
            decision_log(site=site, kind=info["kind"], action="pause_queue")
            self.status.config(text="💾 硬碟空間不足：佇列已暫停。清出空間後按「▶ 繼續」")
            if self.is_hidden() and not self.quitting:
                self.notify("💾 硬碟空間不足，下載已暫停")
            self.refresh_queue()
            self.set_buttons()
            return

        # 可自動重試：排進「等重試」，時間到自動繼續（指數退避＋抖動；429 照 Retry-After）
        tries = (t.get("tries", 0) if t else 0) + 1
        if t and info["retry"] and tries <= info["max"]:
            wait = retry_wait_seconds(info, tries, msg)
            cool = HOSTS.bad(site, info["kind"], wait)     # 只冷卻這個網站，其他網站照常
            if cool:
                decision_log(site=site, kind=info["kind"], action="host_cooldown", wait=round(cool))
            t.update(status=RETRY_WAIT, tries=tries, retry_at=time.time() + wait,
                     note=f"{info['label']}，{int(wait)} 秒後自動重試（第 {tries}/{info['max']} 次）")
            decision_log(site=site, kind=info["kind"], action="retry", wait=round(wait), tries=tries,
                         net_hold=round(hold))
            self.status.config(text=f"🔁 {info['label']}：{int(wait)} 秒後自動重試（第 {tries}/{info['max']} 次）")
            self.cancel_event = None
            self.worker = None
            self.refresh_queue()
            self.pump()                                 # 這個在等，先下載下一個
            self.set_buttons()
            return

        # 重試也沒用（403／404／網址錯…）→ 需要處理；重試次數用完 → 失敗
        if not info["retry"] and info["kind"] != "unknown":
            decision_log(site=site, kind=info["kind"], action="needs_user")
            note = f"{info['label']}。{info['hint']}"
            self.status.config(text=f"✋ {note}")
            if t:
                t["note"] = note
            self.finish_task(tid, NEEDS_ACTION, error=f"{info['label']}：{msg[:300]}", history_status=FAILED)
            return
        decision_log(site=site, kind=info["kind"], action="give_up", tries=tries - 1)
        self.status.config(text=f"❌ {info['label']}：自動重試 {tries - 1} 次都失敗　→ 可以按「↻ 重試失敗的」")
        self.finish_task(tid, FAILED, error=f"{info['label']}（自動重試 {tries - 1} 次）：{msg[:300]}")

    # ----- 瀏覽器連動 -----

    def remote_status(self, ticket):
        """連線服務的執行緒會呼叫這裡；只讀資料，不改畫面"""
        rec = self.remote.get(ticket)
        if not rec:
            return None
        t = self.queue.get(rec.get("task_id")) if rec.get("task_id") else None
        if t:
            return {"status": t["status"], "progress": round(t["progress"]), "title": t["title"]}
        return {k: rec.get(k) for k in ("status", "progress", "title", "error")}

    def classify_domains(self, site, domains, hosts=None):
        """
        連線服務的執行緒呼叫。先查免費的公開廣告清單；清單沒有的，有設定 AI 金鑰才問 AI。
        回傳 {網域: {"verdict", "label", "reason", "block", "source"}}
        """
        hosts = hosts or {}
        out, rest = {}, []
        for d in domains:
            hit = None
            for h in [*hosts.get(d, []), d]:
                hit = self.blocklist.match(h)
                if hit:
                    break
            if hit:
                out[d] = {"verdict": "ad", "reason": f"在公開廣告清單裡（{hit}）", "source": "list"}
            else:
                rest.append(d)
        key = (self.settings["ai_key"] or "").strip()
        if rest and key:
            cache = dict(self.settings["ai_cache"] or {})
            now = time.time()
            ask = []
            for d in rest:
                c = cache.get(d)
                if c and now - c.get("t", 0) < AI_CACHE_DAYS * 86400:
                    out[d] = {"verdict": c["verdict"], "reason": c["reason"], "source": "ai"}
                else:
                    ask.append(d)
            if ask:
                got = self.ai_fn(key, site, ask)
                for d in ask:
                    r = got.get(d, {"verdict": "unknown", "reason": "AI 沒有回答這個"})
                    out[d] = {"verdict": r["verdict"], "reason": r["reason"], "source": "ai"}
                    cache[d] = {"verdict": r["verdict"], "reason": r["reason"], "t": now}
                if len(cache) > AI_CACHE_MAX:              # 太多就忘掉最舊的
                    cache = dict(sorted(cache.items(), key=lambda kv: kv[1].get("t", 0))[-AI_CACHE_MAX:])
                self.settings["ai_cache"] = cache
                self.settings.save()
                log.info("AI 判斷 %d 個網域", len(ask))
        else:
            why = self.blocklist.error or "公開廣告清單裡沒有"
            for d in rest:
                out[d] = {"verdict": "unknown", "reason": why, "source": "list"}
        for r in out.values():
            r["label"] = "已知廣告" if r["source"] == "list" and r["verdict"] == "ad" else AI_VERDICTS.get(r["verdict"], "不確定")
            r["block"] = r["verdict"] in AI_BLOCK
        return out

    def node_enqueue(self, url, media, title):
        """V3.1 瀏覽器「交給節點」（連線服務的執行緒呼叫）：建一個 video.download 任務，帶瀏覽器拿到的影片位址。
        Master 會把參數全部重新驗證一次（同網站、清單網域、https）；不合格 → RuntimeError（回給瀏覽器看）"""
        svc = getattr(self, "nexus", None)
        if not svc or not svc.running or not svc.tasks:
            raise RuntimeError("電腦版的 Master 沒開：到「⚙ 進階設定 → 🖧 節點」勾「啟用 Master」，並至少核准一台節點")
        params = {"url": url, "media_url": media, "return_to_master": True}     # V4 D2：瀏覽器交的也預設傳回
        if title.strip():
            params["title"] = title.strip()
        t = svc.tasks.create("video.download", params, actor="browser")
        if t["status"] == "REJECTED":
            raise ValueError("任務不合格：" + (t.get("error_message") or "內容不對"))
        return t["task_id"]

    def remote_register(self, ticket, url, h, media=None):
        """連線服務的執行緒呼叫：先記下任務再回覆瀏覽器，瀏覽器馬上來問進度才查得到"""
        self.remote[ticket] = {"status": "讀取中", "progress": 0, "title": url, "error": ""}
        self.q.put(("remote_enqueue", (ticket, url, h, media)))

    def on_remote_enqueue(self, data):
        ticket, url, h, media = data
        self.remote.setdefault(ticket, {"status": "讀取中", "progress": 0, "title": url, "error": ""})
        if len(self.remote) > 200:                  # 舊的忘掉
            for k in list(self.remote)[:50]:
                if k != ticket:
                    self.remote.pop(k, None)
        def work():
            try:
                if media:
                    # 先用瀏覽器給的影片位址；不行才自己開網頁（網站可能會擋）
                    try:
                        title, dur, fmts, mbps = self.analyze_media_fn(media, h)
                        if fmts:
                            self.q.put(("remote_analyzed", (ticket, url, h, title, fmts, mbps)))
                            return
                    except Exception:
                        log.exception("瀏覽器給的影片位址讀不到，改開網頁：%s", log_url(url))
                title, dur, fmts, mbps = self.analyze_fn(url)
                self.q.put(("remote_analyzed", (ticket, url, h, title, fmts, mbps)))
            except Exception as e:
                log.exception("瀏覽器送來的網址讀取失敗：%s", log_url(url))
                self.q.put(("remote_failed", (ticket, f"讀取失敗：{e}")))
        threading.Thread(target=work, daemon=True).start()

    def on_remote_analyzed(self, data):
        ticket, url, h, title, fmts, mbps = data
        rec = self.remote.get(ticket, {})
        if not fmts:
            rec.update(status=FAILED, error="找不到可下載的畫質", title=title)
            return
        self.remember_speed(mbps)
        f = fmts[pick_default(fmts, str(h) if h else self.settings["default_quality"])]
        if self.queue.is_duplicate(url, f["height"]):
            rec.update(status=FAILED, error="已經在佇列裡了", title=title)
            return
        old = find_downloaded(self.history.items, url) if self.settings["skip_downloaded"] else None
        if old:
            rec.update(status=FAILED, error=f"已經下載過了：{os.path.basename(old['file'])}", title=title)
            self.status.config(text=f"🔗 略過（已經下載過）：{title[:40]}")
            return
        t = self.enqueue({"url": url, "title": title, "fmt": f, "size": f["size"], "out": self.settings.download_dir})
        rec.update(task_id=t["id"], title=title, status=WAITING)
        self.status.config(text=f"🔗 從瀏覽器加入：{title}（{quality_label(f['height'])}）")
        if self.is_hidden():
            self.notify(f"從瀏覽器加入：{title[:60]}")

    def on_remote_failed(self, data):
        ticket, msg = data
        self.remote.setdefault(ticket, {}).update(status=FAILED, error=msg[:200])

    def check_clipboard(self):
        """視窗被切回來時看一下剪貼簿；有支援的影片網址就出現提示列"""
        try:
            clip = self.root.clipboard_get().strip()
        except Exception:
            return
        if (clip == self.last_clip or "\n" in clip or len(clip) > 500 or not bridge_url_allowed(clip)
                or clip == self.url_var.get().strip()):
            return
        self.last_clip = clip
        self.clip_lbl.config(text=f"📋 剪貼簿裡有影片網址：{clip[:60]}{'…' if len(clip) > 60 else ''}")
        self.clip_bar.pack(fill="x", padx=10, pady=(6, 2), before=self.nb)

    def use_clipboard(self):
        self.clip_bar.pack_forget()
        self.url_var.set(self.last_clip)
        self.nb.select(self.tab_dl)
        self.analyze()

    def show_home(self):
        self.header.set_active(-1)
        self.home.show()
        if not getattr(self, "_warmed", False):          # 第一次：在首頁後面把每個分頁偷偷畫一次，之後切換不會停頓
            self._warmed = True
            self.root.after(400, self._warm_tabs)

    def _warm_tabs(self):
        if not self.home.visible():
            return
        cur = self.nb.index("current")
        self._warming = True                             # 這時候的「切換分頁」不要把首頁關掉
        try:
            for i in range(self.nb.index("end")):
                self.nb.select(i)
                self.root.update()
            self.nb.select(cur)
            self.root.update()
        finally:
            self._warming = False

    def pick_home(self, idx):
        self.nb.select(idx)
        self.header.set_active(idx)

    def nav(self, idx):
        """頂部選單：-1＝首頁，其他＝分頁"""
        if idx == -1:
            if not self.home.visible():
                self.show_home()
            return
        if self.home.visible():
            self.home.hide()
        self.nb.select(idx)
        self.root.update_idletasks()                  # 先把新分頁排版好，再開始播底線動畫（不然動畫會跳格）
        self.header.set_active(idx)

    def home_kpis(self):
        """首頁數據卡：今天下載（附 7 天長條圖）、近 30 天下載量、平均速度、成功率"""
        now = datetime.now()
        days = [(now - timedelta(days=d)).strftime("%Y-%m-%d") for d in range(6, -1, -1)]
        since = (now - timedelta(days=30)).strftime("%Y-%m-%d")
        items = self.history.items
        done = [r for r in items if r.get("status") == DONE]
        spark = [sum(1 for r in done if str(r.get("time", "")).startswith(d)) for d in days]
        recent = [r for r in items if str(r.get("time", "")) >= since and r.get("status") in (DONE, FAILED)]
        ok = sum(1 for r in recent if r["status"] == DONE)
        gb = sum(r.get("size") or 0 for r in recent if r["status"] == DONE) / 1e9
        speeds = [r["mbps"] for r in self.stats.items[:20] if r.get("mbps")]
        avg = sum(speeds) / len(speeds) if speeds else (self.mbps or None)
        live = self.live and time.time() - self.live[0] < 3       # 正在下載 → 顯示這一秒的速度
        return [
            {"label": "今天下載", "value": spark[-1], "fmt": "{:.0f}", "unit": "部", "spark": spark},
            {"label": "近 30 天", "value": gb, "fmt": "{:.1f}", "unit": "GB", "dot": C["accent"]},
            {"label": "目前速度（每秒更新）", "value": self.live[1] * 8 / 1e6, "fmt": "{:.1f}", "unit": "Mbps", "dot": C["accent"]}
            if live else {"label": "平均速度（最近 20 部）", "value": avg, "fmt": "{:.0f}", "unit": "Mbps", "dot": C["ok"]},
            {"label": "成功率", "value": ok / len(recent) * 100 if recent else None, "fmt": "{:.0f}", "unit": "%",
             "dot": C["ok"] if not recent or ok / len(recent) >= .9 else C["warn"]},
        ]

    def home_status(self):
        run = sum(1 for t in self.queue.tasks if t["status"] == RUNNING)
        wait = sum(1 for t in self.queue.tasks if t["status"] in (WAITING, RETRY_WAIT))
        parts = []
        if run or wait:
            parts.append(f"正在下載 {run} 部、排隊 {wait} 部")
        if self.mbps:
            parts.append(f"網速約 {self.mbps:.0f} Mbps")
        parts.append("瀏覽器已連動" if self.bridge.server else "瀏覽器未連動")
        return "・".join(parts)

    def refresh_header(self):
        """品牌列右邊的狀態膠囊：下載中幾個、網速、瀏覽器連動、版本"""
        run = sum(1 for t in self.queue.tasks if t["status"] == RUNNING)
        wait = sum(1 for t in self.queue.tasks if t["status"] in (WAITING, RETRY_WAIT))
        pills = []
        if run or wait:
            pills.append((f"下載中 {run}｜排隊 {wait}", C["accent"]))
        if self.live and time.time() - self.live[0] < 3:
            pills.append((f"下載速度 {self.live[1] * 8 / 1e6:.1f} Mbps", C["accent"]))
        elif self.mbps:
            pills.append((f"{self.mbps:.0f} Mbps", C["ok"]))
        pills.append(("連動中" if self.bridge.server else "未連動", C["ok"] if self.bridge.server else "#9aa3b2"))
        pills.append((f"v{VERSION}", C["accent2"]))
        self.ticker.set_items([t for t, _ in pills] + list(TIPS))

    def refresh_bridge_label(self):
        if not hasattr(self, "bridge_lbl"):
            return
        n = len(self.settings["bridge_tokens"] or [])
        if self.bridge.server:
            self.bridge_lbl.config(text=f"✅ 執行中（127.0.0.1:{self.bridge.port}）｜已配對 {n} 個瀏覽器", foreground=C["ok"])
        elif self.settings["bridge_enabled"]:
            self.bridge_lbl.config(text="⚠ 無法啟動（連接埠都被占用）", foreground=C["bad"])
        else:
            self.bridge_lbl.config(text="關閉中（按「配對」會打開）", foreground=C["muted"])

    def pair_browser(self):
        if not self.bridge.start():
            messagebox.showerror("無法啟動", f"連接埠 {BRIDGE_PORTS[0]}～{BRIDGE_PORTS[-1]} 都被占用，暫時無法使用瀏覽器連動。")
            self.refresh_bridge_label()
            return
        self.settings["bridge_enabled"] = True
        self.settings.save()
        self.refresh_bridge_label()
        code = self.bridge.new_pair_code()
        win = tk.Toplevel(self.root)
        win.title("配對瀏覽器腳本")
        win.geometry(f"{px(440)}x{px(300)}")
        self.float_dialog(win, transient=True)
        ttk.Label(win, text="在影片網頁按右下角「⬇ 下載」→「🖥 送到電腦版」，\n跳出配對視窗時輸入：",
                  justify="center").pack(pady=(18, 8))
        ttk.Label(win, text=f"{code[:3]} {code[3:]}", font=("Consolas", 32, "bold")).pack()
        left = ttk.Label(win, text="", foreground=C["muted"])
        left.pack(pady=8)
        ttk.Label(win, text="配對碼 5 分鐘內有效、只能用一次。\n配對過的瀏覽器之後就不用再輸入。",
                  foreground=C["muted"], justify="center").pack()
        ttk.Button(win, text="關閉", command=win.destroy).pack(pady=10)

        def tick():
            if not win.winfo_exists():
                return
            remain = int(self.bridge.pair_expires - time.time())
            if self.bridge.pair_code != code:
                left.config(text="✅ 配對完成！" if len(self.settings["bridge_tokens"] or []) else "配對碼已作廢")
                self.refresh_bridge_label()
                return
            if remain <= 0:
                left.config(text="配對碼已過期，請重新按「配對」")
                return
            left.config(text=f"剩下 {remain // 60}:{remain % 60:02d}")
            win.after(1000, tick)
        tick()

    def unpair_browsers(self):
        if messagebox.askyesno("取消所有配對", "確定要取消所有已配對的瀏覽器嗎？之後要用的話需要重新配對。"):
            n = len(self.settings["bridge_tokens"] or [])
            self.bridge.unpair_all()
            self.refresh_bridge_label()
            messagebox.showinfo("取消所有配對", f"✅ 已取消 {n} 個配對。\n瀏覽器下次「送到電腦版」時會要你重新輸入配對碼。")

    def stop_bridge(self):
        self.bridge.stop()
        self.settings["bridge_enabled"] = False
        self.settings.save()
        self.refresh_bridge_label()

    # ------------------------------------------------------------------
    # v1.36.0 新介面（webui/）：網頁畫面只讀 web_publish() 的狀態、只能叫 web_<動作>()（名稱白名單在 webui.ACTIONS）
    # ------------------------------------------------------------------
    WEB_ST = {RUNNING: "run", WAITING: "wait", RETRY_WAIT: "retry", PAUSED: "pause", DONE: "done",
              FAILED: "fail", NEEDS_ACTION: "need", CANCELLED: "cancel"}
    WEB_OLD_TABS = {"dl": 0, "hist": 1, "set": 2, "setup": 3}

    def float_dialog(self, win, transient=False):
        """小視窗：新介面時原本的視窗藏著，掛在它下面會一起藏起來 → 改成先跳到最上面"""
        if self.web and not self.web_fallback and self.root.state() == "withdrawn":
            win.attributes("-topmost", True)
            win.after(600, lambda: win.winfo_exists() and win.attributes("-topmost", False))
            win.after(50, lambda: win.winfo_exists() and (win.lift(), win.focus_force()))
        elif transient:
            win.transient(self.root)

    def web_attach(self):
        """新介面：原本的視窗藏起來當「舊介面」（還沒做好的頁面先用它）；網頁太久沒說好了就改用原本的視窗"""
        from webui import LOAD_TIMEOUT
        self.web.app = self
        self.web.thumb_dir = os.path.join(data_dir(), "thumbs")
        self._load_notices()
        self.root.withdraw()
        self.root.protocol("WM_DELETE_WINDOW", self.root.withdraw)   # 舊介面的 X 只是藏起來，主視窗是網頁
        self.web_publish()
        self.root.after(LOAD_TIMEOUT * 1000, self._web_watchdog)
        log.info("新介面啟動")

    def _web_watchdog(self):
        if self.web.loaded or self.web_fallback:
            return
        log.warning("新介面 %d 秒沒有回應 → 改用原本的視窗", int(time.time() - self._start_time))
        self.web_use_classic()
        messagebox.showinfo("新介面打不開", "新介面這次沒有成功打開，先用原本的視窗。\n"
                            "（③ 設定 可以取消「使用新介面」；錯誤日誌在 ⚙ 進階設定 → 問題排查）")

    def web_use_classic(self):
        """馬上換回原本的視窗（網頁視窗藏起來，程式結束時一起關）"""
        self.web_fallback = True
        self.web.hide()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.on_show_window(None)

    def add_notice(self, icon, text, page=""):
        """通知中心（新介面右上角 🔔）：最新的在前、留 30 則、存檔（重開還看得到）；page＝按了跳到哪一頁"""
        self.notices.insert(0, {"icon": icon, "text": str(text)[:160], "time": datetime.now().strftime("%m-%d %H:%M"),
                                "page": page if page in ("home", "dl", "set") else ""})
        del self.notices[30:]
        self._save_notices()

    def _notices_path(self):
        return os.path.join(data_dir(), "notices.json")

    def _save_notices(self):
        try:
            tmp = self._notices_path() + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.notices, f, ensure_ascii=False)
            os.replace(tmp, self._notices_path())
        except Exception:
            log.exception("存通知失敗")

    def _load_notices(self):
        try:
            with open(self._notices_path(), encoding="utf-8") as f:
                items = json.load(f)
            self.notices = [n for n in items if isinstance(n, dict) and isinstance(n.get("text"), str)][:30]
        except Exception:
            self.notices = []

    @staticmethod
    def _hist_key(r):
        return "h" + hashlib.sha1(f"{r.get('time')}|{r.get('url')}|{r.get('file')}".encode("utf-8", "replace")).hexdigest()[:12]

    def _hist_find(self, key):
        return next((r for r in self.history.items if self._hist_key(r) == key), None)

    def _web_nexus(self):
        """Nexus 卡片和數據卡用（3 秒算一次：要讀資料庫）"""
        now = time.time()
        c = getattr(self, "_web_nx", None)
        if c and now - c[0] < 3:
            return c[1]
        n = None
        svc = getattr(self, "nexus", None)
        if svc:
            try:
                devs = [d for d in svc.devices() if d["status"] in ("ACTIVE", "PENDING", "SUSPENDED")]
                act = [d for d in devs if d["status"] == "ACTIVE"]
                online = [d for d in act if d.get("connection_state") == "CONNECTED"]
                busy = 0
                if svc.running() and svc.tasks:
                    busy = sum(1 for t in svc.tasks.list(active_only=True)
                               if t.get("status") in ("RUNNING", "UPLOADING", "ASSIGNED", "ACCEPTED"))
                state = {"PENDING": "等核准", "SUSPENDED": "已暫停"}
                n = {"enabled": bool(svc.cfg["enabled"]), "running": svc.running(), "online": len(online), "total": len(act),
                     "tasks": busy,
                     "devices": [{"name": d["name"], "online": d in online,
                                  "state": state.get(d["status"]) or ("連線中" if d in online else "離線")} for d in devs[:6]]}
            except Exception as e:
                log.debug("新介面讀節點失敗：%r", e)
        self._web_nx = (now, n)
        return n

    def _web_disk(self):
        now = time.time()
        c = getattr(self, "_web_dk", None)
        if c and now - c[0] < 5:
            return c[1]
        import shutil
        d = self.settings.download_dir
        try:
            free = shutil.disk_usage(d if os.path.isdir(d) else os.path.expanduser("~")).free
        except Exception:
            free = None
        self._web_dk = (now, (free, os.path.splitdrive(d)[0] or d))
        return self._web_dk[1]

    def web_publish(self):
        """把現在的狀態整理成一份 JSON 給網頁（整份換掉，網頁執行緒只讀）"""
        if not self.web:
            return
        now = time.time()
        live = self.live[1] if self.live and now - self.live[0] < 3 else None
        tasks = []
        for t in self.queue.tasks:
            st = self.WEB_ST.get(t["status"], "wait")
            if st == "done" and self.settings["keep_history"]:
                continue                                   # 下載紀錄那邊會有
            item = {"key": f"q{t['id']}", "title": t["title"], "site": site_of(t["url"]),
                    "quality": quality_label(t["fmt"]["height"]), "size": fmt_size(t["size"]) if t.get("size") else "",
                    "st": st, "progress": round(t.get("progress") or 0, 1), "cancel": st == "run", "remove": st != "run"}
            if st == "run" and live:
                item["speed"] = f"{live * 8 / 1e6:.1f} Mbps"
            if st == "retry":
                item["label"] = f"{max(0, int(t.get('retry_at', 0) - now))} 秒後重試"
            if t.get("note") and st in ("fail", "need"):
                item["note"] = str(t["note"])[:80]
            tasks.append(item)
        ver = getattr(self.history, "version", 0)
        if getattr(self, "_web_hver", None) != ver or self.web.hist is None:   # 紀錄有變才重做（最多 500 筆）
            self._web_hver = ver
            hist, files, urls = [], {}, {}
            for r in self.history.items:
                st = self.WEB_ST.get(r.get("status"), "done")
                k = self._hist_key(r)
                hist.append({"key": k, "title": r.get("title") or r.get("url", ""), "site": site_of(r.get("url", "")),
                             "quality": quality_label(r.get("height")) if r.get("height") else "",
                             "size": fmt_size(r["size"]) if r.get("size") else "", "time": str(r.get("time", ""))[5:],
                             "st": st, "progress": 100 if st == "done" else 0, "open": st == "done" and bool(r.get("file")),
                             "node": bool(r.get("node")), "note": "" if st == "done" else str(r.get("error") or "")[:120]})
                if r.get("file"):
                    files[k] = r["file"]
                urls[k] = r.get("url", "")
            self.web.hist, self.web.files, self._web_urls = hist, files, urls     # 整份換掉（網頁執行緒只讀）
        tasks += self.web.hist[:40]
        run, wait = self.queue.count(RUNNING), self.queue.count(WAITING, RETRY_WAIT)
        today = datetime.now().strftime("%Y-%m-%d")
        done = [r for r in self.history.items if r.get("status") == DONE]
        n_today = sum(1 for r in done if str(r.get("time", "")).startswith(today))
        paused = bool(self.queue_paused or self.queue.count(PAUSED))
        sub = "⏸ 已暫停" if paused else (f"目前 {live * 8 / 1e6:.1f} Mbps" if live else (f"排隊 {wait} 部" if wait else "沒有在下載"))
        nx = self._web_nexus()
        free, drive = self._web_disk()
        kpis = [
            {"icon": "⬇", "label": "進行中下載", "value": run + wait, "unit": "部", "sub": sub},
            {"icon": "✓", "label": "已完成下載", "value": len(done), "unit": "部", "sub": f"今天 {n_today} 部"},
            {"icon": "🖧", "label": "Nexus 節點在線", "value": f"{nx['online']} / {nx['total']}", "unit": "",
             "sub": "Master 執行中" if nx["running"] else "Master 未啟動"} if nx and nx["enabled"] else
            {"icon": "☀", "label": "今天下載", "value": n_today, "unit": "部", "sub": "沒開 Nexus 節點"},
            {"icon": "💾", "label": "可用空間", "value": "—" if free is None else (f"{free / 1e9:.0f}" if free >= 1e11 else f"{free / 1e9:.1f}"),
             "unit": "" if free is None else "GB", "sub": f"下載資料夾在 {drive}"},
        ]
        self.web.state = {
            "version": VERSION, "theme": self.theme, "paused": paused, "bridge": bool(self.bridge.server),
            "kpis": kpis, "tasks": tasks, "nexus": nx, "notices": list(self.notices),
            "update": {"tag": self.update_info[0]} if self.update_info else None, "ask": self._web_ask,
            "hist_ver": ver, "analysis": self._web_an, "batch": self.batch_running,
            "qualities": QUALITY_CHOICES, "default_quality": self.settings["default_quality"],
            "rate_limit": self.settings["rate_limit_mbps"] or 0,
            "settings": self._web_settings(), "setup": self._web_setup_state(),
            "start_page": "setup" if getattr(self, "_first_run", False) else "",
        }
        # Nexus 管理頁開著（網頁 5 秒內問過）才讀資料庫，2 秒一次；在 Tk 執行緒做（資料庫連線不跨執行緒）
        if now - self.web.want_nexus < 5 and now - getattr(self, "_web_nx_full_t", 0) >= 2:
            self._web_nx_full_t = now
            try:
                self.web.nexus_full = self._web_nexus_full()
            except Exception as e:
                log.exception("新介面讀節點")
                self.web.nexus_full = {"ok": False, "err": f"讀不到節點資料：{type(e).__name__}"}

    def on_web_action(self, data):
        name, arg, box, ev = data
        try:
            box["reply"] = getattr(self, "web_" + name)(arg) or {"ok": True}
        except Exception as e:
            log.exception("新介面動作 %s", name)
            box["reply"] = {"ok": False, "msg": f"出錯了：{type(e).__name__}"}
        finally:
            ev.set()
        if not self.quitting:
            try:
                self.web_publish()
            except Exception:
                pass

    def web_add(self, text, quality=None):
        urls = []
        for line in str(text).splitlines():
            u = line.strip()
            if u.startswith(("http://", "https://")) and u not in urls:
                urls.append(u)
        if not urls:
            return {"ok": False, "msg": "沒有看到網址（一行一個，http 開頭）"}
        if len(urls) > 50:
            return {"ok": False, "msg": "一次最多 50 個網址"}
        if self.batch_running:
            return {"ok": False, "msg": "上一批網址還在讀取，等一下再加"}
        q = quality if quality in QUALITY_CHOICES else self.settings["default_quality"]
        self.start_batch(urls, q)
        return {"ok": True, "msg": f"已加入 {len(urls)} 部，讀取網址中…（畫質：{quality_label(None) if q == '最高' else q + 'p'}以內最高的）"}

    def web_paste(self, _):
        try:
            clip = self.root.clipboard_get()
        except Exception:
            clip = ""
        r = self.web_add(clip)
        if not r["ok"] and "沒有看到網址" in r["msg"]:
            r["msg"] = "剪貼簿裡沒有網址：先複製影片網址再按一次"
        return r

    def web_toggle_pause(self, _):
        self.toggle_pause()
        return {"ok": True, "msg": "▶ 繼續下載" if not self.queue_paused else "⏸ 已暫停（已下載的部分會保留）"}

    def web_retry_failed(self, _):
        n = self.queue.count(FAILED, NEEDS_ACTION)
        self.retry()
        return {"ok": True, "msg": f"重新排隊 {n} 部" if n else "沒有失敗的任務"}

    def _web_qtask(self, key):
        if not (isinstance(key, str) and key.startswith("q") and key[1:].isdigit()):
            return None
        return self.queue.get(int(key[1:]))

    def web_cancel(self, key):
        t = self._web_qtask(key)
        if not t or t["status"] != RUNNING or not self.cancel_event:
            return {"ok": False, "msg": "這個已經不在下載了"}
        self.stop_mode = "cancel"
        self.cancel_event.set()
        return {"ok": True, "msg": "取消中…"}

    def web_remove(self, key):
        t = self._web_qtask(key)
        if not t:
            return {"ok": False, "msg": "找不到這個任務"}
        if t["status"] == RUNNING:
            return {"ok": False, "msg": "正在下載，要停止請按 ✕"}
        if t["status"] == PAUSED and t.get("partials"):
            cleanup_partials(t["partials"], include_complete=True)
        self.queue.remove(t["id"])
        self.refresh_queue()
        self.set_buttons()
        return {"ok": True}

    def _web_file(self, key):
        r = self._hist_find(key)
        f = r and r.get("file")
        if not f or not os.path.isfile(f):
            return None
        return f

    def web_play(self, key):
        f = self._web_file(key)
        if not f:
            return {"ok": False, "msg": "檔案不見了（可能被移動或刪除）"}
        play_file(f)                       # v1.36.1：原本誤用 open_path → 只打開資料夾
        return {"ok": True, "msg": "用預設播放器打開…"}

    def web_reveal(self, key):
        f = self._web_file(key)
        if not f:
            return {"ok": False, "msg": "檔案不見了（可能被移動或刪除）"}
        open_path(f, select=True)
        return {"ok": True}

    def web_open_folder(self, _):
        open_path(self.settings.download_dir)

    def web_old(self, page):
        """還沒做好的頁面：打開原本的視窗（舊介面）到那一頁；進階設定的頁只開進階設定那個小視窗"""
        if page in ("nodes", "tasks", "security", "stats"):
            self.show_advanced(self.adv_pages[page])
            return {"ok": True, "msg": "這頁還在做，先用原本的視窗打開"}
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass
        if page in self.WEB_OLD_TABS:
            self.nav(self.WEB_OLD_TABS[page])
        return {"ok": True, "msg": "這頁還在做，先用原本的視窗打開"}

    # ---- v1.37.0 下載管理頁 ----
    def web_add_batch(self, arg):
        return self.web_add(arg["text"], arg.get("quality"))

    def web_analyze(self, url, want_height=None):
        """讀取一個網址的畫質清單（跟 ① 的「讀取」一樣），結果放在狀態的 analysis"""
        url = str(url).strip()
        if not url.startswith(("http://", "https://")):
            return {"ok": False, "msg": "網址要 http 開頭"}
        if self._web_an and self._web_an["state"] == "loading":
            return {"ok": False, "msg": "上一個網址還在讀取"}
        aid = str(time.time_ns())
        self._web_an = {"id": aid, "url": url, "state": "loading"}
        self._web_an_raw = None
        self._web_want = want_height

        def work():
            try:
                self.q.put(("web_analyzed", (aid, url) + tuple(self.analyze_fn(url))))
            except Exception as e:
                log.exception("新介面讀取失敗：%s", log_url(url))
                self.q.put(("web_analyzed", (aid, url, None, 0, None, str(e))))
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True}

    def on_web_analyzed(self, data):
        aid, url, title, dur, fmts, mbps = data
        if not self._web_an or self._web_an["id"] != aid:
            return
        if fmts is None:
            self._web_an = {"id": aid, "url": url, "state": "error", "err": f"讀取失敗：{mbps}"[:300]}
            return
        if mbps:
            self.remember_speed(mbps)
        if not fmts:
            self._web_an = {"id": aid, "url": url, "state": "error", "title": title, "err": "找不到可下載的畫質"}
            return
        site = site_of(url)
        tf, _ = self.stats.factor("time", site)
        sf, _ = self.stats.factor("size", site)
        rows = []
        for i, f in enumerate(fmts):
            size = f["size"] * (sf if (sf and not f["exact"]) else 1) if f["size"] else 0
            raw_t = f["size"] * 8 / (self.mbps * 1e6) if f["size"] and self.mbps else None
            rows.append({"i": i, "label": quality_label(f["height"]),
                         "size": ("" if f["exact"] or not size else "約 ") + (fmt_size(size) if size else "大小未知"),
                         "time": fmt_time(raw_t * (tf or 1)) if raw_t else "時間未知", "kind": "MP4 直連" if f["direct"] else "切片"})
        want = self._web_want
        pick = pick_default(fmts, (str(want) if want else "最高") if want is not None else self.settings["default_quality"])
        old = find_downloaded(self.history.items, url)
        self._web_an_raw = (url, title, fmts)
        self._web_an = {"id": aid, "url": url, "state": "ok", "title": title, "duration": fmt_duration(dur),
                        "formats": rows, "pick": pick, "site": site,
                        "downloaded": f"{old.get('time', '')} 下載過（{quality_label(old.get('height'))}）" if old else ""}

    def web_enqueue_fmt(self, arg):
        an, raw = self._web_an, getattr(self, "_web_an_raw", None)
        if not an or an["id"] != arg["id"] or not raw or not 0 <= arg["i"] < len(raw[2]):
            return {"ok": False, "msg": "畫質清單已經過期，請重新讀取"}
        url, title, fmts = raw
        f = fmts[arg["i"]]
        if self.queue.is_duplicate(url, f["height"]):
            return {"ok": False, "msg": "這部影片的這個畫質已經在排隊、下載中或暫停了"}
        self.enqueue({"url": url, "title": title, "fmt": f, "size": f["size"], "out": self.settings.download_dir})
        self._web_an = None
        return {"ok": True, "msg": f"已加入佇列：{quality_label(f['height'])}"}

    def web_clear_analysis(self, _):
        self._web_an = None

    def web_clear_finished(self, _):
        n = len(self.queue.tasks)
        self.queue.clear_finished()
        self.refresh_queue()
        n -= len(self.queue.tasks)
        return {"ok": True, "msg": f"清掉 {n} 筆已結束的" if n else "沒有已結束的任務"}

    def _web_recs(self, keys):
        keys = set(keys)
        return [r for r in self.history.items if self._hist_key(r) in keys]

    def web_redownload(self, key):
        r = self._hist_find(key)
        if not r or not r.get("url"):
            return {"ok": False, "msg": "找不到這筆紀錄"}
        return self.web_analyze(r["url"], want_height=r.get("height") or 0)

    def web_copy_url(self, key):
        r = self._hist_find(key)
        if not r or not r.get("url"):
            return {"ok": False, "msg": "找不到這筆紀錄"}
        self.root.clipboard_clear()
        self.root.clipboard_append(r["url"])
        return {"ok": True, "msg": "網址已複製"}

    def web_subtitles(self, keys):
        files = [r.get("file") for r in self._web_recs(keys) if r.get("status") == DONE]
        files = [f for f in files if f and os.path.isfile(f)]
        if not files:
            return {"ok": False, "msg": "選的影片裡沒有「下載完成、檔案還在」的"}
        self.subtitle_dialog(files=files)
        return {"ok": True, "msg": f"AI 字幕：{len(files)} 部（設定視窗已打開）"}

    def web_delete_hist(self, keys):
        keys = set(keys)
        before = len(self.history.items)
        self.history.items = [r for r in self.history.items if self._hist_key(r) not in keys]
        n = before - len(self.history.items)
        if n:
            self.history.save()
            self.refresh_history()
        return {"ok": True, "msg": f"刪除 {n} 筆紀錄（影片檔案不會刪）"}

    # ---- v1.38.0 設定頁、安裝擋廣告頁 ----
    WEB_BOOL_SETTINGS = ("skip_downloaded", "tray_always", "keep_history", "keep_stats")

    def _web_settings(self):
        """設定頁要顯示的值（金鑰本身不送出去，只說有沒有設定）"""
        st = self.settings
        lbl = lambda w: getattr(self, w).cget("text") if hasattr(self, w) else ""  # noqa: E731
        return {
            "download_dir": st.download_dir, "default_dir": st["download_dir"] in ("", None),
            "default_quality": st["default_quality"], "rate_limit": st["rate_limit_mbps"] or 0,
            "skip_downloaded": bool(st["skip_downloaded"]), "smart_segments": st["smart_segments"],
            "smart_modes": [[v, k] for k, v in SMART_MODES.items()],
            "filename_template": st["filename_template"] if st["filename_template"] in FILENAME_TEMPLATES else DEFAULT_TEMPLATE,
            "templates": list(FILENAME_TEMPLATES), "tpl_preview": preview_filename(st["filename_template"]),
            "theme": st["theme"], "themes": [[v, k] for k, v in THEMES.items()],
            "close_action": st["close_action"], "close_choices": CLOSE_CHOICES if pystray else [c for c in CLOSE_CHOICES if "系統匣" not in c],
            "tray_always": bool(st["tray_always"]), "tray_ok": pystray is not None,
            "keep_history": bool(st["keep_history"]), "keep_stats": bool(st["keep_stats"]),
            "bridge": lbl("bridge_lbl"), "bridge_on": bool(self.bridge.server), "paired": len(st["bridge_tokens"] or []),
            "blocklist": lbl("bl_lbl"), "ai": lbl("ai_lbl"), "ai_has_key": bool((st["ai_key"] or "").strip()),
            "mbps": round(self.mbps, 1) if self.mbps else 0, "data_dir": data_dir(),
        }

    def _web_setup_state(self):
        return {"chrome": bool(getattr(self, "chrome", None)), "done": sorted(getattr(self, "_web_steps", set()))}

    def web_set(self, arg):
        """設定頁改一個值：每個值都檢查過才存；原本視窗上的選項也跟著改"""
        k, v = arg["key"], arg["value"]
        st = self.settings
        if k == "default_quality" and v in QUALITY_CHOICES:
            st[k] = v
        elif k == "rate_limit" and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 10000:
            st["rate_limit_mbps"] = float(v)
            THROTTLE.set_mbps(float(v))
            self.refresh_queue()
        elif k in self.WEB_BOOL_SETTINGS and isinstance(v, bool):
            st[k] = v
            if k == "tray_always":
                if v:
                    self.start_tray()
                elif self.tray and not self.is_hidden():          # 縮在系統匣時不能拿掉，不然找不回來
                    self._stop_tray()
        elif k == "smart_segments" and v in SMART_MODES.values():
            st[k] = v
        elif k == "filename_template" and v in FILENAME_TEMPLATES:
            st[k] = v
        elif k == "close_action" and v in CLOSE_CHOICES and (pystray or "系統匣" not in v):
            st[k] = v
        elif k == "log_keep_days" and type(v) is int and v in (0, 3, 7, 14, 30):
            st[k] = v
        elif k == "theme" and v in THEMES.values():
            self.theme_var.set(next(n for n, m in THEMES.items() if m == v))
            self.change_theme()                                      # 會存檔、換原本視窗的顏色
        else:
            return {"ok": False, "msg": "這個值不能用"}
        st.save()
        try:
            self.apply_settings_to_ui()
            self.theme_var.set(next((n for n, m in THEMES.items() if m == st["theme"]), "跟系統"))
            self.tray_var.set(bool(st["tray_always"]))
        except Exception:
            log.exception("同步原本視窗的設定")
        return {"ok": True, "msg": "已儲存"}

    class _DialogHost:
        """新介面時原本的視窗藏著：要跳 Windows 對話框（選資料夾、存檔）時，暫時把它變成一個看不見的 1×1 小視窗擺最上面當主人，
        對話框才會出現在新介面前面；關掉對話框就還原"""

        def __init__(self, app):
            self.app = app

        def __enter__(self):
            r = self.app.root
            self.hidden = self.app.web and not self.app.web_fallback and r.state() == "withdrawn"
            if self.hidden:
                self.geo = r.geometry()
                r.attributes("-alpha", 0.0)
                r.geometry(f"1x1+{r.winfo_screenwidth() // 2}+{r.winfo_screenheight() // 3}")
                r.deiconify()
                r.attributes("-topmost", True)
                r.update()
            return self

        def __exit__(self, *exc):
            if self.hidden:
                r = self.app.root
                r.withdraw()
                r.attributes("-topmost", False)
                r.geometry(self.geo)
                r.attributes("-alpha", 1.0)
                unlayer(r)
            return False

    def web_choose_dir(self, _):
        with self._DialogHost(self):
            d = filedialog.askdirectory(initialdir=self.settings.download_dir or default_download_dir(), title="選下載資料夾")
        if not d:
            return {"ok": True}
        return self._web_set_dir(d)

    def _web_set_dir(self, d):
        if not os.path.isdir(d):
            return {"ok": False, "msg": "這個資料夾不能用"}
        self.settings["download_dir"] = "" if os.path.normpath(d) == os.path.normpath(default_download_dir()) else d
        self.settings.save()
        self.dir_var.set(self.settings.download_dir)
        self._web_dk = None
        return {"ok": True, "msg": "下載資料夾已更新"}

    def web_reset_dir(self, _):
        return self._web_set_dir(default_download_dir())

    def web_smart_report(self, _):
        return {"ok": True, "report": smart_report()}

    def web_unpair_all(self, _):
        n = len(self.settings["bridge_tokens"] or [])
        self.bridge.unpair_all()
        self.refresh_bridge_label()
        return {"ok": True, "msg": f"已取消 {n} 個配對"}

    def web_stop_bridge(self, _):
        self.stop_bridge()
        return {"ok": True, "msg": "瀏覽器連動已關閉（下次按「配對瀏覽器」會再打開）"}

    def web_update_blocklist(self, _):
        self.update_blocklist()
        return {"ok": True, "msg": "更新廣告清單中…"}

    def web_set_ai_key(self, key):
        key = key.strip()
        if key == "-":                                             # 網頁送 "-" ＝清除
            key = ""
        if key and not re.fullmatch(r"[A-Za-z0-9_\-]{20,200}", key):
            return {"ok": False, "msg": "金鑰格式不對（應該是 sk-ant- 開頭的一長串）"}
        self.ai_key_var.set(key)
        self.save_ai_key()
        return {"ok": True, "msg": "金鑰已儲存（用 Windows 加密存在這台電腦）" if key else "金鑰已清除"}

    def web_test_ai(self, _):
        self.test_ai()
        return {"ok": True, "msg": "測試中…"}

    def web_clear_ai_cache(self, _):
        self.clear_ai_cache()

    def web_reset_speed(self, _):
        self.reset_speed()
        return {"ok": True, "msg": "網速紀錄已清除"}

    def web_export(self, _):
        with self._DialogHost(self):
            self.export_settings()

    def web_import(self, _):
        with self._DialogHost(self):
            self.import_settings()

    def web_open_backups(self, _):
        self.open_backups()

    def web_open_log(self, _):
        p = getattr(log, "log_path", None)
        if not (p and os.path.exists(p)):
            return {"ok": False, "msg": "目前還沒有日誌"}
        play_file(p) if sys.platform.startswith("win") else open_path(p, select=True)

    def web_open_data(self, _):
        open_path(data_dir())

    def web_setup_step(self, n):
        """④ 安裝擋廣告的三步（跟原本視窗的「打開」一樣，在 Chrome 打開對的頁面）"""
        urls = {"1": URL_VM_STORE, "2": URL_VM_SETTINGS, "3": URL_SCRIPT}
        url = urls.get(n)
        if not url:
            return {"ok": False, "msg": "沒有這一步"}
        if url.startswith("chrome://"):
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
        try:
            if getattr(self, "chrome", None):
                subprocess.Popen([self.chrome, url])
            else:
                webbrowser.open(url)
        except Exception:
            webbrowser.open(url)
        self._web_steps = getattr(self, "_web_steps", set()) | {n}
        if url.startswith("chrome://"):
            return {"ok": True, "msg": "如果 Chrome 沒有自動打開設定頁：在網址列按 Ctrl+V 貼上再按 Enter（網址已複製）"}
        return {"ok": True, "msg": "已在 Chrome 打開"}

    # ---- v1.39.0 Nexus 管理頁（跟「⚙ 進階設定 → 🖧 節點／📋 節點任務」做一樣的事，只呼叫 MasterService） ----
    NX_OPS = ("approve", "reject", "suspend", "resume", "revoke")
    NX_CANCELLABLE = ("CREATED", "QUEUED", "SCHEDULED", "DISPATCHED", "ACCEPTED", "PREPARING", "RUNNING", "UPLOADING")

    def _nx_addrs(self):
        from nexus.master.server import BindPolicy
        c = getattr(self, "_web_addrs", None)
        if not c or time.time() - c[0] > 30:
            c = self._web_addrs = (time.time(), BindPolicy.local_addresses())
        return list(c[1])

    def _web_nexus_full(self):
        svc = getattr(self, "nexus", None)
        if not svc:
            return {"ok": False, "err": "節點功能沒有載入（這版打包可能少了 cryptography 套件）"}
        from nexus.gui import devices_tab as dt
        from nexus.gui import tasks_tab as tt
        from nexus.master.server import BindPolicy
        on, cfg = svc.running, svc.cfg
        try:
            fp = svc.ca_fingerprint()[:16]
        except Exception:
            fp = ""
        rem = svc.registry.pair.remaining() if on and svc.registry else 0
        cur = str(cfg["bind_host"])
        addrs = self._nx_addrs()
        if cur not in addrs and BindPolicy.classify(cur) != "invalid":
            addrs.append(cur)
        order = {"PENDING": 0, "ACTIVE": 1, "SUSPENDED": 2}
        devs = sorted(svc.devices(), key=lambda d: d["created_at"] or "", reverse=True)
        devs = sorted(devs, key=lambda d: order.get(d["status"], 9))
        names = {d["device_id"]: d["name"] for d in devs}
        drows = []
        for d in devs[:60]:
            st, act = d["status"], d["status"] == "ACTIVE"
            iso = "—"
            if act:
                res = ((svc.capabilities(d["device_id"]) or {}).get("resources") or {})
                iso = ("基本（可下載）" if res.get("isolation") == "basic" else
                       "不能用：" + (res.get("isolation_reason") or "原因不明")[:40] if "isolation" in res else "—")
            drows.append({"id": d["device_id"], "name": d["name"], "status": st, "status_name": dt.STATE_NAME.get(st, st),
                          "conn": dt.CONN_NAME.get(d["connection_state"], d["connection_state"]) if act else "—",
                          "online": act and d["connection_state"] == "CONNECTED",
                          "seen": dt._ago(d["last_seen_at"]) if act else "—", "iso": iso, "created": dt._local(d["created_at"]),
                          "ip": d["source_ip_first"] or "—", "fp": d["public_key_fingerprint"][:16],
                          "ops": [o for o, ok in (("approve", st == "PENDING"), ("reject", st == "PENDING"), ("suspend", act),
                                                  ("resume", st == "SUSPENDED"), ("revoke", st in ("ACTIVE", "SUSPENDED"))) if ok and on]})
        trows = []
        ts = svc.task_view()
        if ts:
            for t in ts.list(active_only=False, limit=100):
                st = t["status"]
                text = tt.STATE_NAME.get(st, st)
                prog = None
                if st == "UPLOADING":
                    pr = svc.upload_progress(t["task_id"])
                    if pr and pr.get("disk_full"):
                        text = "傳回中（這台空間不夠，清出空間後自動繼續）"
                    elif pr:
                        prog = int(pr["ratio"] * 100)
                        text = f"傳回中 {prog}%（{tt.fmt_size(pr['received'])}／{tt.fmt_size(pr['size'])}）"
                if st == "FAILED" and t.get("error_code") in tt.RETURN_ERRORS:
                    text = "傳回失敗（檔案留在節點）"
                trows.append({"id": t["task_id"], "type": tt.TYPE_NAME.get(t["type"], t["type"]), "what": tt.summary_of(t),
                              "status": st, "text": text, "tag": tt.TAG.get(st, ""), "progress": prog,
                              "node": names.get(t["assigned_device_id"], "—") if t["assigned_device_id"] else "—",
                              "tries": f"{t['attempt_count']}/{t['max_attempts']}", "created": tt.local(t["created_at"]),
                              "cancel": on and st in self.NX_CANCELLABLE, "fail": on and st == "UNKNOWN",
                              "retry_return": on and st == "FAILED" and t.get("error_code") in tt.RETURN_ERRORS})
        from nexus.tasks import adapters
        types = sorted(adapters.ADAPTERS, key=lambda n: (not n.startswith("video."), n))
        from nexus.net import sites as nsites
        browser_only = sorted(h for h in nsites.page_hosts() if nsites.needs_browser(h))
        return {"ok": True, "running": on, "error": svc.error or "", "host": str(svc.host or ""), "port": svc.port,
                "cfg_host": cur, "cfg_port": int(cfg["port"]), "enabled": bool(cfg["enabled"]),
                "addrs": [[a, f"{a}（{dt.KIND_NAME.get(BindPolicy.classify(a), BindPolicy.classify(a))}）"] for a in addrs],
                "lan": BindPolicy.classify(cur) == "lan", "fp": fp,
                "pair": getattr(self, "_web_pair_code", "") if rem else "", "pair_left": rem,
                "devices": drows, "tasks": trows, "browser_only": browser_only,
                "types": [[n, tt.TYPE_NAME.get(n, n), adapters.get(n).description, adapters.get(n).default_runtime_seconds] for n in types]}

    def _nx(self):
        svc = getattr(self, "nexus", None)
        if not svc:
            raise RuntimeError("節點功能沒有載入")
        return svc

    def _nx_refresh(self):
        self._web_nx_full_t = 0                           # 下一次 publish 馬上重讀
        for tab in (getattr(self, "nodes_tab", None), getattr(self, "tasks_tab", None)):
            try:
                tab and tab.refresh()
            except Exception:
                pass

    def web_nx_enable(self, v):
        svc = self._nx()
        if v not in ("on", "off"):
            return {"ok": False, "msg": "參數不對"}
        if v == "on":
            try:
                svc.start()
            except Exception as e:
                self._nx_refresh()
                return {"ok": False, "msg": f"Master 啟動失敗：{e}"[:200]}
        else:
            svc.stop()
        svc.cfg["enabled"] = v == "on"
        svc.cfg.save()
        self._nx_refresh()
        return {"ok": True, "msg": "Master 已啟用" if v == "on" else "Master 已關閉（所有節點連線切斷）"}

    def web_nx_bind(self, arg):
        from nexus.master.server import BindPolicy
        svc = self._nx()
        host, port = str(arg.get("host") or ""), arg.get("port")
        if (host not in self._nx_addrs() and host != str(svc.cfg["bind_host"])) or BindPolicy.classify(host) == "invalid":
            return {"ok": False, "msg": "這個位址不能用"}
        if type(port) is not int or not 1024 <= port <= 65535:
            return {"ok": False, "msg": "埠要在 1024～65535 之間"}
        svc.cfg["bind_host"], svc.cfg["port"] = host, port
        svc.cfg.save()
        self._nx_refresh()
        return {"ok": True, "msg": "已儲存" + ("；Master 開著：關掉再打開才會換位址" if svc.running else "")}

    def web_nx_pair(self, _):
        svc = self._nx()
        if not svc.running:
            return {"ok": False, "msg": "要先啟用 Master，節點才連得進來"}
        self._web_pair_code = svc.registry.pair.new()
        self._nx_refresh()
        return {"ok": True}

    def web_nx_device(self, arg):
        svc = self._nx()
        op, did, reason = arg.get("op"), str(arg.get("id") or ""), str(arg.get("reason") or "").strip()
        if op not in self.NX_OPS or not svc.running:
            return {"ok": False, "msg": "現在不能這樣做"}
        if not reason:
            return {"ok": False, "msg": "沒有理由就不執行（會寫進稽核紀錄）"}
        try:
            getattr(svc.registry, op)(did, "owner", reason[:200])
        except Exception as e:
            return {"ok": False, "msg": getattr(e, "message", None) or str(e)}
        finally:
            self._nx_refresh()
        return {"ok": True, "msg": {"approve": "已核准", "reject": "已拒絕", "suspend": "已暫停", "resume": "已恢復",
                                    "revoke": "已撤銷（不能復原）"}[op]}

    def web_nx_task_new(self, arg):
        """新增節點任務（跟「📋 節點任務 → ＋ 新增任務」一樣）"""
        import uuid
        from nexus.tasks import adapters
        svc = self._nx()
        if not svc.running:
            return {"ok": False, "msg": "要先啟用 Master"}
        n = arg.get("type")
        if n not in adapters.ADAPTERS:
            return {"ok": False, "msg": "不認得的任務類型"}
        ret = {"return_to_master": True} if arg.get("ret") is True else {}
        try:
            if n == "video.download":
                q = arg.get("quality") if arg.get("quality") in ("best", "1080", "720", "480") else "best"
                params = {"url": str(arg.get("url") or "").strip(), "quality": q, **ret}
            elif n == "test.file":
                params = {"size_mb": int(arg.get("size_mb") or 20), **ret}
            elif n == "test.echo":
                params = {"text": str(arg.get("text") or "")}
            elif n == "test.sleep":
                params = {"seconds": int(arg.get("seconds") or 10)}
            else:
                params = {"code": str(arg.get("code") or "ADAPTER_ERROR")}
            spec = adapters.get(n)
            runtime = int(arg.get("runtime") or spec.default_runtime_seconds)
            tries = int(arg.get("tries") or 2)
            t = svc.tasks.create(n, params, actor="owner", idempotency_key=str(uuid.uuid4())[:8],
                                 max_runtime_seconds=runtime, max_attempts=tries)
        except (TypeError, ValueError):
            return {"ok": False, "msg": "數字格式不對"}
        except Exception as e:
            return {"ok": False, "msg": getattr(e, "message", None) or str(e)}
        finally:
            self._nx_refresh()
        if t["status"] == "REJECTED":
            return {"ok": False, "msg": "任務不合格：" + (t.get("error_message") or "內容不合格")[:200]}
        return {"ok": True, "msg": "已送出，排程器會選一台節點"}

    def _nx_task_op(self, arg, op):
        svc = self._nx()
        tid, reason = str(arg.get("id") or ""), str(arg.get("reason") or "").strip()
        if not svc.running:
            return {"ok": False, "msg": "要先啟用 Master"}
        if not reason:
            return {"ok": False, "msg": "沒有理由就不執行（會寫進稽核紀錄）"}
        try:
            r = getattr(svc.tasks, op)(tid, "owner", reason[:200])
        except Exception as e:
            return {"ok": False, "msg": getattr(e, "message", None) or str(e)}
        finally:
            self._nx_refresh()
        if op == "cancel" and r.get("status") == "CANCELLING":
            return {"ok": True, "msg": "已通知節點停止；節點確認停下後才會變成「已取消」"}
        return {"ok": True, "msg": "好了"}

    def web_nx_cancel(self, arg):
        return self._nx_task_op(arg, "cancel")

    def web_nx_fail(self, arg):
        return self._nx_task_op(arg, "mark_failed")

    def web_nx_retry_return(self, tid):
        try:
            self._nx().retry_return(tid)
        except Exception as e:
            return {"ok": False, "msg": getattr(e, "message", None) or str(e)}
        finally:
            self._nx_refresh()
        return {"ok": True, "msg": "節點上線時會自動接著傳（已經收到的部分不用重傳）"}

    def web_nx_detail(self, tid):
        """任務詳情：結果、為什麼選這台、時間軸"""
        from nexus.gui import tasks_tab as tt
        from nexus.tasks.scheduler import REASONS, WEIGHTS
        svc = self._nx()
        ts = svc.task_view()
        if not ts:
            return {"ok": False, "msg": "沒有任務資料"}
        try:
            d = ts.detail(tid)
        except Exception:
            return {"ok": False, "msg": "找不到這個任務"}
        t = d["task"]
        names = {x["device_id"]: x["name"] for x in svc.devices()}
        node = names.get(t["assigned_device_id"], "節點")
        res = ""
        if t["result_json"] and t["type"] in tt.FILE_TYPES:
            res = tt.DetailWindow._download_result(t["result_json"], node, t["status"])
            if t["error_message"]:
                res = f"錯誤：{t['error_code']}　{t['error_message'][:200]}\n" + res
        elif t["result_json"]:
            res = "結果：" + t["result_json"][:200]
        elif t["error_message"]:
            res = f"錯誤：{t['error_code']}　{t['error_message'][:200]}"
        why = []
        note = "還沒排程過。"
        if d["decisions"]:
            last = d["decisions"][-1]
            w = WEIGHTS.get(last["weights_version"], {})
            note = (f"最近一次排程：{tt.local(last['decided_at'])}（" + "、".join(f"{tt.SCORE_NAME[k]} {int(v * 100)}%" for k, v in w.items())
                    + "）。先排除不合格的節點，再替合格的打 0～100 分，最高分的拿到。"
                    + (f"這個任務一共排程過 {len(d['decisions'])} 次。" if len(d["decisions"]) > 1 else ""))
            for c in last["candidates"]:
                if c["eligible"]:
                    why.append([names.get(c["device_id"], c["name"]), "✔ 選中" if c["device_id"] == last["chosen_device_id"] else "合格",
                                f"{c['total']:.1f}", "　".join(f"{tt.SCORE_NAME[k]} {v:.2f}" for k, v in c["scores"].items())])
                else:
                    why.append([names.get(c["device_id"], c["name"]), "排除", "—", "；".join(REASONS.get(r, r) for r in c["reasons"])])
        rows = []
        for a in d["audit"]:
            st = a["action"].split(".", 1)[-1].upper()
            rows.append((a["ts"], tt._who(a["actor"], names), tt.STATE_NAME.get(st, a["action"]), a["reason"] or ""))
        for e in d["events"]:
            if e["type"] == "PROGRESS":
                try:
                    pct = int(json.loads(e["data_json"]).get("progress", 0) * 100)
                except Exception:
                    pct = 0
                rows.append((e["received_at"], names.get(e["device_id"], "節點"), "進度", f"{pct}%"))
        rows = [[tt.local(r[0]), *r[1:]] for r in sorted(rows, key=lambda x: x[0])][-200:]
        good = bool(t["result_json"]) and t["status"] in ("SUCCEEDED", "UPLOADING", "VERIFYING")
        return {"ok": True, "detail": {
            "head": f"{tt.TYPE_NAME.get(t['type'], t['type'])}｜{tt.STATE_NAME.get(t['status'], t['status'])}｜節點：{names.get(t['assigned_device_id'], '—')}"
                    f"｜嘗試 {t['attempt_count']}/{t['max_attempts']}｜{tt.summary_of(t)}",
            "result": res, "good": good, "note": note, "why": why, "timeline": rows}}

    def web_nx_audit(self, _):
        svc = self._nx()
        names = {d["device_id"]: d["name"] for d in svc.devices()}
        return {"ok": True, "audit": [[ev["ts"][:19].replace("T", " "), ev["action"], ev["actor"],
                                       names.get(ev["resource_id"], (ev["resource_id"] or "")[:12]), ev["outcome"], ev["reason"] or ""]
                                      for ev in svc.audit(200)]}

    # ---- v1.41.0 安全與統計頁（跟「⚙ 進階設定 → 🔒 安全／統計」一樣的內容） ----
    KNOWN_HOSTS = ("github.com", "githubusercontent.com", "huggingface.co", "hf.co", "hf-mirror.com", "anthropic.com")

    def web_sec_view(self, _):
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"  # noqa: E731
        meta = self.settings["bridge_meta"] or {}
        paired = [{"id": h[:16], "ua": (meta.get(h) or {}).get("ua") or "（舊版配對，沒有紀錄）",
                   "paired": fmt((meta.get(h) or {}).get("paired")), "last": fmt((meta.get(h) or {}).get("last")),
                   "uses": (meta.get(h) or {}).get("uses", 0)} for h in (self.settings["bridge_tokens"] or [])]
        names = {"classify": "判斷廣告網域", "translate": "翻譯字幕"}
        ai = [{"month": month, "items": [f"{names.get(k, k)} {v['calls']} 次（送 {v['in']:,}／收 {v['out']:,} token）" for k, v in m.items()]}
              for month, m in sorted((self.settings["ai_usage"] or {}).items(), reverse=True)[:6]]
        with _NET_LOCK:
            seen = sorted(NET_SEEN.items(), key=lambda x: -x[1]["n"])[:40]
        net = [{"host": h, "n": e["n"], "last": time.strftime("%H:%M:%S", time.localtime(e["last"])),
                "odd": not (any(h == d or h.endswith("." + d) for d in self.KNOWN_HOSTS)
                            or bridge_url_allowed("https://" + h + "/", BRIDGE_HOSTS + BRIDGE_MEDIA_HOSTS))} for h, e in seen]
        logp = os.path.join(data_dir(), "video_helper.log")
        size = sum(os.path.getsize(p) for p in [logp, logp + ".1", logp + ".2"] if os.path.isfile(p))
        if getattr(self, "_web_comp", None) is None:                 # 元件指紋要讀整個 EXE：背景算一次
            self._web_comp = "計算中…"
            threading.Thread(target=lambda: setattr(self, "_web_comp", component_report()), daemon=True).start()
        pct = lambda x: "—" if x is None else f"±{x * 100:.0f}%"  # noqa: E731
        rows = self.stats.summary()
        n, total = sum(r["count"] for r in rows), sum(r["bytes"] for r in rows)
        tf, tn = self.stats.factor("time")
        return {"ok": True, "sec": {
            "paired": paired, "bridge": f"開著（127.0.0.1:{self.bridge.port}，只有這台電腦連得到）" if self.bridge.server else "關閉",
            "ai": ai, "ai_key": bool((self.settings["ai_key"] or "").strip()), "net": net,
            "log_size": fmt_size(size), "log_keep": self.settings["log_keep_days"], "components": self._web_comp,
            "verify": getattr(self, "_web_verify", None),
            "stats": [[r["site"], r["count"], fmt_size(r["bytes"]), f"{r['mbps']:.1f} Mbps" if r["mbps"] else "—",
                       pct(r["time_err"]), pct(r["size_err"])] for r in rows],
            "stats_sum": (f"共下載 {n} 部｜總量 {fmt_size(total)}｜" + (f"整體來說實際時間是預估的 {tf:.2f} 倍（依最近 {tn} 次）"
                                                                    if tf else "累積 3 次以上就會開始校正預估")) if n else "還沒有統計。下載完成後會自動記錄。"}}

    def web_verify_self(self, _):
        if getattr(self, "_web_verify", None) and self._web_verify.get("state") == "running":
            return {"ok": False, "msg": "檢查中，等一下"}
        self._web_verify = {"state": "running", "text": "檢查中…（算指紋、問 GitHub）"}

        def work():
            try:
                res = verify_self()
            except Exception as e:
                log.exception("檢查 EXE")
                res = {"state": "error", "text": f"檢查失敗：{type(e).__name__}"}
            self._web_verify = res
            log.info("檢查 EXE：%s %s", res.get("state"), res.get("sha256", "")[:16])
        threading.Thread(target=work, daemon=True).start()
        return {"ok": True, "msg": "檢查中…"}

    def web_sec_unpair(self, hid):
        h = next((x for x in (self.settings["bridge_tokens"] or []) if x[:16] == hid), None)
        if not h:
            return {"ok": False, "msg": "找不到這個配對"}
        self.bridge.unpair(h)
        self.refresh_bridge_label()
        return {"ok": True, "msg": "已取消這個瀏覽器的配對"}

    def web_clear_logs(self, _):
        self.clear_logs(ask=False)
        return {"ok": True, "msg": "錯誤日誌已清空"}

    def web_clear_stats(self, _):
        self.stats.clear()
        self.refresh_stats()
        return {"ok": True, "msg": "統計已清除，預估回到沒有校正的狀態"}

    def web_pair(self, _):
        self.pair_browser()

    def web_check_update(self, _):
        threading.Thread(target=check_update, args=(self.q, True), daemon=True).start()
        return {"ok": True, "msg": "檢查更新中…"}

    def web_link(self, key):
        url = {"manual": f"https://{REPO.split('/')[0]}.github.io/{REPO.split('/')[1]}/manual/",
               "releases": f"https://github.com/{REPO}/releases",
               "attest": f"https://github.com/{REPO}/attestations",
               "update": self.update_info and self.update_info[1]}.get(key)
        if not url:
            return {"ok": False, "msg": "沒有這個連結"}
        webbrowser.open(url)

    def web_ask(self, title, text, buttons, done):
        aid = str(time.time_ns())
        self._web_ask = {"id": aid, "title": title, "text": text, "buttons": list(buttons)}
        self._web_ask_cb = (aid, done)
        self.web.show()
        self.web_publish()

    def web_answer(self, arg):
        aid, done = getattr(self, "_web_ask_cb", (None, None))
        if not done or arg["id"] != aid:
            return {"ok": False}
        self._web_ask = None
        self._web_ask_cb = (None, None)
        done(arg["choice"])

    def web_clear_notices(self, _):
        self.notices = []
        self._save_notices()

    def on_web_close(self, _):
        """按新介面右上角的 X（跟原本的 on_close 一樣的規則，只是用網頁對話框問）"""
        if self.web_fallback:
            return
        action = self.settings["close_action"]
        if pystray is None and action == "一律縮到系統匣":
            action = "下載中才詢問"
        if action == "一律縮到系統匣":
            self.hide_to_tray()
            return
        if not self.downloads_active():
            self.quit_app()
            return
        btns = (["縮到系統匣，繼續下載"] if pystray and action != "直接結束" else []) + ["取消下載並結束", "回到視窗"]

        def done(i):
            b = btns[i] if 0 <= i < len(btns) else ""
            if b.startswith("縮到"):
                self.hide_to_tray()
            elif b.startswith("取消"):
                self.quit_app()
        self.web_ask("還在下載", self.quit_warning() if action == "直接結束" else
                     "還有影片在下載，要怎麼做？\n縮到右下角的系統匣會繼續下載，完成會通知你。", btns, done)

    # ----- 關閉視窗、系統匣 -----

    def downloads_active(self):
        return bool(self.cancel_event) or self.queue.count(WAITING) > 0

    def on_close(self):
        """按視窗右上角的 X"""
        action = self.settings["close_action"]
        if pystray is None and action == "一律縮到系統匣":
            action = "下載中才詢問"                      # 沒有系統匣功能就退回詢問
        if action == "一律縮到系統匣":
            self.hide_to_tray()
            return
        if self.downloads_active():
            if action == "直接結束":
                if messagebox.askyesno("下載中", self.quit_warning()):
                    self.quit_app()
                return
            if pystray is None:
                if messagebox.askyesno("下載中", self.quit_warning()):
                    self.quit_app()
                return
            ans = messagebox.askyesnocancel(
                "還在下載",
                "還有影片在下載，要怎麼做？\n\n"
                "「是」：縮到右下角的系統匣，繼續下載（完成會通知你）\n"
                "「否」：取消下載並結束程式\n"
                "「取消」：回到視窗")
            if ans is True:
                self.hide_to_tray()
            elif ans is False:
                self.quit_app()
            return
        self.quit_app()

    def quit_warning(self):
        w = self.queue.count(WAITING)
        msg = "還在下載，確定要離開嗎？\n目前的下載會被取消，已下載的部分會被刪除。"
        return msg + (f"\n排隊中的 {w} 部也不會下載。" if w else "")

    def quit_app(self):
        """真的結束：取消下載、清殘檔、收掉系統匣圖示"""
        self.quitting = True
        for t in self.queue.tasks:
            if t["status"] == WAITING:
                t["status"] = CANCELLED
            if t["status"] == PAUSED and t.get("partials"):       # 結束程式：暫停的半成品刪掉（佇列不會保留）
                cleanup_partials(t["partials"], include_complete=True)
        self.stop_mode = "cancel"
        if self.cancel_event:
            self.cancel_event.set()
        if self.worker:
            self.worker.join(timeout=5)         # 等它清完殘檔
        if self.tray:
            try:
                self.tray.stop()
            except Exception:
                pass
        self.bridge.stop()
        if getattr(self, "nexus", None):
            try:
                self.nexus.close()            # 關 Master：所有節點連線斷掉、埠釋放
            except Exception:
                pass
        log.info("關閉")
        self.root.destroy()

    def start_tray(self):
        if self.tray or pystray is None:
            return self.tray is not None
        try:
            # 系統匣的按鈕是在另一個執行緒觸發的，一律透過 self.q 交給主畫面處理
            # v1.35.0：一直都在（設定可關）；右鍵選單多了狀態、暫停／繼續、打開下載資料夾、貼上網址下載
            M = pystray.MenuItem
            menu = pystray.Menu(
                M("打開 Video Helper", lambda: self.q.put(("show_window", None)), default=True),
                M(lambda item: self.tray_status, None, enabled=False),
                pystray.Menu.SEPARATOR,
                M(lambda item: "▶ 繼續全部下載" if self.tray_paused else "⏸ 暫停全部下載",
                  lambda: self.q.put(("tray_pause", None))),
                M("📋 貼上網址下載", lambda: self.q.put(("tray_paste", None))),
                M("📂 打開下載資料夾", lambda: self.q.put(("tray_folder", None))),
                pystray.Menu.SEPARATOR,
                M("結束", lambda: self.q.put(("tray_quit", None))))
            self.tray = pystray.Icon("VideoHelper", make_icon_image(), "Video Helper", menu)
            self.tray.run_detached()
            return True
        except Exception:
            log.exception("系統匣啟動失敗")
            self.tray = None
            return False

    def hide_to_tray(self):
        if not self.start_tray():
            self.root.iconify()                  # 系統匣不能用 → 至少縮到工作列
            return
        if self.web and not self.web_fallback:
            self.web.hide()
        else:
            self.root.withdraw()
        if not self.tray_hinted:
            self.tray_hinted = True
            self.notify("Video Helper 在背景執行中" + ("，下載會繼續" if self.downloads_active() else "") +
                        "。點右下角的圖示可以打開視窗。")
        log.info("縮到系統匣")

    def is_hidden(self):
        if self.web and not self.web_fallback:
            return self.web.hidden
        try:
            return self.root.state() == "withdrawn"
        except Exception:
            return False

    def notify(self, msg):
        if self.tray:
            try:
                self.tray.notify(msg, "Video Helper")
            except Exception:
                pass

    def _stop_tray(self):
        """視窗被關掉時一定要收掉系統匣小圖示：它的執行緒還在，程式就結束不了（測試曾因此卡住 15 分鐘）"""
        t, self.tray = self.tray, None
        if t:
            try:
                t.stop()
            except Exception:
                pass

    def update_tray(self):
        """把下載狀態寫到系統匣的提示文字和選單（每秒由 poll 呼叫；沒變就不動）"""
        if not self.tray:
            return
        run = self.queue.running()
        waiting = self.queue.count(WAITING)
        paused = bool(self.queue_paused or self.queue.count(PAUSED))
        if run:
            st = f"下載中 {run.get('progress', 0):.0f}%" + (f"（還有 {waiting} 部）" if waiting else "")
        elif paused:
            st = "⏸ 已暫停" + (f"（{waiting} 部等待中）" if waiting else "")
        else:
            st = f"{waiting} 部等待中" if waiting else "沒有在下載"
        if (st, paused) != (self.tray_status, self.tray_paused):
            self.tray_status, self.tray_paused = st, paused
            try:
                self.tray.title = "Video Helper：" + st
                self.tray.update_menu()
            except Exception:
                pass

    def on_tray_pause(self, _):
        self.toggle_pause()
        self.update_tray()

    def on_tray_folder(self, _):
        open_path(self.settings.download_dir)

    def on_tray_paste(self, _):
        """系統匣「貼上網址下載」：剪貼簿裡每行一個 http 網址，用預設畫質加入佇列（跟 ① 的批次一樣）"""
        try:
            clip = self.root.clipboard_get()
        except Exception:
            clip = ""
        urls = []
        for line in clip.splitlines():
            u = line.strip()
            if u.startswith("http") and u not in urls:
                urls.append(u)
        if not urls:
            try:
                self.tray.notify("剪貼簿裡沒有網址：先複製影片網址再按一次", "Video Helper")
            except Exception:
                pass
            return
        self.start_batch(urls)
        try:
            self.tray.notify(f"已加入 {len(urls)} 部影片", "Video Helper")
        except Exception:
            pass

    def on_show_window(self, _):
        if self.web and not self.web_fallback:
            self.web.show()
            return
        self.root.deiconify()
        self.root.lift()
        try:
            self.root.focus_force()
        except Exception:
            pass

    def on_tray_quit(self, _):
        if self.downloads_active():
            self.on_show_window(None)
            if not messagebox.askyesno("下載中", self.quit_warning()):
                return
        self.quit_app()

    # ------------------------------------------------------------------
    # ③ 下載紀錄
    # ------------------------------------------------------------------

    def build_history(self, parent):
        pad = {"padx": 10, "pady": 6}
        cols = ("time", "title", "quality", "status")
        self.htree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="extended")   # 可以 Ctrl／Shift 多選（批次字幕）
        for c, name, w, anchor in (("time", "時間", 130, "center"), ("title", "標題", 380, "w"),
                                   ("quality", "畫質", 70, "center"), ("status", "狀態", 70, "center")):
            self.htree.heading(c, text=name)
            self.htree.column(c, width=w, anchor=anchor)
        self.htree.pack(fill="both", expand=True, **pad)
        self.style_tree(self.htree)
        self.htree.bind("<<TreeviewSelect>>", lambda e: self.show_history_detail())
        self.htree.bind("<Double-1>", lambda e: self.history_double())
        self.add_menu(self.htree, [("▶ 播放", self.history_play), ("📂 打開檔案位置", self.history_open),
                                   ("📝 產生 AI 字幕…", self.subtitle_dialog),
                                   ("↻ 重新下載", self.history_redownload), None,
                                   ("刪除這筆紀錄", self.history_delete), ("清除全部紀錄", self.history_clear)])

        self.hdetail = ttk.Label(parent, text="", foreground=C["muted"], wraplength=680, justify="left")
        self.hdetail.pack(fill="x", **pad)

        bar = ttk.Frame(parent)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="📂 打開檔案位置", command=self.history_open).pack(side="left")
        ttk.Button(bar, text="打開下載資料夾", command=lambda: open_path(self.settings.download_dir)).pack(side="left", padx=6)
        ttk.Button(bar, text="📝 AI 字幕", command=self.subtitle_dialog).pack(side="left")
        ttk.Label(bar, text="雙擊播放；按右鍵有更多選項", foreground=C["muted"]).pack(side="right")
        self.refresh_history()

    def refresh_history(self):
        self.htree.delete(*self.htree.get_children())
        icon = {"完成": "✅ 完成", "失敗": "❌ 失敗", "取消": "■ 取消"}
        for i, r in enumerate(self.history.items):
            title = ("🖧 " if r.get("node") else "") + r.get("title", "")          # V4：節點傳回來的標一下
            self.htree.insert("", "end", iid=str(i), tags=({"失敗": "bad"}.get(r.get("status"), ""), "odd" if i % 2 else "even"), values=(
                r.get("time", ""), title, quality_label(r.get("height")), icon.get(r.get("status"), r.get("status", ""))))
        if not self.history.items:
            self.hdetail.config(text="還沒有紀錄。" if self.settings["keep_history"] else "已關閉下載紀錄（可在「③ 設定」→ 進階設定打開）。")

    def selected_history(self):
        sel = self.htree.selection()
        return (int(sel[0]), self.history.items[int(sel[0])]) if sel else (None, None)

    def show_history_detail(self):
        _, r = self.selected_history()
        if not r:
            return
        lines = [f"網址：{r.get('url', '')}"]
        if r.get("file"):
            lines.append(f"檔案：{r['file']}" + ("" if os.path.exists(r["file"]) else "（已不存在）"))
        if r.get("error"):
            lines.append(f"失敗原因：{r['error']}")
        if r.get("node"):
            lines.append(f"來自節點：{r['node']}（已比對 SHA-256）")
        self.hdetail.config(text="\n".join(lines))

    def history_redownload(self):
        _, r = self.selected_history()
        if not r or self.analyzing:
            return
        self.url_var.set(r.get("url", ""))
        self.want_height = r.get("height")
        self.nb.select(self.tab_dl)
        self.analyze()

    def history_open(self):
        _, r = self.selected_history()
        if not r:
            return
        f = r.get("file")
        if f and os.path.exists(f):
            open_path(f, select=True)
        else:
            messagebox.showinfo("找不到檔案", "這筆紀錄的檔案已經不存在（可能被移動或刪除了）。")

    def history_play(self):
        """用電腦預設的播放器打開影片"""
        _, r = self.selected_history()
        if not r:
            return
        f = r.get("file")
        if not (f and os.path.exists(f)):
            messagebox.showinfo("找不到檔案", "這筆紀錄的檔案已經不存在（可能被移動或刪除了）。")
            return
        play_file(f)

    def history_double(self):
        """雙擊：完成的就播放，失敗／取消的就重新下載"""
        _, r = self.selected_history()
        if not r:
            return
        if r.get("file") and os.path.exists(r["file"]):
            self.history_play()
        else:
            self.history_redownload()

    def history_delete(self):
        i, r = self.selected_history()
        if r is not None:
            self.history.remove(i)
            self.refresh_history()
            self.hdetail.config(text="")

    def history_clear(self):
        if self.history.items and messagebox.askyesno("清除全部", "確定要清除所有下載紀錄嗎？（不會刪除已下載的影片）"):
            self.history.clear()
            self.refresh_history()

    # ------------------------------------------------------------------
    # 🔒 安全（資安控制中心）：讓使用者自己看得到程式的安全狀態
    # ------------------------------------------------------------------

    def note_ai_usage(self, kind, usage):
        """AI 每次回答後記一筆（只記次數、字數；在背景執行緒呼叫，存檔很快）"""
        month = time.strftime("%Y-%m")
        u = dict(self.settings["ai_usage"] or {})
        m = dict(u.get(month) or {})
        k = dict(m.get(kind) or {"calls": 0, "in": 0, "out": 0})
        k["calls"] += 1
        k["in"] += int(usage.get("input_tokens") or 0)
        k["out"] += int(usage.get("output_tokens") or 0)
        m[kind] = k
        u[month] = m
        self.settings["ai_usage"] = dict(sorted(u.items())[-12:])     # 只留 12 個月
        self.settings.save()

    def build_nodes_tab(self, page):
        """🖧 節點（Nexus Master）：畫面在 nexus/gui/devices_tab.py；這裡只負責建 MasterService、程式結束時關掉。
        `master.enabled` 預設 false → 平常不開任何埠；只有使用者在這頁勾了才啟動（啟動時勾著就自動開）"""
        self.nexus = None
        self.nodes_tab = None
        try:
            from nexus.gui.devices_tab import DevicesTab
            from nexus.master.service import MasterService
            self.nexus = MasterService()
            try:                                           # v1.33.2：拖動視窗時節點分頁先不重整
                from nexus.gui import devices_tab as _dt, tasks_tab as _tt
                _dt.UI_BUSY = _tt.UI_BUSY = ui_busy
            except Exception:
                pass
            # V4：節點傳回的影片放進 ① 下載影片 同一個資料夾、上傳照 ③ 的下載限速、收完寫進 ② 下載紀錄（D4、D5）
            self.nexus.dest_dir = lambda: self.settings.download_dir
            self.nexus.rate_limit_mbps = lambda: self.settings["rate_limit_mbps"] or 0
            self.nexus.on_returned = lambda t, r: self.q.put(("node_returned", (t, r)))
            if self.nexus.cfg["enabled"]:
                try:
                    self.nexus.start()
                except Exception as e:
                    log.warning("Nexus Master 自動啟動失敗：%s", e)
            self.nodes_tab = DevicesTab(page, self.nexus, self.settings, C, px)
        except Exception as e:
            log.warning("節點分頁無法載入：%r", e)
            ttk.Label(page, text=f"節點功能無法載入：{type(e).__name__}\n（這版打包可能少了 cryptography 套件）",
                      foreground=C["bad"], justify="left").pack(anchor="w")

    def on_node_returned(self, data):
        """V4：節點把影片傳回來了（Master 已驗證大小和 SHA-256）→ 寫進 ② 下載紀錄"""
        t, r = data
        if not self.settings["keep_history"]:
            return
        try:
            params = json.loads(t["manifest_json"]).get("params", {})
        except Exception:
            params = {}
        node = ""
        try:
            node = next((d["name"] for d in self.nexus.devices() if d["device_id"] == t["assigned_device_id"]), "")
        except Exception:
            pass
        for f in r.get("files", []):
            title = r.get("title") or params.get("title") or os.path.splitext(f.get("name", ""))[0]
            self.history.add(title=title, url=params.get("url", ""), height=r.get("height"), status=DONE,
                             file=f.get("path", ""), size=f.get("size") or 0, error="", node=node or "節點")
            if self.web:
                self.add_notice("🖧", f"節點傳回：{title[:60]}" + (f"（{node}）" if node else ""), "dl")
        self.refresh_history()

    def build_tasks_tab(self, page):
        """📋 節點任務（Nexus V2）：畫面在 nexus/gui/tasks_tab.py；用 build_nodes_tab 建好的 self.nexus"""
        self.tasks_tab = None
        if not getattr(self, "nexus", None):
            ttk.Label(page, text="節點功能沒有載入，這頁不能用。", foreground=C["bad"]).pack(anchor="w")
            return
        try:
            from nexus.gui.tasks_tab import TasksTab
            self.tasks_tab = TasksTab(page, self.nexus, C, px)
        except Exception as e:
            log.warning("節點任務分頁無法載入：%r", e)
            ttk.Label(page, text=f"節點任務分頁無法載入：{type(e).__name__}", foreground=C["bad"]).pack(anchor="w")

    def build_security_tab(self, page):
        canvas = tk.Canvas(page, highlightthickness=0, bg=C["bg"])
        bar = ttk.Scrollbar(page, orient="vertical", command=canvas.yview)
        f = ttk.Frame(canvas)
        f.bind("<Configure>", lambda e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.create_window((0, 0), window=f, anchor="nw")
        canvas.configure(yscrollcommand=bar.set)
        canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.sec_frame = f
        top = ttk.Frame(f)
        top.pack(fill="x")
        ttk.Label(top, text="這裡可以看到：誰配對過、AI 用了多少、連過哪些網站、用的元件和指紋。",
                  foreground=C["muted"]).pack(side="left")
        ttk.Button(top, text="↻ 重新整理", command=self.refresh_security).pack(side="right")
        self.sec_body = ttk.Frame(f)
        self.sec_body.pack(fill="both", expand=True)
        self.adv.bind("<Map>", lambda e: self.refresh_security() if e.widget is self.adv else None, add="+")

    def _sec_section(self, title, note=""):
        ttk.Label(self.sec_body, text=title, font=("", 10, "bold")).pack(anchor="w", pady=(12, 2))
        if note:
            ttk.Label(self.sec_body, text=note, foreground=C["muted"], wraplength=690, justify="left").pack(anchor="w")
        box = ttk.Frame(self.sec_body)
        box.pack(fill="x", anchor="w")
        return box

    def refresh_security(self):
        if not getattr(self, "sec_body", None):
            return
        for w in self.sec_body.winfo_children():
            w.destroy()
        fmt = lambda t: time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "—"

        # 1. 已配對的瀏覽器
        box = self._sec_section("🔗 已配對的瀏覽器",
                                "只存密碼的雜湊值（看不到原本的密碼）。不認得的就按「取消」，那個瀏覽器就不能再送影片過來。")
        tokens = list(self.settings["bridge_tokens"] or [])
        meta = self.settings["bridge_meta"] or {}
        if not tokens:
            ttk.Label(box, text="（沒有）").pack(anchor="w")
        for h in tokens:
            m = meta.get(h) or {"ua": "（舊版配對，沒有紀錄）"}
            row = ttk.Frame(box)
            row.pack(fill="x", anchor="w", pady=1)
            ttk.Label(row, text=f"• {m.get('ua', '?')}｜配對 {fmt(m.get('paired'))}｜最後使用 {fmt(m.get('last'))}"
                                f"｜用過 {m.get('uses', 0)} 次｜編號 {h[:8]}").pack(side="left")
            ttk.Button(row, text="取消", command=lambda h=h: (self.bridge.unpair(h), self.refresh_bridge_label(),
                                                             self.refresh_security())).pack(side="left", padx=6)
        st = "開著（127.0.0.1:%s，只有這台電腦連得到）" % self.bridge.port if self.bridge.server else "關閉"
        ttk.Label(box, text=f"連線服務：{st}", foreground=C["muted"]).pack(anchor="w", pady=(4, 0))

        # 2. AI 使用紀錄
        box = self._sec_section("🤖 AI 使用紀錄",
                                "只記次數和字數（token），不記送了什麼內容。費用照 Anthropic 官網的價格算。")
        u = self.settings["ai_usage"] or {}
        if not u:
            ttk.Label(box, text="（還沒用過）" + ("" if (self.settings["ai_key"] or "").strip() else "，也沒填 AI 金鑰：完全不會連到 AI")).pack(anchor="w")
        names = {"classify": "判斷廣告網域", "translate": "翻譯字幕"}
        for month, m in sorted(u.items(), reverse=True)[:6]:
            parts = [f"{names.get(k, k)} {v['calls']} 次（送 {v['in']:,}／收 {v['out']:,} token）" for k, v in m.items()]
            ttk.Label(box, text=f"• {month}：" + "；".join(parts)).pack(anchor="w")

        # 3. 外部連線
        box = self._sec_section("🌐 這次開著時連過的網站",
                                "在程式最底層記錄，只記網域和次數，關掉程式就清空（不存檔）。"
                                "正常會看到：影片網站和它的影片伺服器、github.com（檢查更新、廣告清單）、"
                                "huggingface.co（AI 字幕模型）、api.anthropic.com（有填金鑰才會）。")
        with _NET_LOCK:
            seen = sorted(NET_SEEN.items(), key=lambda x: -x[1]["n"])
        if not seen:
            ttk.Label(box, text="（還沒連過任何網站）").pack(anchor="w")
        known = ("github.com", "githubusercontent.com", "huggingface.co", "hf.co", "hf-mirror.com", "anthropic.com")
        for h, e in seen[:40]:
            tag = "" if any(h == d or h.endswith("." + d) for d in known) or bridge_url_allowed("https://" + h + "/", BRIDGE_HOSTS + BRIDGE_MEDIA_HOSTS) else "　← 不在常見清單，可以查一下是什麼"
            ttk.Label(box, text=f"• {h}　{e['n']} 次｜最後 {time.strftime('%H:%M:%S', time.localtime(e['last']))}{tag}").pack(anchor="w")

        # 4. 錯誤日誌
        box = self._sec_section("📄 錯誤日誌",
                                "只記網站名稱和錯誤訊息，不記完整影片網址和檔名。日誌只在這台電腦上，不會上傳。")
        logp = os.path.join(data_dir(), "video_helper.log")
        size = sum(os.path.getsize(p) for p in [logp, logp + ".1", logp + ".2"] if os.path.isfile(p))
        row = ttk.Frame(box)
        row.pack(anchor="w")
        ttk.Label(row, text=f"目前 {fmt_size(size)}｜超過").pack(side="left")
        keep = ttk.Combobox(row, values=["3", "7", "14", "30", "0"], width=4, state="readonly")
        keep.set(str(self.settings["log_keep_days"]))
        keep.pack(side="left", padx=4)
        ttk.Label(row, text="天沒更新就自動刪（0＝不刪）").pack(side="left")
        keep.bind("<<ComboboxSelected>>", lambda e: (self.settings.__setitem__("log_keep_days", int(keep.get())), self.settings.save()))
        ttk.Button(row, text="🗑 現在清空", command=self.clear_logs).pack(side="left", padx=10)

        # 5. 元件和指紋
        box = self._sec_section("🧩 用到的元件和指紋（SHA-256）",
                                "可以跟 GitHub 發布頁、SECURITY.md 對照。EXE 另外有 GitHub 簽章，"
                                "用 gh attestation verify VideoHelper.exe -R dadanny1122/video-helper 驗證。")
        self.sec_comp = ttk.Label(box, text="計算中…", justify="left", font=("Consolas", 9))
        self.sec_comp.pack(anchor="w")
        ttk.Button(box, text="打開 GitHub 簽章紀錄", command=lambda: webbrowser.open(
            "https://github.com/dadanny1122/video-helper/attestations")).pack(anchor="w", pady=4)
        threading.Thread(target=lambda: self.q.put(("sec_components", component_report())), daemon=True).start()

    def on_sec_components(self, text):
        if getattr(self, "sec_comp", None) is not None and self.sec_comp.winfo_exists():
            self.sec_comp.config(text=text)

    def clear_logs(self, ask=True):
        if ask and not messagebox.askyesno("清空錯誤日誌", "確定要清空錯誤日誌嗎？（出問題要回報時，日誌可以幫忙找原因）"):
            return
        base = os.path.join(data_dir(), "video_helper.log")
        for h in log.handlers:
            if isinstance(h, RotatingFileHandler):
                h.acquire()
                try:
                    h.stream.seek(0)
                    h.stream.truncate()
                finally:
                    h.release()
        for p in (base + ".1", base + ".2"):
            try:
                os.remove(p)
            except OSError:
                pass
        self.refresh_security()

    # ------------------------------------------------------------------
    # AI 字幕
    # ------------------------------------------------------------------

    def subtitle_dialog(self, video=None, files=None):
        """一部或多部（下載紀錄可以 Ctrl／Shift 多選）一起做字幕，一部接一部；files＝新介面勾選的影片"""
        if video:
            videos = [video]
        elif files is not None:
            videos = [f for f in dict.fromkeys(files) if f and os.path.isfile(f)]
        else:
            videos = []
            for iid in self.htree.selection():
                r = self.history.items[int(iid)]
                if r.get("file") and os.path.isfile(r["file"]) and r["file"] not in videos:
                    videos.append(r["file"])
        if not videos:
            messagebox.showinfo("AI 字幕", "請先在清單裡選「已下載完成、檔案還在」的影片（按住 Ctrl 或 Shift 可以一次選很多部）。")
            return
        if getattr(self, "sub_win", None) is not None and self.sub_win.winfo_exists():
            self.sub_win.lift()
            return
        w = self.sub_win = tk.Toplevel(self.root)
        self.float_dialog(w)
        w.title("📝 AI 字幕")
        w.resizable(False, False)
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both")
        head = os.path.basename(videos[0]) if len(videos) == 1 else f"共 {len(videos)} 部影片（一部接一部做）"
        ttk.Label(f, text=head, font=("", 10, "bold"), wraplength=440).grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(f, text="影片說的語言").grid(row=1, column=0, sticky="w", pady=(10, 2))
        lang = ttk.Combobox(f, values=list(SUB_LANGS), state="readonly", width=24)
        lang.set(self.settings["sub_lang"] if self.settings["sub_lang"] in SUB_LANGS else "自動偵測")
        lang.grid(row=1, column=1, sticky="w", pady=(10, 2))
        ttk.Label(f, text="AI 模型").grid(row=2, column=0, sticky="w", pady=2)
        model = ttk.Combobox(f, values=list(SUB_MODELS), state="readonly", width=24)
        model.set(self.settings["sub_model"] if self.settings["sub_model"] in SUB_MODELS else next(iter(SUB_MODELS)))
        model.grid(row=2, column=1, sticky="w", pady=2)
        ttk.Label(f, text="字幕格式").grid(row=3, column=0, sticky="w", pady=2)
        fmt = ttk.Combobox(f, values=list(SUB_FORMATS), state="readonly", width=24)
        fmt.set(self.settings["sub_format"] if self.settings["sub_format"] in SUB_FORMATS else next(iter(SUB_FORMATS)))
        fmt.grid(row=3, column=1, sticky="w", pady=2)
        key = (self.settings["ai_key"] or "").strip()
        tr = tk.BooleanVar(value=bool(key) and self.settings["sub_translate"])
        dual = tk.BooleanVar(value=bool(self.settings["sub_dual"]))
        cb = ttk.Checkbutton(f, text="翻成繁體中文" + ("" if key else "（要先在 ⚙ 進階設定 填 AI 金鑰）"), variable=tr)
        cb.grid(row=4, column=0, columnspan=2, sticky="w", pady=(6, 0))
        cb2 = ttk.Checkbutton(f, text="另外做一份「中文＋原文」雙語字幕（學語言用）", variable=dual)
        cb2.grid(row=5, column=0, columnspan=2, sticky="w", padx=(20, 0))
        sync = lambda *_: cb2.state(["!disabled"] if tr.get() else ["disabled"])
        tr.trace_add("write", sync)
        sync()
        if not key:
            cb.state(["disabled"])
        skip = tk.BooleanVar(value=bool(self.settings["sub_skip_done"]))
        if len(videos) > 1:
            ttk.Checkbutton(f, text="已經有字幕的影片跳過", variable=skip).grid(row=6, column=0, columnspan=2, sticky="w")
        ttk.Label(f, foreground=C["muted"], wraplength=440, justify="left", text=(
            "• 聽寫完全在你的電腦上跑，影片和聲音不會上傳。\n"
            "• 第一次用會下載聽寫程式和模型（只要一次）。沒有顯示卡的電腦，1 小時影片「快」約 10～20 分鐘，「準」慢 3～5 倍。\n"
            "• 影片本來就是中文：語言選「中文」，不用翻譯。\n"
            "• 翻中文時只把字幕文字送給 AI（用你的金鑰，會花一點點額度）。\n"
            "• 字幕存在影片旁邊、跟影片同名；VLC、PotPlayer 打開影片會自動載入。雙語字幕叫「影片.雙語.srt」，在播放器的字幕選單切換。")).grid(row=7, column=0, columnspan=2, sticky="w", pady=(8, 6))
        self.sub_stage = ttk.Label(f, text="")
        self.sub_stage.grid(row=8, column=0, columnspan=2, sticky="w")
        self.sub_bar = ttk.Progressbar(f, length=440, maximum=100)
        self.sub_bar.grid(row=9, column=0, columnspan=2, pady=4)
        self.sub_info = ttk.Label(f, text="", foreground=C["muted"], wraplength=440, justify="left")
        self.sub_info.grid(row=10, column=0, columnspan=2, sticky="w")
        btns = ttk.Frame(f)
        btns.grid(row=11, column=0, columnspan=2, sticky="e", pady=(6, 0))
        self.sub_cancel = threading.Event()
        go = ttk.Button(btns, text="開始", style="Accent.TButton")
        stop = ttk.Button(btns, text="取消", command=lambda: (self.sub_cancel.set(), None) if self.sub_running else w.destroy())
        go.pack(side="left", padx=4)
        stop.pack(side="left")
        self.sub_running = False

        def start():
            self.settings["sub_lang"], self.settings["sub_model"], self.settings["sub_format"] = lang.get(), model.get(), fmt.get()
            self.settings["sub_dual"], self.settings["sub_skip_done"] = bool(dual.get()), bool(skip.get())
            if key:
                self.settings["sub_translate"] = tr.get()
            self.settings.save()
            go.state(["disabled"])
            self.sub_running = True
            todo = [v for v in videos if not (len(videos) > 1 and skip.get() and has_subtitles(v))]
            opts = dict(lang=SUB_LANGS[lang.get()], model=SUB_MODELS[model.get()], key=key if tr.get() else "",
                        fmts=SUB_FORMATS[fmt.get()], dual=bool(tr.get() and dual.get()))
            threading.Thread(target=self._subtitle_worker, args=(todo, len(videos) - len(todo), opts), daemon=True).start()
        go.config(command=start)

    def _subtitle_worker(self, videos, skipped, opts):
        def prog(got, total):
            self.q.put(("sub_progress", (got, total)))
        results, errors = [], []
        for i, video in enumerate(videos, 1):
            tag = f"（{i}/{len(videos)}）{os.path.basename(video)[:30]}｜" if len(videos) > 1 else ""
            if self.sub_cancel.is_set():
                break
            try:
                results.append(make_subtitles(video, opts["lang"], opts["model"], opts["key"],
                                              on_stage=lambda t, tag=tag: self.q.put(("sub_stage", tag + t)),
                                              on_progress=prog, cancel_event=self.sub_cancel,
                                              on_text=lambda t: self.q.put(("sub_text", t)),
                                              fmts=opts["fmts"], dual=opts["dual"]))
            except Cancelled:
                break
            except Exception as e:
                log.exception("AI 字幕失敗")
                errors.append(f"{os.path.basename(video)[:40]}：{e}")
        if self.sub_cancel.is_set() and not results:
            self.q.put(("sub_error", "已取消"))
        elif len(videos) == 1 and errors:
            self.q.put(("sub_error", errors[0].split("：", 1)[-1]))
        else:
            self.q.put(("sub_done", {"results": results, "errors": errors, "skipped": skipped,
                                     "cancelled": self.sub_cancel.is_set()}))

    def _sub_alive(self):
        return getattr(self, "sub_win", None) is not None and self.sub_win.winfo_exists()

    def on_sub_stage(self, text):
        if self._sub_alive():
            self.sub_stage.config(text=text)
            self.sub_bar.config(value=0)

    def on_sub_text(self, text):
        if self._sub_alive():
            self.sub_info.config(text=text)

    def on_sub_progress(self, data):
        got, total = data
        if self._sub_alive():
            if total:
                self.sub_bar.config(value=got * 100 / total)
            if total > 1000:                                   # 下載中（聽寫的進度是千分比，total＝1000）：顯示 MB
                self.sub_stage.config(text=self.sub_stage.cget("text").split("｜")[0] + f"｜{fmt_size(got)} / {fmt_size(total)}")

    def on_sub_done(self, data):
        self.sub_running = False
        if self._sub_alive():
            self.sub_win.destroy()
        rs, errs = data["results"], data["errors"]
        if len(rs) == 1 and not errs and not data["skipped"]:
            res = rs[0]
            msg = f"完成！共 {res['count']} 句，語言：{res['lang']}\n\n原文字幕：{res['orig']}"
            if res.get("zh"):
                msg += f"\n中文字幕：{res['zh']}"
            if res.get("dual"):
                msg += f"\n雙語字幕：{res['dual']}"
        else:
            msg = (f"完成 {len(rs)} 部" + (f"、跳過 {data['skipped']} 部（已經有字幕）" if data["skipped"] else "")
                   + (f"、失敗 {len(errs)} 部" if errs else "") + ("（中途取消）" if data["cancelled"] else "") + "。")
            if errs:
                msg += "\n\n失敗的：\n" + "\n".join(errs[:8])
        if not rs:
            messagebox.showinfo("AI 字幕", msg)
            return
        last = rs[-1]
        if messagebox.askyesno("AI 字幕", msg + "\n\n要打開檔案位置嗎？"):
            open_path(last.get("zh") or last["orig"], select=True)

    def on_sub_error(self, msg):
        self.sub_running = False
        if self._sub_alive():
            self.sub_win.destroy()
        if msg != "已取消":
            messagebox.showerror("AI 字幕", "沒有做成功：" + msg + "\n\n（詳細原因在 ⚙ 進階設定 → 問題排查 → 打開錯誤日誌）")


    # ------------------------------------------------------------------
    # ④ 設定
    # ------------------------------------------------------------------

    def build_settings(self, parent):
        """平常會用的三樣放這裡；其他收進「⚙ 進階設定…」"""
        outer = ttk.Frame(parent, padding=(12, 8))
        outer.pack(fill="both", expand=True)
        box = lambda title: ttk.LabelFrame(outer, text=f" {title} ", padding=(10, 6))

        # --- 下載 ---
        f = box("下載")
        f.pack(fill="x", pady=4)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="下載資料夾：").grid(row=0, column=0, sticky="w", pady=4)
        self.dir_var = tk.StringVar(value=self.settings.download_dir)
        ttk.Entry(f, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="瀏覽…", command=self.choose_dir).grid(row=0, column=2)

        ttk.Label(f, text="預設畫質：").grid(row=1, column=0, sticky="w", pady=4)
        self.q_var = tk.StringVar(value=self.settings["default_quality"])
        ttk.Combobox(f, textvariable=self.q_var, values=QUALITY_CHOICES, state="readonly", width=10).grid(
            row=1, column=1, sticky="w", padx=6)
        ttk.Label(f, text="（沒有這個畫質時，選比它低的最高畫質）", foreground=C["muted"]).grid(
            row=1, column=1, sticky="w", padx=(110, 0))
        ttk.Button(f, text="💾 儲存", command=self.save_settings).grid(row=1, column=2)

        ttk.Label(f, text="速度上限：").grid(row=2, column=0, sticky="w", pady=4)
        self.rate_var = tk.StringVar(value=str(self.settings["rate_limit_mbps"] or "不限制"))
        rate = ttk.Combobox(f, textvariable=self.rate_var, values=["不限制", "5", "10", "20", "50", "100"], width=10)
        rate.grid(row=2, column=1, sticky="w", padx=6)
        ttk.Label(f, text="Mbps（下載時不想卡到別人上網就設一下；可以直接打數字，馬上生效）", foreground=C["muted"]).grid(
            row=2, column=1, sticky="w", padx=(110, 0))
        rate.bind("<<ComboboxSelected>>", lambda e: self.apply_rate_limit())
        rate.bind("<FocusOut>", lambda e: self.apply_rate_limit())
        rate.bind("<Return>", lambda e: self.apply_rate_limit())
        self.skip_var = tk.BooleanVar(value=bool(self.settings["skip_downloaded"]))
        ttk.Checkbutton(f, text="批次、瀏覽器送來的影片：下載過（檔案還在）就自動跳過", variable=self.skip_var,
                        command=lambda: (self.settings.__setitem__("skip_downloaded", bool(self.skip_var.get())),
                                         self.settings.save())).grid(row=3, column=0, columnspan=3, sticky="w", pady=2)
        ttk.Label(f, text="智慧分段：").grid(row=4, column=0, sticky="w", pady=4)
        self.smart_var = tk.StringVar(value=next((k for k, v in SMART_MODES.items() if v == self.settings["smart_segments"]),
                                                 next(iter(SMART_MODES))))
        sm = ttk.Combobox(f, textvariable=self.smart_var, values=list(SMART_MODES), state="readonly", width=26)
        sm.grid(row=4, column=1, sticky="w", padx=6)
        sm.bind("<<ComboboxSelected>>", lambda e: (self.settings.__setitem__("smart_segments", SMART_MODES[self.smart_var.get()]),
                                                   self.settings.save()))
        ttk.Button(f, text="📊 報告", command=lambda: messagebox.showinfo("智慧分段報告", smart_report())).grid(row=4, column=2)
        ttk.Label(f, foreground=C["muted"], wraplength=520, justify="left",
                  text="看下載時的速度和出錯情況，決定下一部影片同時開幾條線（1／2／4／6）。"
                       "網路不穩時開少一點反而比較快；先用「只觀察」累積紀錄，報告看得出有幫助再改「自動調整」。").grid(
            row=5, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="外觀：").grid(row=6, column=0, sticky="w", pady=(8, 4))
        self.theme_var = tk.StringVar(value=next((k for k, v in THEMES.items() if v == self.settings["theme"]), "跟系統"))
        th = ttk.Combobox(f, textvariable=self.theme_var, values=list(THEMES), state="readonly", width=10)
        th.grid(row=6, column=1, sticky="w", padx=6, pady=(8, 4))
        th.bind("<<ComboboxSelected>>", lambda e: self.change_theme())
        self.home_var = tk.BooleanVar(value=bool(self.settings["show_home"]))
        ttk.Checkbutton(f, text="打開時先顯示首頁", variable=self.home_var,
                        command=lambda: (self.settings.__setitem__("show_home", self.home_var.get()), self.settings.save())).grid(
            row=7, column=0, columnspan=3, sticky="w")
        ttk.Label(f, text="（深色／淺色；部分文字顏色重新打開程式後才會全部換好）", style="Muted.TLabel").grid(
            row=6, column=1, sticky="w", padx=(110, 0), pady=(8, 4))
        self.tray_var = tk.BooleanVar(value=bool(self.settings["tray_always"]))

        def on_tray_opt():
            self.settings["tray_always"] = self.tray_var.get()
            self.settings.save()
            if self.tray_var.get():
                self.start_tray()
            elif self.tray and self.root.state() != "withdrawn":   # 視窗開著才收掉（縮在系統匣時不能拿掉，不然找不回來）
                try:
                    self.tray.stop()
                except Exception:
                    pass
                self.tray = None
        ttk.Checkbutton(f, text="右下角系統匣一直顯示小圖示（右鍵：狀態、暫停、貼上網址下載、打開下載資料夾）",
                        variable=self.tray_var, command=on_tray_opt).grid(row=9, column=0, columnspan=3, sticky="w")
        self.snap_var = tk.BooleanVar(value=bool(self.settings["drag_snapshot"]))
        ttk.Checkbutton(f, text="拖動視窗時比較順（拖動時先顯示一張畫面照片，放開就恢復）", variable=self.snap_var,
                        command=lambda: (self.settings.__setitem__("drag_snapshot", self.snap_var.get()), self.settings.save())).grid(
            row=8, column=0, columnspan=3, sticky="w")

        # --- 瀏覽器連動 ---
        c = box("瀏覽器連動（在影片網頁按「🖥 送到電腦版」）")
        c.pack(fill="x", pady=4)
        self.bridge_lbl = ttk.Label(c, text="")
        self.bridge_lbl.pack(side="left", pady=3)
        ttk.Button(c, text="🔗 配對瀏覽器…", command=self.pair_browser).pack(side="right")
        self.refresh_bridge_label()

        ttk.Button(outer, text="⚙ 進階設定…", command=self.show_advanced).pack(anchor="w", pady=(14, 2))
        ttk.Label(outer, foreground=C["muted"], justify="left",
                  text="檔名格式、關閉視窗的行為、備份、檢查更新、錯誤日誌、統計都在這裡。\n平常用不到，出問題或換電腦時再打開。").pack(anchor="w")
        self.ver_lbl = ttk.Label(outer, text=f"目前版本：v{VERSION}", foreground=C["muted"])
        self.ver_lbl.pack(side="bottom", anchor="w")

    def build_advanced(self):
        """進階設定視窗：一開始就建好但藏起來（按鈕、選項才都在），按「⚙ 進階設定…」才顯示"""
        win = self.adv = tk.Toplevel(self.root)
        win.title("進階設定")
        win.geometry(f"{px(760)}x{px(560)}")
        win.withdraw()
        win.protocol("WM_DELETE_WINDOW", win.withdraw)
        nb = self.adv_nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True)
        pages = {}
        self.adv_pages = pages
        for key, text in (("prog", "檔名與程式"), ("link", "瀏覽器連動"), ("security", "🔒 安全"), ("nodes", "🖧 節點"), ("tasks", "📋 節點任務"),
                          ("backup", "備份與更新"), ("debug", "問題排查"), ("ai", "廣告判斷"), ("stats", "統計")):
            pages[key] = ttk.Frame(nb, padding=(12, 10))
            nb.add(pages[key], text=f"  {text}  ")

        # --- 檔名與程式 ---
        g = pages["prog"]
        g.columnconfigure(1, weight=1)
        ttk.Label(g, text="檔名格式：").grid(row=0, column=0, sticky="w", pady=4)
        tpl = self.settings["filename_template"]
        self.tpl_var = tk.StringVar(value=tpl if tpl in FILENAME_TEMPLATES else DEFAULT_TEMPLATE)
        cb = ttk.Combobox(g, textvariable=self.tpl_var, values=list(FILENAME_TEMPLATES), state="readonly", width=28)
        cb.grid(row=0, column=1, sticky="w", padx=6)
        self.tpl_preview = ttk.Label(g, text="", foreground=C["muted"])
        self.tpl_preview.grid(row=1, column=1, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_tpl_preview())
        self.update_tpl_preview()

        ttk.Label(g, text="關閉視窗時：").grid(row=2, column=0, sticky="w", pady=(12, 4))
        ca = self.settings["close_action"]
        self.close_var = tk.StringVar(value=ca if ca in CLOSE_CHOICES else CLOSE_CHOICES[0])
        choices = CLOSE_CHOICES if pystray else [c for c in CLOSE_CHOICES if "系統匣" not in c]
        ttk.Combobox(g, textvariable=self.close_var, values=choices, state="readonly", width=16).grid(
            row=2, column=1, sticky="w", padx=6, pady=(12, 4))
        ttk.Label(g, text=("縮到系統匣＝縮到右下角繼續下載，完成會跳通知" if pystray else "（這台電腦不支援系統匣）"),
                  foreground=C["muted"]).grid(row=3, column=1, sticky="w", padx=6)

        self.keep_var = tk.BooleanVar(value=bool(self.settings["keep_history"]))
        ttk.Checkbutton(g, text="保存下載紀錄", variable=self.keep_var).grid(
            row=4, column=0, columnspan=2, sticky="w", pady=(12, 2))
        self.keep_stats_var = tk.BooleanVar(value=bool(self.settings["keep_stats"]))
        ttk.Checkbutton(g, text="記錄下載統計（用來校正預估；只存數字和網站名稱）", variable=self.keep_stats_var).grid(
            row=5, column=0, columnspan=2, sticky="w", pady=2)
        ttk.Button(g, text="💾 儲存設定", command=self.save_settings).grid(row=6, column=0, sticky="w", pady=(14, 0))

        # --- 瀏覽器連動 ---
        c = pages["link"]
        ttk.Label(c, justify="left", foreground=C["muted"], wraplength=700,
                  text="取消所有配對：所有配對過的瀏覽器都要重新配對（借別人用過電腦時可以按）。\n"
                       "關閉服務：電腦版不再接收瀏覽器送來的影片，下次按「配對瀏覽器…」會再打開。").pack(anchor="w")
        row = ttk.Frame(c)
        row.pack(anchor="w", pady=10)
        ttk.Button(row, text="取消所有配對", command=self.unpair_browsers).pack(side="left")
        ttk.Button(row, text="關閉服務", command=self.stop_bridge).pack(side="left", padx=8)

        self.build_security_tab(pages["security"])
        self.build_nodes_tab(pages["nodes"])
        self.build_tasks_tab(pages["tasks"])

        # --- 備份與更新 ---
        b = pages["backup"]
        ttk.Label(b, text="換電腦、重灌時用；更新版本時也會自動備份一份", foreground=C["muted"]).pack(anchor="w")
        row = ttk.Frame(b)
        row.pack(anchor="w", pady=(6, 16))
        ttk.Button(row, text="⬆ 匯出設定和紀錄…", command=self.export_settings).pack(side="left")
        ttk.Button(row, text="⬇ 匯入…", command=self.import_settings).pack(side="left", padx=6)
        ttk.Button(row, text="📂 自動備份資料夾", command=self.open_backups).pack(side="left")
        ttk.Label(b, text=f"目前版本：v{VERSION}").pack(anchor="w")
        row = ttk.Frame(b)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="檢查更新", command=self.manual_update_check).pack(side="left")
        ttk.Button(row, text="查看更新內容",
                   command=lambda: webbrowser.open(f"https://github.com/{REPO}/releases")).pack(side="left", padx=6)

        # --- 問題排查 ---
        t = pages["debug"]
        ttk.Label(t, text="出錯時打開錯誤日誌，把內容傳給作者；換了網路、預估變很不準時清除網速紀錄。",
                  foreground=C["muted"], wraplength=700).pack(anchor="w")
        row = ttk.Frame(t)
        row.pack(anchor="w", pady=6)
        ttk.Button(row, text="📄 打開錯誤日誌", command=self.open_log).pack(side="left")
        ttk.Button(row, text="📂 打開設定資料夾", command=lambda: open_path(data_dir())).pack(side="left", padx=6)
        ttk.Button(row, text="清除網速紀錄", command=self.reset_speed).pack(side="left")
        ttk.Label(t, text=f"設定資料夾：{data_dir()}", foreground=C["muted"]).pack(anchor="w", pady=(10, 0))

        # --- 廣告判斷 ---
        x = pages["ai"]
        ttk.Label(x, justify="left", wraplength=700, text=(
            "在影片網頁點左下角 🛡 →「🔍 檢查」，會判斷「這頁連到的其他網站」哪些是廣告。紅字＝建議封鎖，要不要封鎖還是你決定。")).pack(anchor="w")
        ttk.Label(x, text="① 公開廣告清單（免費，預設就會用）", font=("", 10, "bold")).pack(anchor="w", pady=(10, 0))
        ttk.Label(x, justify="left", foreground=C["muted"], wraplength=700, text=(
            "使用 StevenBlack/hosts 整理的公開清單（MIT 授權），約 7 萬個已知廣告、追蹤、惡意網址。\n"
            "存在這台電腦、7 天自動更新一次；比對只在你的電腦做，不會送任何資料出去。")).pack(anchor="w")
        row = ttk.Frame(x)
        row.pack(anchor="w", pady=4)
        ttk.Button(row, text="立即更新清單", command=self.update_blocklist).pack(side="left")
        self.bl_lbl = ttk.Label(row, text="", foreground=C["muted"])
        self.bl_lbl.pack(side="left", padx=8)

        ttk.Label(x, text="② AI（選用，要付費；不設定也完全沒關係）", font=("", 10, "bold")).pack(anchor="w", pady=(14, 0))
        ttk.Label(x, justify="left", foreground=C["muted"], wraplength=700, text=(
            "清單裡查不到的網域，有設定金鑰才會再問 AI。只送網域名稱和網站名稱，不送影片標題、網址。\n"
            "金鑰到 console.anthropic.com 申請（要先儲值）；只存在這台電腦，不會被匯出。問過的網域記住 30 天。")).pack(anchor="w")
        row = ttk.Frame(x)
        row.pack(anchor="w", fill="x", pady=4)
        ttk.Label(row, text="API 金鑰：").pack(side="left")
        self.ai_key_var = tk.StringVar(value=self.settings["ai_key"])
        ttk.Entry(row, textvariable=self.ai_key_var, show="•", width=48).pack(side="left", padx=6)
        ttk.Button(row, text="💾 儲存", command=self.save_ai_key).pack(side="left")
        row = ttk.Frame(x)
        row.pack(anchor="w", pady=4)
        ttk.Button(row, text="測試", command=self.test_ai).pack(side="left")
        ttk.Button(row, text="清除 AI 記憶", command=self.clear_ai_cache).pack(side="left", padx=6)
        self.ai_lbl = ttk.Label(x, text="", foreground=C["muted"], wraplength=700, justify="left")
        self.ai_lbl.pack(anchor="w", pady=6)
        self.refresh_ai_label()
        self.refresh_bl_label()

        # --- 統計 ---
        self.build_stats(pages["stats"])

    def refresh_ai_label(self, extra=""):
        n = len(self.settings["ai_cache"] or {})
        has = bool((self.settings["ai_key"] or "").strip())
        self.ai_lbl.config(text=("✅ 已設定金鑰" if has else "沒有設定金鑰（只用免費清單）") + f"｜AI 記住 {n} 個網域" +
                           (f"\n{extra}" if extra else ""))

    def save_ai_key(self):
        self.settings["ai_key"] = self.ai_key_var.get().strip()
        self.settings.save()
        self.refresh_ai_label("已儲存。")

    def test_ai(self):
        key = self.ai_key_var.get().strip()
        if not key:
            self.refresh_ai_label("先貼上金鑰再測試。")
            return
        self.refresh_ai_label("測試中…")

        def work():
            try:
                r = self.ai_fn(key, "Pornhub", ["doubleclick.net", "phncdn.com"])
                txt = "測試成功：" + "、".join(f"{d}＝{AI_VERDICTS.get(v['verdict'], '?')}" for d, v in r.items())
            except Exception as e:
                txt = f"❌ 測試失敗：{e}"
            self.q.put(("ai_test", txt))
        threading.Thread(target=work, daemon=True).start()

    def refresh_bl_label(self, extra=""):
        age = self.blocklist.age_days()
        if age is None:
            t = "還沒下載（第一次按「🔍 檢查」時會自動下載，約 2 MB）"
        else:
            n = self.blocklist.count()
            t = (f"{n:,} 筆｜" if n else "") + ("今天更新" if age < 1 else f"{int(age)} 天前更新")
        self.bl_lbl.config(text=t + (f"｜{extra}" if extra else ""))

    def update_blocklist(self):
        self.refresh_bl_label("更新中…")

        def work():
            try:
                self.blocklist.download()
                self.blocklist.load()
                msg = "✅ 已更新"
            except Exception as e:
                msg = f"❌ 更新失敗：{e}"
            self.q.put(("bl_updated", msg))
        threading.Thread(target=work, daemon=True).start()

    def on_bl_updated(self, msg):
        self.refresh_bl_label(msg)

    def on_ai_test(self, txt):
        self.refresh_ai_label(txt)

    def clear_ai_cache(self):
        self.settings["ai_cache"] = {}
        self.settings.save()
        self.refresh_ai_label("已清除。")

    def show_advanced(self, page=None):
        self.adv.deiconify()
        self.adv.lift()
        if page is not None:
            self.adv_nb.select(page)
        try:
            self.adv.focus_force()
        except Exception:
            pass

    def change_theme(self):
        self.settings["theme"] = THEMES[self.theme_var.get()]
        self.settings.save()
        self.theme = apply_theme(self.root, self.settings["theme"])
        for t in (self.tree, self.qtree, self.htree):
            self.style_tree(t)
        self.header.draw()
        self.ticker.draw()
        if self.home.visible():
            self.home.configure(bg=C["bg"])
            self.home.draw()

    def apply_rate_limit(self):
        txt = self.rate_var.get().strip()
        try:
            v = 0.0 if txt in ("", "不限制", "0") else float(txt)
            if v < 0:
                raise ValueError
        except ValueError:
            self.rate_var.set(str(self.settings["rate_limit_mbps"] or "不限制"))
            return
        self.settings["rate_limit_mbps"] = v
        self.settings.save()
        THROTTLE.set_mbps(v)
        self.rate_var.set(f"{v:g}" if v else "不限制")
        self.refresh_queue()

    def apply_settings_to_ui(self):
        """設定被匯入後，讓畫面上的選項跟著改"""
        self.dir_var.set(self.settings.download_dir)
        self.q_var.set(self.settings["default_quality"])
        self.rate_var.set(str(self.settings["rate_limit_mbps"] or "不限制"))
        self.skip_var.set(bool(self.settings["skip_downloaded"]))
        self.smart_var.set(next((k for k, v in SMART_MODES.items() if v == self.settings["smart_segments"]), next(iter(SMART_MODES))))
        self.settings["seg_state"] = dict(self.settings["seg_state"] or {})   # 不要改到預設值那份
        self.segments = SegmentController(self.settings["seg_state"])
        THROTTLE.set_mbps(self.settings["rate_limit_mbps"])
        self.tpl_var.set(self.settings["filename_template"])
        self.keep_var.set(bool(self.settings["keep_history"]))
        self.keep_stats_var.set(bool(self.settings["keep_stats"]))
        self.close_var.set(self.settings["close_action"])
        self.update_tpl_preview()
        self.refresh_history()

    def export_settings(self):
        path = filedialog.asksaveasfilename(
            title="匯出設定和紀錄", defaultextension=".json", filetypes=[("Video Helper 備份", "*.json")],
            initialfile=f"VideoHelper-備份-{datetime.now():%Y%m%d}.json")
        if not path:
            return
        with_hist = messagebox.askyesno("匯出", "要連下載紀錄一起匯出嗎？\n（選「否」只匯出設定）")
        try:
            export_backup(path, self.settings, self.history if with_hist else None)
            messagebox.showinfo("匯出完成", f"已存到：\n{path}")
        except Exception as e:
            log.exception("匯出失敗")
            messagebox.showerror("匯出失敗", str(e))

    def import_settings(self, path=None):
        path = path or filedialog.askopenfilename(
            title="匯入設定", initialdir=backups_dir() if os.path.isdir(backups_dir()) else None,
            filetypes=[("Video Helper 備份", "*.json")])
        if not path:
            return
        try:
            s, hist, warns = import_backup(path)
        except Exception as e:
            messagebox.showerror("匯入失敗", f"讀不了這個檔案：\n{e}")
            return
        self.settings.data.update(s)
        self.settings.save()
        added = self.history.merge(hist) if hist else 0
        self.apply_settings_to_ui()
        msg = f"已匯入 {len(s)} 項設定" + (f"、{added} 筆新的下載紀錄" if hist is not None else "") + "。"
        if warns:
            msg += "\n\n注意：\n" + "\n".join("・" + w for w in warns)
        log.info("匯入備份：%s（設定 %d 項、紀錄 %d 筆）", path, len(s), added)
        messagebox.showinfo("匯入完成", msg)

    def open_backups(self):
        os.makedirs(backups_dir(), exist_ok=True)
        open_path(backups_dir())

    def manual_update_check(self):
        threading.Thread(target=check_update, args=(self.q, True), daemon=True).start()

    # ------------------------------------------------------------------
    # ⑤ 統計
    # ------------------------------------------------------------------

    def build_stats(self, parent):
        pad = {"padx": 10, "pady": 6}
        self.stats_lbl = ttk.Label(parent, text="", justify="left", font=("", 10))
        self.stats_lbl.pack(fill="x", **pad)
        cols = ("site", "count", "bytes", "mbps", "terr", "serr")
        self.stree = ttk.Treeview(parent, columns=cols, show="headings", height=8)
        for c, name, w in (("site", "網站", 150), ("count", "次數", 60), ("bytes", "總量", 100), ("mbps", "平均速度", 110),
                           ("terr", "時間預估誤差", 120), ("serr", "大小預估誤差", 120)):
            self.stree.heading(c, text=name)
            self.stree.column(c, width=w, anchor="center")
        self.stree.pack(fill="both", expand=True, **pad)
        ttk.Label(parent, foreground=C["muted"], wraplength=740, justify="left",
                  text="誤差＝原本的預估跟實際差多少（中位數）。同一個網站下載 3 次以上，之後的預估會自動用這些結果校正。\n"
                       "只記錄網站名稱和數字（大小、秒數、畫質），不記錄影片標題和網址。可以在「檔名與程式」關掉。").pack(fill="x", **pad)
        bar = ttk.Frame(parent)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="清除統計", command=self.clear_stats).pack(side="right")
        self.refresh_stats()

    def refresh_stats(self):
        if not hasattr(self, "stree"):
            return
        self.stree.delete(*self.stree.get_children())
        pct = lambda x: "—" if x is None else f"±{x * 100:.0f}%"
        rows = self.stats.summary()
        for r in rows:
            self.stree.insert("", "end", values=(r["site"], r["count"], fmt_size(r["bytes"]),
                                                 f"{r['mbps']:.1f} Mbps" if r["mbps"] else "—", pct(r["time_err"]), pct(r["size_err"])))
        total = sum(r["bytes"] for r in rows)
        n = sum(r["count"] for r in rows)
        tf, tn = self.stats.factor("time")
        if not n:
            self.stats_lbl.config(text="還沒有統計。下載完成後會自動記錄。")
            return
        self.stats_lbl.config(text=(f"共下載 {n} 部｜總量 {fmt_size(total)}｜"
                                    + (f"整體來說實際時間是預估的 {tf:.2f} 倍（依最近 {tn} 次）" if tf else "累積 3 次以上就會開始校正預估")))

    def clear_stats(self):
        if self.stats.items and messagebox.askyesno("清除統計", "確定要清除所有下載統計嗎？預估會回到沒有校正的狀態。"):
            self.stats.clear()
            self.refresh_stats()

    def update_tpl_preview(self):
        self.tpl_preview.config(text="範例：" + preview_filename(self.tpl_var.get()))

    def choose_dir(self):
        d = filedialog.askdirectory(initialdir=self.dir_var.get() or default_download_dir())
        if d:
            self.dir_var.set(d)

    def save_settings(self):
        d = self.dir_var.get().strip()
        if d and not os.path.isdir(d):
            try:
                os.makedirs(d, exist_ok=True)
            except Exception:
                messagebox.showerror("資料夾無效", f"無法使用這個資料夾：\n{d}")
                return
        self.settings["download_dir"] = "" if d == default_download_dir() else d
        self.settings["default_quality"] = self.q_var.get()
        self.settings["keep_history"] = bool(self.keep_var.get())
        self.settings["filename_template"] = self.tpl_var.get()
        self.settings["close_action"] = self.close_var.get()
        self.settings["keep_stats"] = bool(self.keep_stats_var.get())
        self.settings.save()
        self.refresh_history()
        messagebox.showinfo("已儲存", "設定已儲存。")

    def open_log(self):
        p = getattr(log, "log_path", None)
        if p and os.path.exists(p):
            if sys.platform.startswith("win"):
                os.startfile(p)
            else:
                open_path(p, select=True)
        else:
            messagebox.showinfo("錯誤日誌", "目前還沒有日誌。")

    def reset_speed(self):
        self.mbps = 0
        self.settings["mbps"] = 0
        self.settings.save()
        self.speed_lbl.config(text="網速：尚未量測")


def prefer_web_ui(st):
    """v1.42.0 起只用新介面（使用者 2026-11-24 決定）：設定檔裡舊的 classic 一律換成 web。
    原本的視窗只在新介面打不開時（沒有 WebView2）自動出現，或用 `VideoHelper.exe --classic` 救急"""
    if st["ui"] != "web":
        st["ui"] = "web"
        st.save()
        return True
    return False


def selftest(out_path):
    """
    VideoHelper.exe --selftest 結果.json
    不開視窗，檢查打包進去的東西都能用（GitHub 打包後會實際執行一次）
    """
    r = {"version": VERSION, "frozen": bool(getattr(sys, "frozen", False)), "ffmpeg": FFMPEG}
    ok = True
    try:
        run = lambda *a: subprocess.run([FFMPEG, "-hide_banner", *a], capture_output=True, text=True, timeout=30,
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
        r["ffmpeg_version"] = run("-version").splitlines()[0]
        bsfs, muxers = run("-bsfs"), run("-muxers")
        r["has_aac_adtstoasc"] = "aac_adtstoasc" in bsfs
        r["has_setts"] = "setts" in bsfs
        r["has_mp4_muxer"] = " mp4 " in muxers
        probe = os.path.join(os.path.dirname(FFMPEG), "ffprobe" + (".exe" if FFMPEG.endswith(".exe") else ""))
        r["has_ffprobe"] = os.path.isfile(probe)
        ok &= all(r[k] for k in ("has_aac_adtstoasc", "has_setts", "has_mp4_muxer", "has_ffprobe"))
    except Exception as e:
        r["ffmpeg_error"] = repr(e)
        ok = False
    r["yt_dlp"] = yt_dlp.version.__version__
    r["tray"] = pystray is not None
    ok &= r["tray"]
    try:                                            # Nexus（節點連線層）：套件與 cryptography 有沒有打包進來、能不能真的簽憑證
        import cryptography
        import nexus
        from nexus.identity import certs as _nc
        _k = _nc.new_key()
        _cert = _nc.sign_device_cert(*_nc.make_ca(years=1), _nc.make_csr(_k, "selftest"), "selftest", days=1)
        r["nexus"] = {"version": nexus.NEXUS_VERSION, "cryptography": cryptography.__version__,
                      "sign_ok": bool(_nc.cert_fingerprint(_cert)),
                      "sites": len(_sites.load()["sites"])}       # sites.json 有打包進去（--add-data）
        ok &= r["nexus"]["sites"] > 0
    except Exception as e:
        r["nexus_error"] = repr(e)
        ok = False
    try:                                            # v1.36.0 新介面：網頁檔、CHANGELOG、pywebview（含 WinForms 那一層）有打包進來
        import webui
        import webui.thumbs  # noqa: F401
        import webview  # noqa: F401
        import webview.platforms.winforms  # noqa: F401
        r["webui"] = {"html": len(webui.page_html()), "news": len(webui.load_news()), "webview2": webui.webview2_version()}
        ok &= r["webui"]["html"] > 5000 and r["webui"]["news"] > 0
    except Exception as e:
        r["webui_error"] = repr(e)
        ok = False
    r["ok"] = bool(ok)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--verify-self" in sys.argv:                  # v1.43.0：CI 在簽章後用真的 EXE 跑一次（不開視窗）
        i = sys.argv.index("--verify-self")
        _res = verify_self()
        with open(sys.argv[i + 1] if len(sys.argv) > i + 1 else "verify.json", "w", encoding="utf-8") as _f:
            json.dump(_res, _f, ensure_ascii=False, indent=1)
        sys.exit(0 if _res.get("state") == "ok" else 1)
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        sys.exit(selftest(sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.json"))
    if "--worker" in sys.argv:
        # Worker 節點模式（決策 3）：獨立設定／日誌／生命週期；完全不建立 App、不碰 settings.json／Bridge／AI 金鑰
        from nexus.worker.tray import main as worker_main
        enable_dpi_awareness()
        sys.exit(worker_main([a for a in sys.argv[1:] if a != "--worker"]))
    enable_dpi_awareness()
    _st = Settings()
    prefer_web_ui(_st)
    if "--classic" not in sys.argv:
        # v1.36.0 新介面：主執行緒開網頁視窗（pywebview 規定），App／Tk 在另一個執行緒；不能用就照舊開原本的視窗
        try:
            import webui
            ok, why = webui.available()
        except Exception as e:
            ok, why = False, f"{type(e).__name__}: {e}"
        if ok:
            started = []

            def start_app(host):
                started.append(1)
                r = tk.Tk()
                App(r, web=host)
                r.mainloop()
            try:
                webui.run(start_app, f"Video Helper v{VERSION}")
                sys.exit(0)
            except SystemExit:
                raise
            except Exception:
                log.exception("新介面啟動失敗")
                if started:
                    sys.exit(1)
                WEB_UNAVAILABLE = "新介面啟動失敗（詳細在錯誤日誌）"
        else:
            log.warning("新介面不能用：%s → 用原本的視窗", why)
            WEB_UNAVAILABLE = why
    root = tk.Tk()
    App(root)
    root.mainloop()

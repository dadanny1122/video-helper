"""
Video Helper 電腦版
分頁 ①「安裝擋廣告」：一步一步帶使用者在 Chrome 裝好暴力猴和 Video Helper 腳本
分頁 ②「下載影片」：貼上網址 → 選畫質 → 加入下載佇列（一次排很多部，依序下載；可取消、可重試）
分頁 ③「下載紀錄」：完成／失敗／取消的任務，可重新下載、打開檔案位置
分頁 ⑤「統計」：每次下載的預估和實際結果（只存數字和網站名稱），並用來校正之後的預估
分頁 ④「設定」：下載資料夾、預設畫質、檔名格式、關閉視窗的行為、備份匯出／匯入、檢查更新、錯誤日誌
系統匣：關掉視窗時可以縮到右下角繼續下載，下載完成會跳通知
瀏覽器連動：配對後，瀏覽器腳本可以把 Pornhub／XVideos 影片直接送進下載佇列（只聽本機 127.0.0.1）

執行前先安裝：
    pip install yt-dlp imageio-ffmpeg pystray pillow
    （打包成 EXE 時不用 imageio-ffmpeg，改附 .github/ffmpeg/build.sh 編出來的迷你 ffmpeg）
執行：
    python video_helper_app.py

程式結構：
    上半部是「不依賴視窗」的工具（設定、紀錄、日誌、下載），可以單獨測試
    下半部是視窗介面（class App），只負責顯示和按鈕
"""

import os
import sys
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
import urllib.parse
import urllib.request
import webbrowser
from datetime import datetime
from logging.handlers import RotatingFileHandler
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

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

VERSION = "1.11.0"
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
    "keep_history": True,
    "keep_stats": True,               # 記錄下載統計（只存數字和網站名稱）
    "filename_template": DEFAULT_TEMPLATE,
    "close_action": "下載中才詢問",   # 關閉視窗時：下載中才詢問 / 一律縮到系統匣 / 直接結束
    "last_version": "",               # 上次執行的版本（版本變了就自動備份）
    "bridge_enabled": False,          # 瀏覽器連動（配對後才會打開）
    "bridge_tokens": [],              # 已配對的瀏覽器（只存雜湊值，不存原本的密碼）
    "mbps": 0,                   # 上次量到的網速
}


class Settings:
    def __init__(self, path=None):
        self.path = path or os.path.join(data_dir(), "settings.json")
        self.data = dict(DEFAULT_SETTINGS)
        self.load()

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
        return self.data.get(k, DEFAULT_SETTINGS.get(k))

    def __setitem__(self, k, v):
        self.data[k] = v

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
NOT_EXPORTED = ("mbps", "last_version", "bridge_enabled", "bridge_tokens")
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

    def next_waiting(self):
        return next((t for t in self.tasks if t["status"] == WAITING), None)

    def count(self, *statuses):
        return sum(t["status"] in statuses for t in self.tasks)

    def is_duplicate(self, url, height):
        """同一個網址、同一個畫質已經在排隊或下載中"""
        return any(t["url"] == url and t["fmt"]["height"] == height and t["status"] in (WAITING, RUNNING)
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
            if t["status"] == FAILED:
                t.update(status=WAITING, progress=0, note="")
                n += 1
        return n

    def clear_finished(self):
        self.tasks = [t for t in self.tasks if t["status"] in (WAITING, RUNNING)]


# ======================================================================
# 錯誤日誌
# ======================================================================

def setup_log():
    lg = logging.getLogger("video_helper")
    if lg.handlers:
        return lg
    lg.setLevel(logging.INFO)
    path = os.path.join(data_dir(), "video_helper.log")
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
#   5. 只接受 Pornhub、XVideos 的網址；只做「加入下載佇列」；每分鐘最多 30 次
# ======================================================================

BRIDGE_PORTS = list(range(47321, 47326))
BRIDGE_HOSTS = ("pornhub.com", "xvideos.com")
# 瀏覽器送來的「影片真正的位址」只接受這兩個網站自己的影片伺服器
BRIDGE_MEDIA_HOSTS = ("phncdn.com", "pornhub.com", "xvideos-cdn.com", "xvideos.com")
BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
PAIR_SECONDS = 300
PAIR_MAX_FAILS = 5
RATE_PER_MIN = 30


def bridge_url_allowed(url, hosts=BRIDGE_HOSTS):
    try:
        u = urllib.parse.urlparse(str(url))
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    h = (u.hostname or "").lower()
    return any(h == d or h.endswith("." + d) for d in hosts)


def hash_token(t):
    return hashlib.sha256(t.encode("utf-8")).hexdigest()


class Bridge:
    def __init__(self, settings, on_enqueue, get_status):
        """
        on_enqueue(ticket, url, height)：收到「加入下載」時呼叫（在連線服務的執行緒裡，要自己轉回主畫面）
        get_status(ticket)：回傳 {"status":…, "progress":…, "title":…} 或 None
        """
        self.settings = settings
        self.on_enqueue = on_enqueue
        self.get_status = get_status
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

    def try_pair(self, code):
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
            self.settings.save()
            log.info("瀏覽器配對成功（目前 %d 個）", len(self.settings["bridge_tokens"]))
            return token, None

    def token_ok(self, token):
        if not token:
            return False
        h = hash_token(token)
        return any(hmac.compare_digest(h, x) for x in (self.settings["bridge_tokens"] or []))

    def unpair_all(self):
        self.settings["bridge_tokens"] = []
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
                return True

            def body(self):
                if "application/json" not in (self.headers.get("Content-Type") or ""):
                    return None
                n = int(self.headers.get("Content-Length") or 0)
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
                if path == "/ping":
                    return self.send(200, {"app": "VideoHelper", "version": VERSION})
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
                    token, err = bridge.try_pair(data.get("code", ""))
                    return self.send(200, {"token": token}) if token else self.send(403, {"error": err})
                if self.path == "/enqueue":
                    if not bridge.token_ok(self.headers.get("X-VH-Token")):
                        return self.send(401, {"error": "需要重新配對"})
                    url = data.get("url", "")
                    if not bridge_url_allowed(url):
                        return self.send(400, {"error": "只接受 Pornhub、XVideos 的影片網址"})
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
                    log.info("瀏覽器送來：%s（%s）", url, quality_label(h) if h else "預設畫質")
                    return self.send(200, {"ok": True, "ticket": ticket})
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


def first_segment_url(m3u8_url, headers, depth=0):
    """從切片清單找出第一個小片段的網址（拿來測速）"""
    try:
        req = urllib.request.Request(m3u8_url, headers=headers)
        with urllib.request.urlopen(req, timeout=10) as r:
            text = r.read().decode("utf-8", "ignore")
    except Exception:
        return None
    uris = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
    if not uris:
        return None
    nxt = urllib.parse.urljoin(m3u8_url, uris[0])
    if "#EXT-X-STREAM-INF" in text and depth < 2:
        return first_segment_url(nxt, headers, depth + 1)
    return nxt


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
                      touched=None):
    """
    把一個檔案切成幾段同時下載，再接回去（內容跟一條線下載的一模一樣）。
    成功回傳 {"bytes", "start", "end"}；伺服器不支援分段就回傳 None（改用一般下載）。
    取消會丟出 Cancelled；某一段一直失敗會丟出例外。
    """
    try:
        total = _probe_size(url, headers)
    except Exception as e:
        log.info("分段下載：問不到檔案大小，改用一般下載（%s）", e)
        return None
    if total < PARALLEL_MIN_BYTES:
        return None
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    step = -(-total // parts)
    ranges = [(i, i * step, min(total, (i + 1) * step) - 1) for i in range(parts) if i * step < total]
    got = [0] * len(ranges)
    lock = threading.Lock()
    errors = []
    start = time.time()
    last = {"t": 0}

    def report():
        now = time.time()
        if not on_progress or now - last["t"] < 0.25:
            return
        last["t"] = now
        done = sum(got)
        speed = done / max(0.01, now - start)
        on_progress(done, total, speed, (total - done) / speed if speed else None)

    def worker(i, a, b):
        part = f"{dest}.part{i}"
        if touched is not None:
            touched.add(part)
        tries = 0
        with open(part, "wb") as f:
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
                    tries = 0
                except Exception as e:
                    tries += 1
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
    for p in parts_files:
        try:
            os.remove(p)
        except OSError:
            pass
    if on_progress:
        on_progress(total, total, total / max(0.01, end - start), 0)
    log.info("分段下載完成：%d 段，%.1f MB，%.1f 秒", len(ranges), total / 1e6, end - start)
    return {"bytes": total, "start": start, "end": end}


def run_download(url, fmt, out_dir, on_progress=None, on_stage=None, cancel_event=None,
                 name_template=DEFAULT_TEMPLATE, stats=None):
    """
    下載一個影片，成功回傳 (檔案路徑, 警告)；警告平常是 None。
    fmt：collect_formats() 回傳的其中一個畫質
    cancel_event：設成 set() 就會停止，並丟出 Cancelled
    name_template：FILENAME_TEMPLATES 裡的名字
    stats：傳一個 dict 進來，下載完會填上實際的 bytes（總大小）和 seconds（下載花的秒數，不含轉檔）
    """
    touched = set()          # 這次下載碰過的檔案（取消時要清掉）
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
        "retries": 10,
        "fragment_retries": 10,
        "merge_output_format": "mp4",
    }
    if FFMPEG:
        opts["ffmpeg_location"] = FFMPEG
        opts["postprocessors"] = [{"key": "FFmpegVideoRemuxer", "preferedformat": "mp4"}]

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
                raw["extractor_key"] = {"pornhub.com": "PornHub", "xvideos.com": "XVideos"}.get(
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
                done = parallel_download(info["url"], {**(info.get("http_headers") or {})}, path,
                                         on_progress=on_progress, cancel_event=cancel_event,
                                         touched=touched)
                if done:
                    clock["start"], clock["end"] = done["start"], done["end"]
                    sizes[path] = done["bytes"]
                    final["path"] = path
            if not done:
                ydl.process_ie_result(info, download=True)
    except BaseException as e:
        # yt-dlp 會把進度回呼裡丟出的例外包起來，所以也要檢查 cancel_event
        cancelled = isinstance(e, Cancelled) or (cancel_event is not None and cancel_event.is_set())
        if cancelled:
            n = cleanup_partials(touched, include_complete=True)
            log.info("已取消：%s（清掉 %d 個殘檔）", url, n)
            raise Cancelled() from None
        # 影片已經下載完，只是最後的修正／轉檔失敗 → 保留檔案，不要讓使用者白等
        if "Postprocessing" in str(e) and final["path"] and os.path.isfile(final["path"]):
            cleanup_partials(touched)
            if stats is not None and clock["start"] and clock["end"]:
                stats.update(bytes=sum(sizes.values()), seconds=max(0.01, clock["end"] - clock["start"]))
            log.warning("下載完成但轉檔失敗，保留原檔：%s → %s（%s）", url, final["path"], e)
            return final["path"], "下載完成，但轉檔失敗；檔案還是可以播放（建議用 VLC）"
        n = cleanup_partials(touched)
        log.exception("下載失敗：%s（%s，清掉 %d 個暫存檔）", url, quality_label(fmt.get("height")), n)
        raise
    if cancel_event is not None and cancel_event.is_set():
        cleanup_partials(touched, include_complete=True)
        raise Cancelled()
    if stats is not None and clock["start"] and clock["end"]:
        stats.update(bytes=sum(sizes.values()), seconds=max(0.01, clock["end"] - clock["start"]))
    log.info("下載完成：%s → %s", url, final["path"])
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


# ======================================================================
# 視窗介面
# ======================================================================

import tkinter as tk                                   # noqa: E402
from tkinter import ttk, messagebox, filedialog        # noqa: E402


class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.settings = Settings()
        self.history = History()
        self.stats = Stats()
        self.auto_backup = auto_backup_on_upgrade(self.settings, self.history)
        self.tray = None               # 系統匣圖示（第一次縮下去時才建立）
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
        self.worker = None
        self.want_height = None        # 從紀錄重新下載時，想要的畫質
        # 真正做事的函式（測試時可以換成假的）
        self.analyze_fn = analyze_url
        self.analyze_media_fn = analyze_media
        self.download_fn = run_download
        # 瀏覽器連動
        self.remote = {}               # 配對的瀏覽器送來的任務：ticket → 狀態
        self.last_clip = ""
        self.bridge = Bridge(self.settings,
                             on_enqueue=self.remote_register,
                             get_status=self.remote_status)

        root.title(f"Video Helper v{VERSION}")
        root.geometry("800x700")
        root.minsize(700, 620)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        tabs = {}
        for key, text in (("setup", "① 安裝擋廣告（第一次用）"), ("dl", "② 下載影片"),
                          ("hist", "③ 下載紀錄"), ("set", "④ 設定"), ("stats", "⑤ 統計")):
            tabs[key] = ttk.Frame(self.nb)
            self.nb.add(tabs[key], text=f"  {text}  ")
        self.tab_dl = tabs["dl"]
        self.build_setup(tabs["setup"])
        self.build_download(tabs["dl"])
        self.build_history(tabs["hist"])
        self.build_settings(tabs["set"])
        self.build_stats(tabs["stats"])

        if self.settings["bridge_enabled"]:
            self.bridge.start()
            self.refresh_bridge_label()
        root.bind("<FocusIn>", lambda e: self.check_clipboard() if e.widget is root else None)

        self.poll()
        threading.Thread(target=check_update, args=(self.q,), daemon=True).start()
        log.info("啟動 v%s", VERSION)

    # 讓背景工作把結果丟回主畫面
    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "on_" + kind)(data)
        except queue.Empty:
            pass
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
                  foreground="#555").pack(anchor="w", pady=(2, 12))

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
            ttk.Label(txt, text=hint, foreground="#555", justify="left").pack(anchor="w")
            mark = ttk.Label(box, text="", foreground="#2e7d32", width=4)
            mark.pack(side="right")
            ttk.Button(box, text="打開", command=lambda u=url, m=mark: self.open_in_chrome(u, m)).pack(side="right", padx=6)
            self.step_marks.append(mark)

        ttk.Separator(f).pack(fill="x", pady=12)
        ttk.Label(f, text="✅ 完成後：打開影片網站，左下角出現綠色盾牌「🛡 已攔截 N 次」就成功了。",
                  wraplength=640, justify="left").pack(anchor="w")
        if not self.chrome:
            ttk.Label(f, text="⚠ 找不到 Chrome，會改用預設瀏覽器打開。這個工具是為 Chrome 設計的。",
                      foreground="#c62828", wraplength=640).pack(anchor="w", pady=(8, 0))

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
        messagebox.showinfo("檢查更新", f"目前已經是最新版本（v{VERSION}）。")

    def on_update_failed(self, msg):
        messagebox.showwarning("檢查更新", f"無法連到 GitHub 檢查更新：\n{msg[:200]}")

    def on_update(self, data):
        tag, url, notes = data
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
        self.btn_read = ttk.Button(top, text="讀取", command=self.analyze)
        self.btn_read.pack(side="left")
        self.btn_batch = ttk.Button(top, text="📋 多個網址…", command=self.open_batch)
        self.btn_batch.pack(side="left", padx=(6, 0))

        # 剪貼簿裡有影片網址時，出現這一列（不會自動做任何事，要你按）
        # 放在分頁的上面，不管現在在哪個分頁都看得到
        self.clip_bar = ttk.Frame(self.root)
        self.clip_lbl = ttk.Label(self.clip_bar, text="", foreground="#1565c0")
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
        self.tree.bind("<Double-1>", lambda e: self.download())

        mid = ttk.Frame(dl)
        mid.pack(fill="x", **pad)
        self.speed_lbl = ttk.Label(mid, text=f"網速：{self.mbps:.1f} Mbps（上次實測）" if self.mbps else "網速：尚未量測")
        self.speed_lbl.pack(side="left")
        ttk.Button(mid, text="打開下載資料夾", command=lambda: open_path(self.settings.download_dir)).pack(side="right")
        self.btn_dl = ttk.Button(mid, text="⬇ 加入下載", command=self.download, state="disabled")
        self.btn_dl.pack(side="right", padx=6)

        # 佇列
        ttk.Separator(dl).pack(fill="x", padx=10, pady=(6, 2))
        self.qhead = ttk.Label(dl, text="下載佇列（依序下載）", font=("", 10, "bold"))
        self.qhead.pack(anchor="w", padx=10)
        qcols = ("title", "quality", "status")
        self.qtree = ttk.Treeview(dl, columns=qcols, show="headings", height=5, selectmode="browse")
        for c, name, w, anchor in (("title", "標題", 470, "w"), ("quality", "畫質", 80, "center"),
                                   ("status", "狀態", 150, "center")):
            self.qtree.heading(c, text=name)
            self.qtree.column(c, width=w, anchor=anchor)
        self.qtree.pack(fill="both", expand=True, **pad)

        qbar = ttk.Frame(dl)
        qbar.pack(fill="x", **pad)
        self.btn_cancel = ttk.Button(qbar, text="■ 取消目前下載", command=self.cancel, state="disabled")
        self.btn_cancel.pack(side="left")
        self.btn_retry = ttk.Button(qbar, text="↻ 重試失敗的", command=self.retry, state="disabled")
        self.btn_retry.pack(side="left", padx=6)
        ttk.Button(qbar, text="移除選取", command=self.queue_remove).pack(side="left")
        ttk.Button(qbar, text="清除已結束", command=self.queue_clear).pack(side="right")

        self.prog = ttk.Progressbar(dl, maximum=100)
        self.prog.pack(fill="x", **pad)
        self.status = ttk.Label(dl, text="" if FFMPEG else "⚠ 找不到 ffmpeg，部分影片可能無法轉成 MP4")
        self.status.pack(fill="x", **pad)

    def set_buttons(self):
        """依目前狀態決定按鈕能不能按"""
        self.btn_read.config(state="disabled" if self.analyzing else "normal")
        self.btn_batch.config(state="disabled" if self.batch_running else "normal")
        self.btn_dl.config(state="normal" if (self.formats and not self.analyzing) else "disabled")
        self.btn_cancel.config(state="normal" if self.cancel_event else "disabled")
        self.btn_retry.config(state="normal" if self.queue.count(FAILED) else "disabled")

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
            log.exception("讀取失敗：%s", url)
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
        self.title_lbl.config(text="❌ " + msg[:200] + "　（詳細原因在「④ 設定」→ 打開錯誤日誌）")
        self.set_buttons()

    def download(self):
        """把選取的畫質加進佇列"""
        sel = self.tree.selection()
        if not sel or self.analyzing:
            return
        f = self.formats[int(sel[0])]
        if self.queue.is_duplicate(self.url, f["height"]):
            messagebox.showinfo("已經在佇列裡", "這部影片的這個畫質已經在排隊或下載中了。")
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
        win.geometry("560x360")
        win.transient(self.root)
        ttk.Label(win, text="一行貼一個網址。每部都會用「預設畫質」"
                            f"（目前：{self.settings['default_quality']}）加入佇列。",
                  wraplength=520).pack(anchor="w", padx=10, pady=(10, 4))
        txt = tk.Text(win, height=12)
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

    def start_batch(self, urls):
        self.batch_running = True
        self.set_buttons()
        self.status.config(text=f"讀取網址中 0/{len(urls)}…")
        threading.Thread(target=self._batch_worker, args=(urls, self.settings["default_quality"]), daemon=True).start()

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
                log.exception("批次讀取失敗：%s", url)
                self.q.put(("batch_item_error", (url, str(e))))
                fail += 1
        self.q.put(("batch_done", (ok, fail)))

    def on_batch_status(self, text):
        if not self.queue.running():
            self.status.config(text=text)

    def on_batch_item(self, data):
        task, mbps = data
        self.remember_speed(mbps)
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
        msg = f"已加入 {ok} 部" + (f"，{fail} 部讀取失敗（原因在「③ 下載紀錄」）" if fail else "")
        # 不跳視窗打斷使用者，寫在佇列標題旁邊
        self.refresh_queue()
        self.qhead.config(text=self.qhead.cget("text") + f"　｜　批次：{msg}")

    # ----- 佇列 -----

    def enqueue(self, task):
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
        """沒有東西在下載、又有人在排隊 → 開始下一個"""
        if self.queue.running():
            return
        t = self.queue.next_waiting()
        if not t:
            self.set_buttons()
            return
        t["status"] = RUNNING
        self.cancel_event = threading.Event()
        self.prog["value"] = 0
        self.status.config(text=f"準備下載：{t['title']}")
        self.refresh_queue()
        self.set_buttons()
        self.worker = threading.Thread(target=self._download_worker, args=(t, self.cancel_event), daemon=True)
        self.worker.start()

    def _download_worker(self, task, cancel_event):
        tid = task["id"]
        measured = {}
        try:
            result = self.download_fn(task["url"], task["fmt"], task["out"],
                                      on_progress=lambda *a: self.q.put(("progress", (tid,) + a)),
                                      on_stage=lambda t: self.q.put(("progress_text", (tid, t))),
                                      cancel_event=cancel_event,
                                      name_template=self.settings["filename_template"],
                                      stats=measured)
            task["measured"] = measured
            self.q.put(("done", (tid, result)))
        except Cancelled:
            self.q.put(("cancelled", tid))
        except Exception as e:
            self.q.put(("download_error", (tid, f"{e}")))

    def refresh_queue(self):
        sel = self.qtree.selection()
        self.qtree.delete(*self.qtree.get_children())
        icon = {WAITING: "⏳ 等待中", DONE: "✅ 完成", FAILED: "❌ 失敗", CANCELLED: "■ 取消"}
        for t in self.queue.tasks:
            st = f"⬇ {t['progress']:.0f}%" if t["status"] == RUNNING else icon.get(t["status"], t["status"])
            if t.get("note") and t["status"] == DONE:
                st = "⚠ 完成（轉檔失敗）"
            self.qtree.insert("", "end", iid=str(t["id"]), values=(t["title"], quality_label(t["fmt"]["height"]), st))
        if sel and self.qtree.exists(sel[0]):
            self.qtree.selection_set(sel[0])
        w, r = self.queue.count(WAITING), self.queue.count(RUNNING)
        self.qhead.config(text=f"下載佇列（依序下載）　下載中 {r}　排隊中 {w}")

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
        self.queue.remove(int(sel[0]))
        self.refresh_queue()
        self.set_buttons()

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
        if speed:
            parts.append(f"{speed * 8 / 1e6:.1f} Mbps")
            self.mbps = speed * 8 / 1e6
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

    def finish_task(self, tid, status, file=None, error=None):
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
                                 status=status, file=file or "", size=t.get("size") or 0, error=error or "")
                self.refresh_history()
        self.cancel_event = None
        self.worker = None
        self.refresh_queue()
        self.pump()                   # 換下一個
        self.set_buttons()
        if self.is_hidden() and t and status in (DONE, FAILED) and not self.quitting:
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

    def on_download_error(self, data):
        tid, msg = data
        self.status.config(text="❌ 下載失敗：" + msg[:160] + "　→ 可以按「↻ 重試失敗的」")
        self.finish_task(tid, FAILED, error=msg[:500])

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
                        log.exception("瀏覽器給的影片位址讀不到，改開網頁：%s", url)
                title, dur, fmts, mbps = self.analyze_fn(url)
                self.q.put(("remote_analyzed", (ticket, url, h, title, fmts, mbps)))
            except Exception as e:
                log.exception("瀏覽器送來的網址讀取失敗：%s", url)
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

    def refresh_bridge_label(self):
        if not hasattr(self, "bridge_lbl"):
            return
        n = len(self.settings["bridge_tokens"] or [])
        if self.bridge.server:
            self.bridge_lbl.config(text=f"✅ 執行中（127.0.0.1:{self.bridge.port}）｜已配對 {n} 個瀏覽器", foreground="#2e7d32")
        elif self.settings["bridge_enabled"]:
            self.bridge_lbl.config(text="⚠ 無法啟動（連接埠都被占用）", foreground="#c62828")
        else:
            self.bridge_lbl.config(text="關閉中（按「配對」會打開）", foreground="#555")

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
        win.geometry("440x300")
        win.transient(self.root)
        ttk.Label(win, text="在影片網頁按右下角「⬇ 下載」→「🖥 送到電腦版」，\n跳出配對視窗時輸入：",
                  justify="center").pack(pady=(18, 8))
        ttk.Label(win, text=f"{code[:3]} {code[3:]}", font=("Consolas", 32, "bold")).pack()
        left = ttk.Label(win, text="", foreground="#555")
        left.pack(pady=8)
        ttk.Label(win, text="配對碼 5 分鐘內有效、只能用一次。\n配對過的瀏覽器之後就不用再輸入。",
                  foreground="#555", justify="center").pack()
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
            self.bridge.unpair_all()
            self.refresh_bridge_label()

    def stop_bridge(self):
        self.bridge.stop()
        self.settings["bridge_enabled"] = False
        self.settings.save()
        self.refresh_bridge_label()

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
        log.info("關閉")
        self.root.destroy()

    def start_tray(self):
        if self.tray or pystray is None:
            return self.tray is not None
        try:
            # 系統匣的按鈕是在另一個執行緒觸發的，一律透過 self.q 交給主畫面處理
            menu = pystray.Menu(
                pystray.MenuItem("打開 Video Helper", lambda: self.q.put(("show_window", None)), default=True),
                pystray.MenuItem("結束", lambda: self.q.put(("tray_quit", None))))
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
        self.root.withdraw()
        if not self.tray_hinted:
            self.tray_hinted = True
            self.notify("Video Helper 在背景執行中" + ("，下載會繼續" if self.downloads_active() else "") +
                        "。點右下角的圖示可以打開視窗。")
        log.info("縮到系統匣")

    def is_hidden(self):
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

    def on_show_window(self, _):
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
        self.htree = ttk.Treeview(parent, columns=cols, show="headings", selectmode="browse")
        for c, name, w, anchor in (("time", "時間", 130, "center"), ("title", "標題", 380, "w"),
                                   ("quality", "畫質", 70, "center"), ("status", "狀態", 70, "center")):
            self.htree.heading(c, text=name)
            self.htree.column(c, width=w, anchor=anchor)
        self.htree.pack(fill="both", expand=True, **pad)
        self.htree.bind("<<TreeviewSelect>>", lambda e: self.show_history_detail())
        self.htree.bind("<Double-1>", lambda e: self.history_redownload())

        self.hdetail = ttk.Label(parent, text="", foreground="#555", wraplength=680, justify="left")
        self.hdetail.pack(fill="x", **pad)

        bar = ttk.Frame(parent)
        bar.pack(fill="x", **pad)
        ttk.Button(bar, text="↻ 重新下載", command=self.history_redownload).pack(side="left")
        ttk.Button(bar, text="📂 打開檔案位置", command=self.history_open).pack(side="left", padx=6)
        ttk.Button(bar, text="刪除這筆", command=self.history_delete).pack(side="left")
        ttk.Button(bar, text="清除全部", command=self.history_clear).pack(side="right")
        self.refresh_history()

    def refresh_history(self):
        self.htree.delete(*self.htree.get_children())
        icon = {"完成": "✅ 完成", "失敗": "❌ 失敗", "取消": "■ 取消"}
        for i, r in enumerate(self.history.items):
            self.htree.insert("", "end", iid=str(i), values=(
                r.get("time", ""), r.get("title", ""), quality_label(r.get("height")), icon.get(r.get("status"), r.get("status", ""))))
        if not self.history.items:
            self.hdetail.config(text="還沒有紀錄。" if self.settings["keep_history"] else "已關閉下載紀錄（可在「④ 設定」打開）。")

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
    # ④ 設定
    # ------------------------------------------------------------------

    def build_settings(self, parent):
        outer = ttk.Frame(parent, padding=(12, 8))
        outer.pack(fill="both", expand=True)
        box = lambda title: ttk.LabelFrame(outer, text=f" {title} ", padding=(10, 4))

        # --- 下載 ---
        f = box("下載")
        f.pack(fill="x", pady=4)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="下載資料夾：").grid(row=0, column=0, sticky="w", pady=3)
        self.dir_var = tk.StringVar(value=self.settings.download_dir)
        ttk.Entry(f, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="瀏覽…", command=self.choose_dir).grid(row=0, column=2)

        ttk.Label(f, text="預設畫質：").grid(row=1, column=0, sticky="w", pady=3)
        self.q_var = tk.StringVar(value=self.settings["default_quality"])
        ttk.Combobox(f, textvariable=self.q_var, values=QUALITY_CHOICES, state="readonly", width=10).grid(
            row=1, column=1, sticky="w", padx=6)
        ttk.Label(f, text="（沒有這個畫質時，選比它低的最高畫質）", foreground="#555").grid(
            row=1, column=1, sticky="w", padx=(110, 0))

        ttk.Label(f, text="檔名格式：").grid(row=2, column=0, sticky="w", pady=3)
        tpl = self.settings["filename_template"]
        self.tpl_var = tk.StringVar(value=tpl if tpl in FILENAME_TEMPLATES else DEFAULT_TEMPLATE)
        cb = ttk.Combobox(f, textvariable=self.tpl_var, values=list(FILENAME_TEMPLATES), state="readonly", width=28)
        cb.grid(row=2, column=1, sticky="w", padx=6)
        self.tpl_preview = ttk.Label(f, text="", foreground="#555")
        self.tpl_preview.grid(row=3, column=1, columnspan=2, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_tpl_preview())
        self.update_tpl_preview()

        # --- 程式 ---
        g = box("程式")
        g.pack(fill="x", pady=4)
        self.keep_var = tk.BooleanVar(value=bool(self.settings["keep_history"]))
        ttk.Checkbutton(g, text="保存下載紀錄", variable=self.keep_var).grid(row=0, column=0, columnspan=2, sticky="w", pady=3)
        self.keep_stats_var = tk.BooleanVar(value=bool(self.settings["keep_stats"]))
        ttk.Checkbutton(g, text="記錄下載統計（用來校正預估；只存數字和網站名稱）", variable=self.keep_stats_var).grid(
            row=0, column=2, sticky="w", pady=3)
        ttk.Label(g, text="關閉視窗時：").grid(row=1, column=0, sticky="w", pady=3)
        ca = self.settings["close_action"]
        self.close_var = tk.StringVar(value=ca if ca in CLOSE_CHOICES else CLOSE_CHOICES[0])
        choices = CLOSE_CHOICES if pystray else [c for c in CLOSE_CHOICES if "系統匣" not in c]
        ttk.Combobox(g, textvariable=self.close_var, values=choices, state="readonly", width=16).grid(
            row=1, column=1, sticky="w", padx=6)
        ttk.Label(g, text=("縮到系統匣＝縮到右下角繼續下載，完成會跳通知" if pystray else "（這台電腦不支援系統匣）"),
                  foreground="#555").grid(row=1, column=2, sticky="w", padx=6)

        ttk.Button(outer, text="💾 儲存設定", command=self.save_settings).pack(anchor="w", pady=(4, 6))

        # --- 瀏覽器連動 ---
        c = box("瀏覽器連動（在影片網頁按一下，就送進電腦版下載）")
        c.pack(fill="x", pady=4)
        self.bridge_lbl = ttk.Label(c, text="")
        self.bridge_lbl.pack(side="left", pady=3)
        ttk.Button(c, text="關閉服務", command=self.stop_bridge).pack(side="right")
        ttk.Button(c, text="取消所有配對", command=self.unpair_browsers).pack(side="right", padx=6)
        ttk.Button(c, text="🔗 配對瀏覽器…", command=self.pair_browser).pack(side="right")
        self.refresh_bridge_label()

        # --- 備份 ---
        b = box("備份")
        b.pack(fill="x", pady=4)
        ttk.Button(b, text="⬆ 匯出設定和紀錄…", command=self.export_settings).pack(side="left", pady=3)
        ttk.Button(b, text="⬇ 匯入…", command=self.import_settings).pack(side="left", padx=6)
        ttk.Button(b, text="📂 自動備份資料夾", command=self.open_backups).pack(side="left")
        ttk.Label(b, text="換電腦、重灌時用；更新版本時也會自動備份", foreground="#555").pack(side="left", padx=8)

        # --- 版本 ---
        v = box("版本")
        v.pack(fill="x", pady=4)
        ttk.Label(v, text=f"目前版本：v{VERSION}").pack(side="left", pady=3)
        ttk.Button(v, text="檢查更新", command=self.manual_update_check).pack(side="left", padx=8)
        ttk.Button(v, text="查看更新內容", command=lambda: webbrowser.open(f"https://github.com/{REPO}/releases")).pack(side="left")

        # --- 問題排查 ---
        t = box("問題排查")
        t.pack(fill="x", pady=4)
        ttk.Button(t, text="📄 打開錯誤日誌", command=self.open_log).pack(side="left", pady=3)
        ttk.Button(t, text="📂 打開設定資料夾", command=lambda: open_path(data_dir())).pack(side="left", padx=6)
        ttk.Button(t, text="清除網速紀錄", command=self.reset_speed).pack(side="left")
        ttk.Label(outer, text=f"設定資料夾：{data_dir()}", foreground="#888").pack(anchor="w", pady=(6, 0))

    def apply_settings_to_ui(self):
        """設定被匯入後，讓畫面上的選項跟著改"""
        self.dir_var.set(self.settings.download_dir)
        self.q_var.set(self.settings["default_quality"])
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
        ttk.Label(parent, foreground="#555", wraplength=740, justify="left",
                  text="誤差＝原本的預估跟實際差多少（中位數）。同一個網站下載 3 次以上，之後的預估會自動用這些結果校正。\n"
                       "只記錄網站名稱和數字（大小、秒數、畫質），不記錄影片標題和網址。可以在「④ 設定」關掉。").pack(fill="x", **pad)
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
    r["ok"] = bool(ok)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(r, f, ensure_ascii=False, indent=1)
    return 0 if ok else 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        i = sys.argv.index("--selftest")
        sys.exit(selftest(sys.argv[i + 1] if len(sys.argv) > i + 1 else "selftest.json"))
    root = tk.Tk()
    App(root)
    root.mainloop()

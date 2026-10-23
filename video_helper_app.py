"""
Video Helper 電腦版
分頁 ①「安裝擋廣告」：一步一步帶使用者在 Chrome 裝好暴力猴和 Video Helper 腳本
分頁 ②「下載影片」：貼上網址 → 選畫質 → 加入下載佇列（一次排很多部，依序下載；可取消、可重試）
分頁 ③「下載紀錄」：完成／失敗／取消的任務，可重新下載、打開檔案位置
分頁 ④「設定」：下載資料夾、預設畫質、檔名格式、是否保存紀錄、錯誤日誌

執行前先安裝：
    pip install yt-dlp imageio-ffmpeg
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

import yt_dlp

FFMPEG = os.environ.get("VIDEO_HELPER_FFMPEG")      # 測試時可以指定別的 ffmpeg
if not FFMPEG:
    try:
        import imageio_ffmpeg
        FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()       # 內建 ffmpeg，用來合併與轉成 MP4
    except Exception:
        FFMPEG = None

VERSION = "1.5.0"
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
    "filename_template": DEFAULT_TEMPLATE,
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

    def save(self):
        try:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except Exception:
            log.exception("儲存下載紀錄失敗")


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


def check_update(q):
    """問 GitHub 有沒有比現在更新的版本（連同更新內容）"""
    try:
        req = urllib.request.Request(f"https://api.github.com/repos/{REPO}/releases/latest",
                                     headers={"User-Agent": "VideoHelper"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.load(r)
        tag = (data.get("tag_name") or "").lstrip("v")
        ver = lambda v: tuple(int(x) for x in v.split("."))
        if tag and ver(tag) > ver(VERSION):
            q.put(("update", (tag, data.get("html_url"), (data.get("body") or "").strip())))
    except Exception:
        pass


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


def run_download(url, fmt, out_dir, on_progress=None, on_stage=None, cancel_event=None,
                 name_template=DEFAULT_TEMPLATE):
    """
    下載一個影片，成功回傳 (檔案路徑, 警告)；警告平常是 None。
    fmt：collect_formats() 回傳的其中一個畫質
    cancel_event：設成 set() 就會停止，並丟出 Cancelled
    name_template：FILENAME_TEMPLATES 裡的名字
    """
    touched = set()          # 這次下載碰過的檔案（取消時要清掉）
    final = {"path": None}

    def hook(d):
        if cancel_event is not None and cancel_event.is_set():
            raise Cancelled()
        fn = d.get("filename")
        if fn:
            touched.add(fn)
        if d.get("tmpfilename"):
            touched.add(d["tmpfilename"])
        if d["status"] == "downloading" and on_progress:
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            on_progress(d.get("downloaded_bytes", 0), total, d.get("speed"), d.get("eta"))
        elif d["status"] == "finished":
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

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.download([url])
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
            log.warning("下載完成但轉檔失敗，保留原檔：%s → %s（%s）", url, final["path"], e)
            return final["path"], "下載完成，但轉檔失敗；檔案還是可以播放（建議用 VLC）"
        n = cleanup_partials(touched)
        log.exception("下載失敗：%s（%s，清掉 %d 個暫存檔）", url, quality_label(fmt.get("height")), n)
        raise
    if cancel_event is not None and cancel_event.is_set():
        cleanup_partials(touched, include_complete=True)
        raise Cancelled()
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
        self.download_fn = run_download

        root.title(f"Video Helper v{VERSION}")
        root.geometry("780x640")
        root.minsize(680, 560)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.nb = ttk.Notebook(root)
        self.nb.pack(fill="both", expand=True)
        tabs = {}
        for key, text in (("setup", "① 安裝擋廣告（第一次用）"), ("dl", "② 下載影片"),
                          ("hist", "③ 下載紀錄"), ("set", "④ 設定")):
            tabs[key] = ttk.Frame(self.nb)
            self.nb.add(tabs[key], text=f"  {text}  ")
        self.tab_dl = tabs["dl"]
        self.build_setup(tabs["setup"])
        self.build_download(tabs["dl"])
        self.build_history(tabs["hist"])
        self.build_settings(tabs["set"])

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
        self.remember_speed(mbps)
        self.analyzing = False
        self.title_lbl.config(text=f"{self.title}　（長度 {fmt_duration(self.duration)}）")
        if not self.mbps:
            self.speed_lbl.config(text="網速：無法量測（下載時會顯示實際速度）")
        if not self.formats:
            self.title_lbl.config(text=f"{self.title}：找不到可下載的畫質")
            self.set_buttons()
            return
        for i, f in enumerate(self.formats):
            size = ("" if f["exact"] or not f["size"] else "約 ") + fmt_size(f["size"])
            t = fmt_time(f["size"] * 8 / (self.mbps * 1e6)) if f["size"] and self.mbps else "時間未知"
            kind = "MP4 直連" if f["direct"] else "切片"
            self.tree.insert("", "end", iid=str(i), values=(quality_label(f["height"]), size, t, kind))

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
        try:
            result = self.download_fn(task["url"], task["fmt"], task["out"],
                                      on_progress=lambda *a: self.q.put(("progress", (tid,) + a)),
                                      on_stage=lambda t: self.q.put(("progress_text", (tid, t))),
                                      cancel_event=cancel_event,
                                      name_template=self.settings["filename_template"])
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

    def on_progress_text(self, data):
        tid, text = data
        self.status.config(text=text)

    def finish_task(self, tid, status, file=None, error=None):
        t = self.queue.get(tid)
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

    def on_close(self):
        waiting = self.queue.count(WAITING)
        if self.cancel_event or waiting:
            msg = "還在下載，確定要離開嗎？\n目前的下載會被取消，已下載的部分會被刪除。"
            if waiting:
                msg += f"\n排隊中的 {waiting} 部也不會下載。"
            if not messagebox.askyesno("下載中", msg):
                return
            for t in self.queue.tasks:
                if t["status"] == WAITING:
                    t["status"] = CANCELLED
            if self.cancel_event:
                self.cancel_event.set()
            if self.worker:
                self.worker.join(timeout=5)     # 等它清完殘檔
        log.info("關閉")
        self.root.destroy()

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
        f = ttk.Frame(parent, padding=16)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)

        ttk.Label(f, text="下載資料夾：").grid(row=0, column=0, sticky="w", pady=6)
        self.dir_var = tk.StringVar(value=self.settings.download_dir)
        ttk.Entry(f, textvariable=self.dir_var).grid(row=0, column=1, sticky="ew", padx=6)
        ttk.Button(f, text="瀏覽…", command=self.choose_dir).grid(row=0, column=2)

        ttk.Label(f, text="預設畫質：").grid(row=1, column=0, sticky="w", pady=6)
        self.q_var = tk.StringVar(value=self.settings["default_quality"])
        ttk.Combobox(f, textvariable=self.q_var, values=QUALITY_CHOICES, state="readonly", width=10).grid(
            row=1, column=1, sticky="w", padx=6)
        ttk.Label(f, text="讀取影片後自動選這個畫質（沒有的話選比它低的最高畫質）",
                  foreground="#555").grid(row=2, column=1, columnspan=2, sticky="w", padx=6)

        ttk.Label(f, text="檔名格式：").grid(row=3, column=0, sticky="w", pady=(12, 6))
        tpl = self.settings["filename_template"]
        self.tpl_var = tk.StringVar(value=tpl if tpl in FILENAME_TEMPLATES else DEFAULT_TEMPLATE)
        cb = ttk.Combobox(f, textvariable=self.tpl_var, values=list(FILENAME_TEMPLATES), state="readonly", width=28)
        cb.grid(row=3, column=1, sticky="w", padx=6, pady=(12, 6))
        self.tpl_preview = ttk.Label(f, text="", foreground="#555")
        self.tpl_preview.grid(row=4, column=1, columnspan=2, sticky="w", padx=6)
        cb.bind("<<ComboboxSelected>>", lambda e: self.update_tpl_preview())
        self.update_tpl_preview()

        self.keep_var = tk.BooleanVar(value=bool(self.settings["keep_history"]))
        ttk.Checkbutton(f, text="保存下載紀錄", variable=self.keep_var).grid(row=5, column=1, sticky="w", padx=6, pady=(12, 6))

        ttk.Button(f, text="💾 儲存設定", command=self.save_settings).grid(row=6, column=1, sticky="w", padx=6, pady=(6, 18))

        ttk.Separator(f).grid(row=7, column=0, columnspan=3, sticky="ew", pady=6)
        ttk.Label(f, text="問題排查", font=("", 11, "bold")).grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 2))
        ttk.Label(f, text="下載或讀取失敗時，詳細原因會寫進錯誤日誌。回報問題時把日誌檔傳給開發者。",
                  foreground="#555", wraplength=600).grid(row=9, column=0, columnspan=3, sticky="w")
        bar = ttk.Frame(f)
        bar.grid(row=10, column=0, columnspan=3, sticky="w", pady=8)
        ttk.Button(bar, text="📄 打開錯誤日誌", command=self.open_log).pack(side="left")
        ttk.Button(bar, text="📂 打開設定資料夾", command=lambda: open_path(data_dir())).pack(side="left", padx=6)
        ttk.Button(bar, text="清除網速紀錄", command=self.reset_speed).pack(side="left")
        ttk.Label(f, text=f"設定資料夾：{data_dir()}　｜　版本 v{VERSION}", foreground="#888").grid(
            row=11, column=0, columnspan=3, sticky="w", pady=(12, 0))

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


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

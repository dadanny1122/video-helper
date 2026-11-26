"""v1.48.0 五個新功能的共用邏輯（不碰視窗，方便測試）；規格 docs/infra/05-五個新功能規格.md

1. 密碼鎖：PIN 雜湊、錯誤等待
2. 稍後下載清單：later.json
3. 定時下載：時段、限速
4. 自動整理：存檔資料夾、找重複、丟資源回收筒
5. 下載完成通知手機：Discord Webhook
"""
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime

# ======================================================================
# 1. 密碼鎖
# ======================================================================

PIN_RE = re.compile(r"\d{4,12}")
PIN_ITER = 200_000
LOCK_IDLE_CHOICES = (0, 5, 15, 30, 60)       # 分鐘；0＝不會自己鎖
WRONG_FREE = 5                                # 前 5 次打錯不用等
WAIT_FIRST, WAIT_MAX = 30, 15 * 60


def pin_ok_format(pin):
    return isinstance(pin, str) and bool(PIN_RE.fullmatch(pin))


def make_pin(pin, iterations=PIN_ITER):
    """PIN → 要存的東西（不存原本的數字）"""
    if not pin_ok_format(pin):
        raise ValueError("密碼要 4～12 位數字")
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pin.encode(), salt, iterations)
    return {"v": 1, "iter": iterations, "salt": salt.hex(), "hash": h.hex()}


def check_pin(rec, pin):
    if not (isinstance(rec, dict) and isinstance(pin, str) and pin_ok_format(pin)):
        return False
    try:
        h = hashlib.pbkdf2_hmac("sha256", pin.encode(), bytes.fromhex(rec["salt"]), int(rec["iter"]))
        return hmac.compare_digest(h.hex(), str(rec["hash"]))
    except Exception:
        return False


def has_pin(rec):
    return isinstance(rec, dict) and bool(rec.get("hash")) and bool(rec.get("salt"))


class Lock:
    """鎖住／解鎖的狀態（只放記憶體）。打錯 5 次以後要等 30 秒，之後每次加倍，最多 15 分鐘"""

    def __init__(self, clock=time.time):
        self.clock = clock
        self.locked = False
        self.wrong = 0
        self.until = 0.0

    def wait_left(self):
        return max(0, int(self.until - self.clock() + 0.999))

    def try_unlock(self, rec, pin):
        """回傳 (成功嗎, 訊息)"""
        left = self.wait_left()
        if left:
            return False, f"打錯太多次，請等 {left} 秒再試"
        if check_pin(rec, pin):
            self.locked, self.wrong, self.until = False, 0, 0.0
            return True, "已解鎖"
        self.wrong += 1
        if self.wrong >= WRONG_FREE:
            wait = min(WAIT_MAX, WAIT_FIRST * 2 ** (self.wrong - WRONG_FREE))
            self.until = self.clock() + wait
            return False, f"密碼不對，請等 {wait} 秒再試"
        return False, f"密碼不對（再錯 {WRONG_FREE - self.wrong} 次要等一下）"


# ======================================================================
# 2. 稍後下載清單
# ======================================================================

LATER_MAX = 300


class LaterList:
    """稍後下載：[{url, title, site, time}]，最新的在前；同一部影片（key_fn 一樣）不重複"""

    def __init__(self, path, key_fn):
        self.path, self.key_fn = path, key_fn
        self.lock = threading.Lock()
        self.version = 0
        try:
            with open(path, encoding="utf-8") as f:
                self.items = [r for r in json.load(f) if isinstance(r, dict) and isinstance(r.get("url"), str)]
        except Exception:
            self.items = []

    def add(self, url, title="", site=""):
        """回傳 True＝新加入；False＝已經在清單裡（標題有新的就更新）"""
        k = self.key_fn(url)
        with self.lock:
            for r in self.items:
                if self.key_fn(r["url"]) == k:
                    if title and not r.get("title"):
                        r["title"] = title
                        self._save()
                    return False
            self.items.insert(0, {"url": url, "title": str(title or "")[:200], "site": site,
                                  "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
            del self.items[LATER_MAX:]
            self._save()
            return True

    def remove_key(self, url):
        """同一部影片的都拿掉；回傳拿掉幾個"""
        k = self.key_fn(url)
        with self.lock:
            n = len(self.items)
            self.items = [r for r in self.items if self.key_fn(r["url"]) != k]
            if len(self.items) != n:
                self._save()
            return n - len(self.items)

    def clear(self):
        with self.lock:
            self.items = []
            self._save()

    def _save(self):
        self.version += 1
        tmp = self.path + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.items, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError:
            pass


def later_open_url(url):
    """要瀏覽器打開、讓腳本自動送出的網址（加 #vh-later）"""
    return url.split("#")[0] + "#vh-later"


# ======================================================================
# 3. 定時下載
# ======================================================================

SCHED_MODES = {"關閉": "off", "只在時段內下載": "window", "時段內全速、其他時間限速": "fast_night"}
SCHED_DAY_MBPS = (2, 5, 10, 20)


def in_window(hour, start, end):
    """整點時段 [start, end)；可以跨半夜（例如 23～7）；start == end 當作整天"""
    if start == end:
        return True
    if start < end:
        return start <= hour < end
    return hour >= start or hour < end


def sched_state(st, now=None):
    """回傳 (可以開始下載嗎, 這時候的速度上限 Mbps（0＝不限）, 說明)"""
    now = now or datetime.now()
    mode = st.get("sched_mode", "off")
    base = float(st.get("rate_limit_mbps") or 0)
    s, e = int(st.get("sched_start", 1)), int(st.get("sched_end", 7))
    inside = in_window(now.hour, s, e)
    if mode == "window":
        return inside, base, "" if inside else f"🌙 定時下載：{s}:00～{e}:00 才下載"
    if mode == "fast_night" and not inside:
        day = float(st.get("sched_day_mbps") or 5)
        lim = min(day, base) if base else day
        return True, lim, f"🌙 定時下載：{e}:00～{s}:00 限速 {lim:g} Mbps"
    return True, base, ""


# ======================================================================
# 4. 自動整理資料夾、找重複
# ======================================================================

ORGANIZE_MODES = {"不整理": "off", "依網站分資料夾": "site", "依網站＋月份": "site_month"}
_BAD = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_folder(name):
    n = _BAD.sub("_", str(name or "")).strip(" .")
    return n[:60] or "其他"


def organize_dir(base, mode, site_name, now=None):
    """加入佇列時決定要存到哪個資料夾；off 就是原本的"""
    if mode not in ("site", "site_month"):
        return base
    parts = [base, safe_folder(site_name)]
    if mode == "site_month":
        parts.append((now or datetime.now()).strftime("%Y-%m"))
    return os.path.join(*parts)


def find_duplicates(items, key_fn, done="完成", exists=os.path.isfile):
    """下載紀錄裡同一部影片有 2 個以上檔案還在 → [[(紀錄位置, 紀錄), ...] 畫質高的在前]"""
    groups = {}
    for i, r in enumerate(items):
        f = r.get("file")
        if r.get("status") != done or not f or not exists(f):
            continue
        groups.setdefault(key_fn(r.get("url", "")), []).append((i, r))
    out = []
    for g in groups.values():
        files = {}
        for i, r in g:                     # 同一個檔案（紀錄重複）只算一次
            files.setdefault(os.path.normcase(os.path.abspath(r["file"])), (i, r))
        if len(files) > 1:
            out.append(sorted(files.values(), key=lambda x: (-(x[1].get("height") or 0), -(x[1].get("size") or 0))))
    return out


def recycle_supported():
    return sys.platform == "win32"


def to_recycle_bin(path):
    """丟到資源回收筒（可以救回）；不是 Windows 就拒絕。成功回傳 True"""
    if not recycle_supported():
        raise OSError("只有 Windows 有資源回收筒")
    import ctypes
    from ctypes import wintypes

    class SHFILEOPSTRUCTW(ctypes.Structure):
        _fields_ = [("hwnd", wintypes.HWND), ("wFunc", wintypes.UINT), ("pFrom", wintypes.LPCWSTR),
                    ("pTo", wintypes.LPCWSTR), ("fFlags", ctypes.c_ushort), ("fAnyOperationsAborted", wintypes.BOOL),
                    ("hNameMappings", ctypes.c_void_p), ("lpszProgressTitle", wintypes.LPCWSTR)]
    FO_DELETE, FOF_SILENT, FOF_NOCONFIRMATION, FOF_ALLOWUNDO, FOF_NOERRORUI = 3, 4, 0x10, 0x40, 0x400
    p = os.path.abspath(path)
    if not os.path.isfile(p):
        raise FileNotFoundError(p)
    op = SHFILEOPSTRUCTW(None, FO_DELETE, p + "\0", None,
                         FOF_SILENT | FOF_NOCONFIRMATION | FOF_ALLOWUNDO | FOF_NOERRORUI, False, None, None)
    fn = ctypes.windll.shell32.SHFileOperationW
    fn.argtypes = [ctypes.POINTER(SHFILEOPSTRUCTW)]
    fn.restype = ctypes.c_int
    rc = fn(ctypes.byref(op))
    if rc or op.fAnyOperationsAborted or os.path.exists(p):
        raise OSError(f"丟到資源回收筒失敗（{rc}）")
    return True


# ======================================================================
# 5. 下載完成通知手機（Discord Webhook）
# ======================================================================

WEBHOOK_RE = re.compile(r"https://(?:(?:ptb|canary)\.)?discord(?:app)?\.com/api/webhooks/\d{5,25}/[A-Za-z0-9_\-]{20,120}")
NOTIFY_MODES = {"關閉": "off", "佇列全部做完時": "all_done"}


def webhook_ok(url):
    return isinstance(url, str) and bool(WEBHOOK_RE.fullmatch(url.strip()))


def _size(b):
    if b >= 1e9:
        return f"{b / 1e9:.1f} GB"
    if b >= 1e6:
        return f"{b / 1e6:.0f} MB"
    return f"{b / 1e3:.0f} KB" if b else "0 MB"


def notify_text(done, failed, nbytes):
    """只有數字：不寫標題、網站、網址"""
    if done:
        s = f"✅ Video Helper：下載完成 {done} 部，共 {_size(nbytes)}"
        return s + (f"（失敗 {failed} 部）" if failed else "")
    return f"❌ Video Helper：{failed} 部下載失敗，打開電腦版看原因"


class Batch:
    """這一批（從佇列有東西到全部處理完）完成／失敗幾部、多大"""

    def __init__(self):
        self.done = self.failed = self.bytes = 0

    def add(self, ok, nbytes=0):
        if ok:
            self.done += 1
            self.bytes += int(nbytes or 0)
        else:
            self.failed += 1

    def take(self):
        r = (self.done, self.failed, self.bytes)
        self.done = self.failed = self.bytes = 0
        return r

    def __bool__(self):
        return bool(self.done or self.failed)


def send_webhook(url, text, timeout=10, opener=None):
    """傳一則訊息到 Discord；失敗丟出例外（呼叫的人自己決定要不要提醒）"""
    if not webhook_ok(url):
        raise ValueError("Webhook 網址不對")
    body = json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode()
    req = urllib.request.Request(url.strip(), data=body, method="POST",
                                 headers={"Content-Type": "application/json", "User-Agent": "VideoHelper"})
    with (opener or urllib.request.urlopen)(req, timeout=timeout) as r:
        code = getattr(r, "status", 204)
    if code >= 300:
        raise OSError(f"Discord 回應 {code}")
    return True


def host_of(url):
    return (urllib.parse.urlparse(url or "").hostname or "").lower()

"""影片縮圖（v1.37.0，規格 U3）：請 Windows 檔案總管的縮圖功能幫忙做（跟在資料夾裡看到的一樣）。

- 只讀自己下載好的檔案，不去網站抓、不用加新的影片解碼器
- 做好存在設定資料夾 thumbs\\（小張 JPG，檔名＝路徑＋大小＋修改時間的雜湊，影片換了就重做）
- 一條背景執行緒一張一張做；做不出來（不是 Windows、沒有解碼器、檔案壞掉）就記住，不一直重試
"""
import base64
import hashlib
import os
import queue
import sys
import threading

W, H = 192, 108                      # 16:9，網頁上顯示 64×40 左右（高 DPI 也夠清楚）
MAX_FILES = 2000                     # thumbs 資料夾最多留幾張（超過刪最舊的）


def shell_thumbnail(path, size=256):
    """Windows：IShellItemImageFactory::GetImage（只要縮圖、不要檔案圖示）→ PIL 圖；做不出來回 None"""
    if sys.platform != "win32":
        return None
    import ctypes
    from ctypes import wintypes as wt

    class GUID(ctypes.Structure):
        _fields_ = [("a", ctypes.c_uint32), ("b", ctypes.c_uint16), ("c", ctypes.c_uint16), ("d", ctypes.c_ubyte * 8)]

    class SIZE(ctypes.Structure):
        _fields_ = [("cx", ctypes.c_long), ("cy", ctypes.c_long)]

    class BITMAP(ctypes.Structure):
        _fields_ = [("bmType", ctypes.c_long), ("bmWidth", ctypes.c_long), ("bmHeight", ctypes.c_long),
                    ("bmWidthBytes", ctypes.c_long), ("bmPlanes", wt.WORD), ("bmBitsPixel", wt.WORD), ("bmBits", ctypes.c_void_p)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wt.DWORD), ("biWidth", ctypes.c_long), ("biHeight", ctypes.c_long), ("biPlanes", wt.WORD),
                    ("biBitCount", wt.WORD), ("biCompression", wt.DWORD), ("biSizeImage", wt.DWORD),
                    ("biXPelsPerMeter", ctypes.c_long), ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wt.DWORD),
                    ("biClrImportant", wt.DWORD)]

    # IShellItemImageFactory {bcc18b79-ba16-442f-80c4-8a59c30c463b}
    iid = GUID(0xBCC18B79, 0xBA16, 0x442F, (ctypes.c_ubyte * 8)(0x80, 0xC4, 0x8A, 0x59, 0xC3, 0x0C, 0x46, 0x3B))
    shell32, gdi32, user32 = ctypes.windll.shell32, ctypes.windll.gdi32, ctypes.windll.user32
    shell32.SHCreateItemFromParsingName.argtypes = [wt.LPCWSTR, ctypes.c_void_p, ctypes.POINTER(GUID), ctypes.POINTER(ctypes.c_void_p)]
    shell32.SHCreateItemFromParsingName.restype = ctypes.c_long
    gdi32.GetObjectW.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p]
    gdi32.GetDIBits.argtypes = [wt.HDC, wt.HBITMAP, wt.UINT, wt.UINT, ctypes.c_void_p, ctypes.c_void_p, wt.UINT]
    gdi32.DeleteObject.argtypes = [wt.HANDLE]
    user32.GetDC.argtypes = [wt.HWND]
    user32.GetDC.restype = wt.HDC
    user32.ReleaseDC.argtypes = [wt.HWND, wt.HDC]

    item = ctypes.c_void_p()
    if shell32.SHCreateItemFromParsingName(os.path.abspath(path), None, ctypes.byref(iid), ctypes.byref(item)) != 0 or not item:
        return None
    vtbl = ctypes.cast(ctypes.cast(item, ctypes.POINTER(ctypes.c_void_p))[0], ctypes.POINTER(ctypes.c_void_p))
    release = ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(vtbl[2])
    get_image = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, SIZE, ctypes.c_uint, ctypes.POINTER(wt.HBITMAP))(vtbl[3])
    hbm = wt.HBITMAP()
    try:
        SIIGBF_BIGGERSIZEOK, SIIGBF_THUMBNAILONLY = 0x1, 0x8
        if get_image(item, SIZE(size, size), SIIGBF_BIGGERSIZEOK | SIIGBF_THUMBNAILONLY, ctypes.byref(hbm)) != 0 or not hbm:
            return None
        bm = BITMAP()
        gdi32.GetObjectW(hbm, ctypes.sizeof(bm), ctypes.byref(bm))
        w, h = bm.bmWidth, abs(bm.bmHeight)
        if not (0 < w <= 4096 and 0 < h <= 4096):
            return None
        bih = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), w, -h, 1, 32, 0, 0, 0, 0, 0, 0)   # 由上往下、32 位元
        buf = ctypes.create_string_buffer(w * h * 4)
        dc = user32.GetDC(None)
        try:
            got = gdi32.GetDIBits(dc, hbm, 0, h, buf, ctypes.byref(bih), 0)
        finally:
            user32.ReleaseDC(None, dc)
        if got != h:
            return None
        from PIL import Image
        return Image.frombuffer("RGB", (w, h), buf.raw, "raw", "BGRX", 0, 1)
    finally:
        if hbm:
            gdi32.DeleteObject(hbm)
        release(item)


def fit(img, w=W, h=H):
    """裁成 16:9 置中、縮小"""
    from PIL import Image
    iw, ih = img.size
    scale = max(w / iw, h / ih)
    img = img.resize((max(w, round(iw * scale)), max(h, round(ih * scale))), Image.BILINEAR)
    x, y = (img.size[0] - w) // 2, (img.size[1] - h) // 2
    return img.crop((x, y, x + w, y + h))


def looks_blank(img):
    """全黑／全白（例如影片第一格）當作沒做出來，讓網頁顯示色塊"""
    lo, hi = img.convert("L").getextrema()
    return hi - lo < 8


class ThumbMaker:
    def __init__(self, folder, make=None):
        self.folder = folder
        self.make = make or shell_thumbnail       # 測試可以換成假的
        self.failed = set()
        self.pending = set()
        self.lock = threading.Lock()
        self.q = queue.Queue()
        self.thread = None
        if folder:
            os.makedirs(folder, exist_ok=True)

    def _name(self, path):
        try:
            st = os.stat(path)
        except OSError:
            return None
        h = hashlib.sha1(f"{os.path.abspath(path)}|{st.st_size}|{int(st.st_mtime)}".encode("utf-8", "replace")).hexdigest()
        return os.path.join(self.folder, h[:20] + ".jpg")

    def get(self, path):
        """有做好就回 data:image/jpeg;base64,…；還在做回 None；做不出來回 \"\" """
        if not self.folder or not path:
            return ""
        out = self._name(path)
        if not out:
            return ""
        if os.path.isfile(out):
            with open(out, "rb") as f:
                return "data:image/jpeg;base64," + base64.b64encode(f.read()).decode()
        with self.lock:
            if out in self.failed:
                return ""
            if out not in self.pending:
                self.pending.add(out)
                self.q.put((path, out))
                if self.thread is None:
                    self.thread = threading.Thread(target=self._work, daemon=True, name="thumbs")
                    self.thread.start()
        return None

    def _work(self):
        com = False
        if sys.platform == "win32":
            import ctypes
            com = ctypes.windll.ole32.CoInitializeEx(None, 2) in (0, 1)        # 單執行緒 COM（縮圖要）
        try:
            while True:
                try:
                    path, out = self.q.get(timeout=30)
                except queue.Empty:
                    with self.lock:                       # 確定沒有新工作才結束（get() 會再開一條）
                        if self.q.empty():
                            self.thread = None
                            return
                    continue
                ok = False
                try:
                    img = self.make(path)
                    if img is not None:
                        img = fit(img.convert("RGB"))
                        if not looks_blank(img):
                            tmp = out + ".tmp"
                            img.save(tmp, "JPEG", quality=72, optimize=True)
                            os.replace(tmp, out)
                            ok = True
                except Exception:
                    ok = False
                with self.lock:
                    self.pending.discard(out)
                    if not ok:
                        self.failed.add(out)
                self._trim()
        finally:
            if com:
                import ctypes
                ctypes.windll.ole32.CoUninitialize()

    def _trim(self):
        try:
            names = [os.path.join(self.folder, n) for n in os.listdir(self.folder) if n.endswith(".jpg")]
            if len(names) > MAX_FILES:
                names.sort(key=os.path.getmtime)
                for n in names[:len(names) - MAX_FILES]:
                    os.remove(n)
        except OSError:
            pass

"""
Video Helper 電腦版
貼上影片網址 → 選畫質 → 看大小與預估時間 → 確認 → 下載到「下載」資料夾

執行前先安裝：
    pip install yt-dlp imageio-ffmpeg
執行：
    python video_helper_app.py
"""

import os
import sys
import time
import queue
import threading
import subprocess
import urllib.request
import tkinter as tk
from tkinter import ttk, messagebox

import yt_dlp

try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()   # 內建 ffmpeg，用來轉成 MP4
except Exception:
    FFMPEG = None

DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads")
VERSION = "1.0.0"


# ---------- 小工具 ----------

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


def collect_formats(info):
    """把 yt-dlp 給的格式整理成「每個畫質一個選項」"""
    dur = info.get("duration") or 0
    best = {}
    for f in info.get("formats", []):
        h = f.get("height")
        if not h or f.get("vcodec") == "none":
            continue
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


# ---------- 主視窗 ----------

class App:
    def __init__(self, root):
        self.root = root
        self.q = queue.Queue()
        self.formats = []
        self.title = ""
        self.mbps = 0
        self.busy = False

        root.title(f"Video Helper 電腦版 v{VERSION}")
        root.geometry("640x460")
        root.minsize(560, 400)

        pad = {"padx": 10, "pady": 6}

        # 網址列
        top = ttk.Frame(root)
        top.pack(fill="x", **pad)
        ttk.Label(top, text="影片網址：").pack(side="left")
        self.url_var = tk.StringVar()
        entry = ttk.Entry(top, textvariable=self.url_var)
        entry.pack(side="left", fill="x", expand=True, padx=6)
        entry.bind("<Return>", lambda e: self.analyze())
        self.btn_read = ttk.Button(top, text="讀取", command=self.analyze)
        self.btn_read.pack(side="left")

        self.title_lbl = ttk.Label(root, text="貼上網址後按「讀取」", wraplength=600)
        self.title_lbl.pack(fill="x", **pad)

        # 畫質清單
        cols = ("quality", "size", "time", "type")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=8, selectmode="browse")
        for c, name, w in zip(cols, ("畫質", "檔案大小", "預估下載時間", "類型"), (100, 140, 160, 120)):
            self.tree.heading(c, text=name)
            self.tree.column(c, width=w, anchor="center")
        self.tree.pack(fill="both", expand=True, **pad)
        self.tree.bind("<Double-1>", lambda e: self.download())

        # 網速 + 按鈕
        mid = ttk.Frame(root)
        mid.pack(fill="x", **pad)
        self.speed_lbl = ttk.Label(mid, text="網速：尚未量測")
        self.speed_lbl.pack(side="left")
        ttk.Button(mid, text="打開下載資料夾", command=self.open_folder).pack(side="right")
        self.btn_dl = ttk.Button(mid, text="⬇ 下載選取的畫質", command=self.download, state="disabled")
        self.btn_dl.pack(side="right", padx=6)

        # 進度
        self.prog = ttk.Progressbar(root, maximum=100)
        self.prog.pack(fill="x", **pad)
        self.status = ttk.Label(root, text="" if FFMPEG else "⚠ 找不到 ffmpeg，部分影片可能無法轉成 MP4")
        self.status.pack(fill="x", **pad)

        self.poll()

    # 讓背景工作把結果丟回主畫面
    def poll(self):
        try:
            while True:
                kind, data = self.q.get_nowait()
                getattr(self, "on_" + kind)(data)
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    # ----- 讀取影片資訊 -----

    def analyze(self):
        url = self.url_var.get().strip()
        if not url or self.busy:
            return
        self.busy = True
        self.btn_read.config(state="disabled")
        self.btn_dl.config(state="disabled")
        self.tree.delete(*self.tree.get_children())
        self.title_lbl.config(text="讀取中…")
        self.status.config(text="")
        threading.Thread(target=self._analyze_worker, args=(url,), daemon=True).start()

    def _analyze_worker(self, url):
        try:
            with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True, "noplaylist": True}) as ydl:
                info = ydl.extract_info(url, download=False)
            fmts = collect_formats(info)
            mbps = 0
            for f in fmts:
                if f["direct"] and f["url"]:
                    mbps = speed_test(f["url"], f["headers"])
                    if mbps:
                        break
            self.q.put(("analyzed", (info.get("title") or "video", fmts, mbps)))
        except Exception as e:
            self.q.put(("error", f"讀取失敗：{e}"))

    def on_analyzed(self, data):
        self.title, self.formats, mbps = data
        if mbps:
            self.mbps = mbps
        self.busy = False
        self.btn_read.config(state="normal")
        self.title_lbl.config(text=self.title)
        self.speed_lbl.config(text=f"網速：{self.mbps:.1f} Mbps" if self.mbps else "網速：無法量測（下載時會顯示實際速度）")

        if not self.formats:
            self.status.config(text="找不到可下載的畫質")
            return
        for i, f in enumerate(self.formats):
            size = ("" if f["exact"] or not f["size"] else "約 ") + fmt_size(f["size"])
            t = fmt_time(f["size"] * 8 / (self.mbps * 1e6)) if f["size"] and self.mbps else "時間未知"
            kind = "MP4 直連" if f["direct"] else "切片"
            self.tree.insert("", "end", iid=str(i), values=(f"{f['height']}p", size, t, kind))
        self.tree.selection_set("0")
        self.btn_dl.config(state="normal")

    # ----- 下載 -----

    def download(self):
        sel = self.tree.selection()
        if not sel or self.busy:
            return
        f = self.formats[int(sel[0])]
        vals = self.tree.item(sel[0], "values")
        if not messagebox.askyesno("確定要下載嗎？",
                                   f"{self.title}\n\n畫質：{vals[0]}\n大小：{vals[1]}\n時間：{vals[2]}\n\n儲存到：{DOWNLOAD_DIR}"):
            return
        self.busy = True
        self.btn_dl.config(state="disabled")
        self.btn_read.config(state="disabled")
        self.prog["value"] = 0
        self.status.config(text="準備下載…")
        threading.Thread(target=self._download_worker, args=(f,), daemon=True).start()

    def _download_worker(self, f):
        def hook(d):
            if d["status"] == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                self.q.put(("progress", (d.get("downloaded_bytes", 0), total, d.get("speed"), d.get("eta"))))
            elif d["status"] == "finished":
                self.q.put(("progress_text", "下載完成，處理中（轉成 MP4）…"))

        fmt = f["id"] if f["audio"] else f"{f['id']}+bestaudio/{f['id']}"
        opts = {
            "format": fmt,
            "outtmpl": os.path.join(DOWNLOAD_DIR, "%(title).100B [%(height)sp].%(ext)s"),
            "noplaylist": True,
            "quiet": True,
            "no_warnings": True,
            "noprogress": True,
            "progress_hooks": [hook],
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
                ydl.download([self.url_var.get().strip()])
            self.q.put(("done", None))
        except Exception as e:
            self.q.put(("error", f"下載失敗：{e}"))

    def on_progress(self, data):
        done, total, speed, eta = data
        if total:
            self.prog["value"] = done / total * 100
        parts = [f"{fmt_size(done)} / {fmt_size(total)}" if total else fmt_size(done)]
        if speed:
            parts.append(f"{speed * 8 / 1e6:.1f} Mbps")
            self.mbps = speed * 8 / 1e6
        if eta is not None:
            parts.append(f"剩約 {fmt_time(eta)}")
        self.status.config(text="下載中：" + "｜".join(parts))

    def on_progress_text(self, text):
        self.status.config(text=text)

    def on_done(self, _):
        self.busy = False
        self.prog["value"] = 100
        self.btn_dl.config(state="normal")
        self.btn_read.config(state="normal")
        self.status.config(text="✅ 下載完成，檔案在「下載」資料夾")

    def on_error(self, msg):
        self.busy = False
        self.btn_dl.config(state="normal" if self.formats else "disabled")
        self.btn_read.config(state="normal")
        self.status.config(text="❌ " + msg[:200])
        if self.title_lbl.cget("text") == "讀取中…":
            self.title_lbl.config(text="貼上網址後按「讀取」")

    def open_folder(self):
        if sys.platform.startswith("win"):
            os.startfile(DOWNLOAD_DIR)
        elif sys.platform == "darwin":
            subprocess.run(["open", DOWNLOAD_DIR])
        else:
            subprocess.run(["xdg-open", DOWNLOAD_DIR])


if __name__ == "__main__":
    root = tk.Tk()
    App(root)
    root.mainloop()

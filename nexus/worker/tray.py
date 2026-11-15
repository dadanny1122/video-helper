"""
`VideoHelper.exe --worker`：Worker 節點模式。
  ・不建立下載器的 App、不碰 settings.json／history.json／Bridge／AI 金鑰（決策 3：最小權限、獨立生命週期）
  ・自己的設定（nexus\\worker\\config.json）、自己的日誌（nexus\\worker\\worker.log，有遮罩）
  ・一個小視窗：Master 位址、電腦名稱、配對碼 → 配對；顯示狀態、我方指紋、Master 指紋；可縮到系統匣
  ・`--headless`：沒有視窗（CI／測試用），只跑 Agent
"""
import logging
import logging.handlers
import os
import sys
import threading

from nexus import NEXUS_VERSION
from nexus.core.config import Config, mode_dir, set_worker_profile, worker_profile
from nexus.core.masking import mask
from nexus.worker.agent import WorkerAgent
from nexus.worker.credstore import CredStore

log = logging.getLogger("nexus.worker")


class _MaskFilter(logging.Filter):
    def filter(self, record):
        record.msg = mask(record.getMessage())
        record.args = ()
        return True


def setup_worker_logging(level=logging.INFO):
    path = os.path.join(mode_dir("worker"), "worker.log")
    h = logging.handlers.RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    h.addFilter(_MaskFilter())
    root = logging.getLogger("nexus")
    root.setLevel(level)
    root.addHandler(h)
    return path


def run_headless(master_url: str | None, pair_code: str | None, name: str | None, once: bool = False) -> int:
    """沒有視窗：有給位址＋配對碼就先配對，然後跑到被 Ctrl+C／once 為止"""
    cfg = Config("worker")
    agent = WorkerAgent(cfg, CredStore())
    if master_url and pair_code:
        agent.pair(master_url, pair_code, name or cfg["device_name"] or _hostname())
    agent.start()
    try:
        if once:
            import time
            time.sleep(1.5)
        else:
            threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        agent.stop()
    return 0


def _hostname():
    import socket
    return socket.gethostname()[:32]


def friendly_error(e: Exception, url: str) -> str:
    """把 Windows 的連線錯誤翻成「該檢查什麼」"""
    import socket
    import ssl
    from urllib.parse import urlsplit

    from nexus.core.errors import NexusError
    if isinstance(e, NexusError):
        return e.message
    host = urlsplit(url).hostname or url
    if isinstance(e, ValueError) or not url.startswith("https://"):
        return "Master 位址格式不對，要像 https://192.168.1.10:47331（同一台測試用 https://127.0.0.1:47331）"
    if isinstance(e, ConnectionRefusedError) or getattr(e, "winerror", None) == 10061:
        return (f"{host} 有回應，但沒有 Master 在聽。\n請到主程式「⚙ 進階設定 → 🖧 節點」勾「啟用 Master」，"
                "並確認埠號跟這裡一樣。")
    if isinstance(e, (TimeoutError, socket.timeout)) or getattr(e, "winerror", None) in (10060, 10065, 10051):
        return (f"等了很久 {host} 都沒有回應。\n請檢查：\n・位址是不是 Master 畫面上顯示的那個（同一台測試用 127.0.0.1）\n"
                "・兩台是不是在同一個網路\n・Master 那台的 Windows 防火牆有沒有允許 VideoHelper.exe（私人網路）")
    if isinstance(e, socket.gaierror):
        return f"找不到 {host} 這台電腦，請改用 Master 畫面上顯示的 IP 位址。"
    if isinstance(e, ssl.SSLError):
        return f"連上了但加密握手失敗（{e}）。對方可能不是 Video Helper 的 Master。"
    return f"連不上 Master：{e}"


def run_window() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg = Config("worker")
    store = CredStore()
    root = tk.Tk()
    root.title(f"Video Helper 節點（Worker）v{NEXUS_VERSION}" + (f"　[{worker_profile()}]" if worker_profile() else ""))
    k = root.winfo_fpixels("1i") / 96          # 高 DPI（150% 等）：視窗跟字一起放大
    W = lambda n: int(n * k)                   # noqa: E731
    root.geometry(f"{W(600)}x{W(470)}")
    root.minsize(W(560), W(430))
    try:
        import sv_ttk
        sv_ttk.set_theme("dark", root)
    except Exception:
        pass

    f = ttk.Frame(root, padding=14)
    f.pack(fill="both", expand=True)
    f.columnconfigure(1, weight=1)
    ttk.Label(f, text="這台電腦要當別台 Video Helper 的節點。第一版只會「連線、心跳、被管理」，不會接任務。",
              wraplength=W(560), justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
    ttk.Label(f, text="Master 位址").grid(row=1, column=0, sticky="w")
    url = ttk.Entry(f, width=34)
    if cfg["master_url"]:
        url.insert(0, cfg["master_url"])
    default_name = _hostname() + (f"-{worker_profile()}" if worker_profile() else "")
    url.grid(row=1, column=1, columnspan=2, sticky="we", pady=2)
    ttk.Label(f, foreground="#999", text="例：https://192.168.1.10:47331（照 Master 畫面上的；同一台測試用 https://127.0.0.1:47331）",
              wraplength=W(550), justify="left").grid(row=2, column=0, columnspan=3, sticky="w")
    ttk.Label(f, text="這台電腦的名稱").grid(row=3, column=0, sticky="w")
    name = ttk.Entry(f, width=34)
    name.insert(0, cfg["device_name"] or default_name)
    name.grid(row=3, column=1, columnspan=2, sticky="we", pady=2)
    ttk.Label(f, text="配對碼（Master 畫面上的 6 位數）").grid(row=4, column=0, sticky="w")
    code = ttk.Entry(f, width=12)
    code.grid(row=4, column=1, sticky="w", pady=2)
    status = ttk.Label(f, text="", wraplength=W(560), justify="left")
    status.grid(row=6, column=0, columnspan=3, sticky="w", pady=(10, 4))
    fps = ttk.Label(f, text="", foreground="#8ea3d6", wraplength=W(560), justify="left")
    fps.grid(row=7, column=0, columnspan=3, sticky="w")
    hint = ttk.Label(f, foreground="#999", wraplength=W(560), justify="left",
                     text="核准前請跟 Master 那邊核對「Master 指紋」前 8 碼是否一致，一樣才按核准。")
    hint.grid(row=8, column=0, columnspan=3, sticky="w", pady=(6, 0))

    agent = WorkerAgent(cfg, store)

    def refresh(*_):
        s = agent.status()
        names = {"UNCONFIGURED": "尚未配對", "REGISTERING": "送出註冊中…", "PENDING": "等待 Master 核准…",
                 "ACTIVE": "已連線（心跳正常）", "RECONNECTING": "重新連線中…", "REVOKED": "已被撤銷，請重新配對",
                 "EXPIRED": "憑證過期，請重新配對", "MASTER_CHANGED": "⚠ Master 的憑證變了，已停止（不會自動信任）",
                 "INCOMPATIBLE": "版本不相容，請更新程式", "STOPPED": "已停止"}
        status.config(text=f"狀態：{names.get(s['state'], s['state'])}　{s['detail'] or ''}")
        mfp = s["master_ca_fingerprint"] or s["master_server_fingerprint"] or "—"
        exp = f"\n憑證到期：{s['credential_expires_at'][:10]}" if s["credential_expires_at"] else ""
        fps.config(text=f"我的指紋：{s['my_fingerprint']}　Master 指紋：{mfp}{exp}")

    def do_pair():
        try:
            agent.pair(url.get().strip(), code.get().strip(), name.get().strip() or default_name)
            code.delete(0, "end")
        except Exception as e:
            messagebox.showerror("配對失敗", friendly_error(e, url.get().strip()))
        refresh()

    btns = ttk.Frame(f)
    btns.grid(row=5, column=0, columnspan=3, sticky="w", pady=(8, 0))
    ttk.Button(btns, text="配對", command=do_pair).pack(side="left")
    ttk.Button(btns, text="結束節點", command=lambda: (agent.stop(), root.destroy())).pack(side="left", padx=8)

    agent.on_state = lambda *_: root.after(0, refresh)
    agent.start()
    refresh()

    def tick():
        refresh()
        root.after(2000, tick)
    tick()
    root.protocol("WM_DELETE_WINDOW", lambda: (agent.stop(), root.destroy()))
    root.mainloop()
    return 0


class ProfileBusy(RuntimeError):
    pass


def lock_profile():
    """同一個 profile 只能開一個（兩個程序共用同一個身分會互相蓋掉狀態）。回傳要一直留著的檔案物件"""
    path = os.path.join(mode_dir("worker"), "worker.lock")
    fh = open(path, "a+")
    try:
        if sys.platform.startswith("win"):
            import msvcrt
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as e:
        fh.close()
        raise ProfileBusy("這個節點（profile）已經開著了") from e
    return fh


def _arg(argv, flag):
    return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    profile = _arg(argv, "--profile")
    try:
        set_worker_profile(profile)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    try:
        _lock = lock_profile()  # noqa: F841  留著＝鎖住
    except ProfileBusy as e:
        if "--headless" in argv:
            print(e, file=sys.stderr)
        else:
            import tkinter as tk
            from tkinter import messagebox
            r = tk.Tk()
            r.withdraw()
            messagebox.showinfo("Video Helper 節點", f"{e}。\n要再開一個節點，請用不同的 --profile 名稱。")
            r.destroy()
        return 3
    setup_worker_logging()
    log.info("Worker 模式啟動 v%s profile=%s", NEXUS_VERSION, profile or "（預設）")
    if "--headless" in argv:
        return run_headless(_arg(argv, "--master"), _arg(argv, "--pair"), _arg(argv, "--name"), once="--once" in argv)
    return run_window()


if __name__ == "__main__":
    sys.exit(main())

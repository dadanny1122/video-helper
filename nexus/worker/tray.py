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
    if isinstance(e, ValueError) and "配對碼" in str(e):
        return str(e)
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


def _i18n():
    """英文版（v1.45.0）：跟主程式同一份字典；打包少了就一律中文"""
    try:
        from webui import i18n
        return i18n
    except Exception:
        return None


def _t(s: str, **kw) -> str:
    i = _i18n()
    out = i.t(s) if i else s
    return out.format(**kw) if kw else out


def run_window() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg = Config("worker")
    I18N = _i18n()
    if I18N:                                    # 節點自己的設定（不碰主程式的 settings.json）：auto＝跟 Windows
        I18N.set_lang(cfg["lang"])
    store = CredStore()
    root = tk.Tk()
    root.title(f"Video Helper 節點（Worker）v{NEXUS_VERSION}" + (f"　[{worker_profile()}]" if worker_profile() else ""))
    k = root.winfo_fpixels("1i") / 96          # 高 DPI（150% 等）：視窗跟字一起放大
    W = lambda n: int(n * k)                   # noqa: E731
    root.geometry(f"{W(620)}x{W(600)}")
    root.minsize(W(580), W(560))
    try:
        import sv_ttk
        sv_ttk.set_theme("dark", root)
    except Exception:
        pass

    f = ttk.Frame(root, padding=14)
    f.pack(fill="both", expand=True)
    f.columnconfigure(1, weight=1)
    ttk.Label(f, text="這台電腦要當別台 Video Helper 的節點：跟 Master 連線、心跳、被管理；勾下面那格才會接 Master 派來的任務。",
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
    # V2：任務
    accept = tk.BooleanVar(value=bool(cfg["accept_tasks"]))

    def on_accept():
        cfg["accept_tasks"] = bool(accept.get())
        cfg.save()
    ttk.Checkbutton(f, text="接收 Master 派來的任務（下載影片、測試任務）", variable=accept,
                    command=on_accept).grid(row=9, column=0, columnspan=3, sticky="w", pady=(10, 0))
    tasks_lbl = ttk.Label(f, text="", wraplength=W(560), justify="left")
    tasks_lbl.grid(row=10, column=0, columnspan=3, sticky="w", pady=(4, 0))
    # V3：下載
    from nexus.worker.executor import default_download_dir, sandbox_status
    ttk.Label(f, text="下載資料夾").grid(row=11, column=0, sticky="w", pady=(10, 0))
    dl = ttk.Entry(f, width=34)
    dl.insert(0, cfg["download_dir"] or default_download_dir(worker_profile()))
    dl.grid(row=11, column=1, sticky="we", pady=(10, 0))

    def save_dl(*_):
        v = dl.get().strip()
        cfg["download_dir"] = "" if v == default_download_dir(worker_profile()) else v
        cfg.save()

    def pick_dl():
        from tkinter import filedialog
        d = filedialog.askdirectory(parent=root, initialdir=dl.get() or os.path.expanduser("~"))
        if d:
            dl.delete(0, "end")
            dl.insert(0, os.path.normpath(d))
            save_dl()
    dl.bind("<FocusOut>", save_dl)
    ttk.Button(f, text="選…", command=pick_dl).grid(row=11, column=2, sticky="w", padx=(6, 0), pady=(10, 0))
    ttk.Label(f, text="同時下載").grid(row=12, column=0, sticky="w", pady=2)
    conc = ttk.Combobox(f, state="readonly", width=8, values=[_t("1 部"), _t("2 部")])
    conc.current(1 if int(cfg["max_concurrency"] or 1) >= 2 else 0)

    def on_conc(*_):
        cfg["max_concurrency"] = 2 if conc.get().startswith("2") else 1
        cfg.save()
    conc.bind("<<ComboboxSelected>>", on_conc)
    conc.grid(row=12, column=1, sticky="w", pady=2)
    # V4：傳回 Master 後這份要不要留（D3：預設刪，驗證成功才刪）
    keep = tk.BooleanVar(value=bool(cfg.get("keep_after_return", False)))

    def on_keep():
        cfg["keep_after_return"] = bool(keep.get())
        cfg.save()
    ttk.Checkbutton(f, text="傳回 Master 之後，這台也留一份（不勾＝Master 確認收到才刪）", variable=keep,
                    command=on_keep).grid(row=12, column=2, sticky="w", padx=(10, 0), pady=2)
    # APEX（借 Dask）：自訂資源——這台有什麼特別的東西（GPU、裝了 PySAM…），有要求的任務只會派給有的節點
    ttk.Label(f, text="自訂資源").grid(row=14, column=0, sticky="w", pady=(8, 2))
    named = ttk.Entry(f, width=30)
    from nexus.tasks.capabilities import named_text, parse_named
    named.insert(0, ", ".join(f"{k}={v}" for k, v in sorted((cfg.get("named_resources") or {}).items())))
    named_msg = ttk.Label(f, text="例：gpu=1, pysam=1（沒有就空白）", foreground="#8ea3d6")

    def save_named(*_):
        try:
            cfg["named_resources"] = parse_named(named.get())
        except ValueError as e:
            named_msg.config(text=f"沒存：{e}", foreground="#e0a040")
            return
        cfg.save()
        named_msg.config(text=("已存：" + named_text(cfg["named_resources"])) if cfg["named_resources"] else "已存：沒有",
                         foreground="#8ea3d6")
    named.bind("<FocusOut>", save_named)
    named.bind("<Return>", save_named)
    named.grid(row=14, column=1, sticky="w", pady=(8, 2))
    named_msg.grid(row=14, column=2, sticky="w", padx=(10, 0), pady=(8, 2))
    sb = sandbox_status()
    iso_text = ("沙箱：基本（程序限制＋只准連支援的網站，不是完全隔離）" if sb.get("isolation") == "basic"
                else "沙箱：不能用（" + "；".join(sb.get("reasons") or ["原因不明"]) + "）→ 不會接下載任務")
    ttk.Label(f, text=iso_text, foreground="#8ea3d6" if sb.get("isolation") == "basic" else "#e0a040",
              wraplength=W(560), justify="left").grid(row=13, column=0, columnspan=3, sticky="w", pady=(4, 0))

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
        names = {"test.echo": "回聲", "test.sleep": "等待", "test.fail": "故意失敗", "test.file": "測試檔",
                 "video.download": "下載影片", "upload": "傳回 Master"}
        run = agent.executor.summary()

        def one(r):
            extra = {"disk_full": "（Master 空間不夠，等一下）", "retrying": "（連不上，重試中）"}.get(r.get("status", ""), "")
            return f"{_t(names.get(r['type'], r['type']))} {int(r['progress'] * 100)}%{_t(extra) if extra else ''}"
        sep = ", " if I18N and I18N.LANG == "en" else "、"
        tasks_lbl.config(text=_t("目前任務：{a}", a=sep.join(one(r) for r in run)) if run else _t("目前任務：沒有"))

    def do_pair():
        if agent.store.state["status"] in ("ACTIVE", "PENDING") and agent.store.state["device_id"] and not messagebox.askyesno(
                "重新配對？", "這個節點已經配對過了。\n\n重新配對會換一個全新身分，Master 名單上舊的那筆不會自己消失，"
                "配對完要到 Master 的「🖧 節點」把舊的撤銷。\n\n確定要重新配對嗎？"):
            return
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

    def toggle_lang():                           # 換語言：存在節點自己的設定，下次打開生效（不中斷連線）
        en = bool(I18N and I18N.LANG == "en")
        cfg["lang"] = "zh" if en else "en"
        cfg.save()
        messagebox.showinfo("Video Helper", "下次打開節點時換語言" if en else "The language changes the next time the node starts")
    if I18N:
        ttk.Button(btns, text="🌐 中文" if I18N.LANG == "en" else "🌐 English", command=toggle_lang).pack(side="left")

    if I18N:
        I18N.tk_translate(root)
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

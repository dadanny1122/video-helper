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
from nexus.core.config import Config, mode_dir
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


def run_window() -> int:
    import tkinter as tk
    from tkinter import messagebox, ttk

    cfg = Config("worker")
    store = CredStore()
    root = tk.Tk()
    root.title(f"Video Helper 節點（Worker）v{NEXUS_VERSION}")
    root.geometry("520x420")
    root.minsize(480, 380)
    try:
        import sv_ttk
        sv_ttk.set_theme("dark", root)
    except Exception:
        pass

    f = ttk.Frame(root, padding=14)
    f.pack(fill="both", expand=True)
    ttk.Label(f, text="這台電腦要當別台 Video Helper 的節點。第一版只會「連線、心跳、被管理」，不會接任務。",
              wraplength=480, justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
    ttk.Label(f, text="Master 位址").grid(row=1, column=0, sticky="w")
    url = ttk.Entry(f, width=34)
    url.insert(0, cfg["master_url"] or "https://192.168.1.10:47331")
    url.grid(row=1, column=1, columnspan=2, sticky="we", pady=2)
    ttk.Label(f, text="這台電腦的名稱").grid(row=2, column=0, sticky="w")
    name = ttk.Entry(f, width=34)
    name.insert(0, cfg["device_name"] or _hostname())
    name.grid(row=2, column=1, columnspan=2, sticky="we", pady=2)
    ttk.Label(f, text="配對碼（Master 畫面上的 6 位數）").grid(row=3, column=0, sticky="w")
    code = ttk.Entry(f, width=12)
    code.grid(row=3, column=1, sticky="w", pady=2)
    status = ttk.Label(f, text="", wraplength=480, justify="left")
    status.grid(row=5, column=0, columnspan=3, sticky="w", pady=(10, 4))
    fps = ttk.Label(f, text="", foreground="#8ea3d6", wraplength=480, justify="left")
    fps.grid(row=6, column=0, columnspan=3, sticky="w")
    hint = ttk.Label(f, foreground="#999", wraplength=480, justify="left",
                     text="核准前請跟 Master 那邊核對「Master 指紋」前 8 碼是否一致，一樣才按核准。")
    hint.grid(row=7, column=0, columnspan=3, sticky="w", pady=(6, 0))

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
            agent.pair(url.get().strip(), code.get().strip(), name.get().strip() or _hostname())
            code.delete(0, "end")
        except Exception as e:
            messagebox.showerror("配對失敗", str(e))
        refresh()

    btns = ttk.Frame(f)
    btns.grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 0))
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


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    setup_worker_logging()
    log.info("Worker 模式啟動 v%s", NEXUS_VERSION)
    if "--headless" in argv:
        def arg(flag):
            return argv[argv.index(flag) + 1] if flag in argv and argv.index(flag) + 1 < len(argv) else None
        return run_headless(arg("--master"), arg("--pair"), arg("--name"), once="--once" in argv)
    return run_window()


if __name__ == "__main__":
    sys.exit(main())

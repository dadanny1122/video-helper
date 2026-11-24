"""
「⚙ 進階設定 → 📋 節點任務」分頁（06-V2 §7）：任務只能在這裡建立（決策 D2：沒有網路端點）。
只呼叫 TaskService（不碰資料庫）。
    任務列表（進行中／全部）、新增任務（V3：下載影片；以及測試任務）、任務詳情（時間軸、結果、排程理由）、取消（要理由）、狀態不明時人工標記失敗
    V4：「下載完傳回這台」、傳回進度、傳回失敗時「重新傳回」
"""
import json
import tkinter as tk
import uuid
from tkinter import messagebox, simpledialog, ttk

from nexus.core.models import parse_utc
from nexus.tasks import adapters
from nexus.tasks.scheduler import REASONS, WEIGHTS

TYPE_NAME = {"test.echo": "回聲", "test.sleep": "等待", "test.fail": "故意失敗", "test.file": "測試檔（測傳回）",
             "video.download": "下載影片（節點）"}
FILE_TYPES = ("video.download", "test.file")
RETURN_ERRORS = ("TRANSFER_FAILED", "TRANSFER_EXPIRED")
STATE_NAME = {
    "CREATED": "已建立", "VALIDATING": "檢查中", "QUEUED": "排隊中", "SCHEDULED": "已選節點", "DISPATCHED": "已派出（等確認）",
    "ACCEPTED": "節點已接受", "PREPARING": "準備中", "RUNNING": "執行中", "UPLOADING": "傳回中", "VERIFYING": "驗證結果",
    "CANCELLING": "取消中…", "SUCCEEDED": "成功", "FAILED": "失敗", "REJECTED": "不合格（已拒絕）", "RESULT_REJECTED": "結果不通過",
    "CANCELLED": "已取消", "TIMED_OUT": "逾時", "UNKNOWN": "狀態不明（節點失聯）", "RECONCILING": "核對中"}
TAG = {"SUCCEEDED": "ok", "FAILED": "bad", "REJECTED": "bad", "RESULT_REJECTED": "bad", "TIMED_OUT": "bad", "UNKNOWN": "warn",
       "CANCELLED": "muted", "CANCELLING": "warn", "QUEUED": "muted"}
SCORE_NAME = {"cpu": "CPU", "memory": "記憶體", "disk": "磁碟", "queue": "空閒", "reliability": "可靠度"}


def local(ts):
    if not ts:
        return "—"
    try:
        return parse_utc(ts).astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:
        return ts


def summary_of(task: dict) -> str:
    try:
        p = json.loads(task["manifest_json"]).get("params", {})
    except Exception:
        return ""
    if "text" in p:
        t = str(p["text"])
        return t if len(t) <= 24 else t[:23] + "…"
    if "seconds" in p:
        return f"{p['seconds']} 秒"
    if "code" in p:
        return str(p["code"])
    if "size_mb" in p:
        return f"{p['size_mb']} MB" + ("，傳回" if p.get("return_to_master") else "")
    if "url" in p:                                    # 下載影片：只顯示網站和最後一段，不顯示完整網址
        try:
            import urllib.parse
            u = urllib.parse.urlsplit(str(p["url"]))
            tail = u.path.rstrip("/").rsplit("/", 1)[-1] or u.query
            q = p.get("quality", "best")
            back = "，傳回" if p.get("return_to_master") else ""
            return f"{(u.hostname or '').removeprefix('www.')}／{tail[:14]}（{'最高' if q == 'best' else q + 'p'}{back}）"
        except Exception:
            return "影片"
    return ""


def fmt_size(n) -> str:
    try:
        n = float(n)
    except (TypeError, ValueError):
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} GB"



def UI_BUSY():
    """電腦版會換成 video_helper_app.ui_busy（視窗正在被拖動／改大小）"""
    return False

class TasksTab:
    def __init__(self, page, service, colors, px=lambda n: n):
        self.page, self.svc, self.C, self.px = page, service, colors, px
        self._after = None
        self._rows = {}
        self._build()
        self.refresh()
        page.bind("<Map>", lambda e: self._start_timer(), add="+")
        page.bind("<Unmap>", lambda e: self._stop_timer(), add="+")

    # ---------- 畫面 ----------
    def _build(self):
        C, px, p = self.C, self.px, self.page
        ttk.Label(p, foreground=C["muted"], wraplength=px(720), justify="left",
                  text="派任務給已核准的節點：「下載影片」會在節點的沙箱裡下載；勾「下載完傳回這台」，"
                       "節點會把影片分段傳回來、比對 SHA-256 後放進這台的下載資料夾（斷線會接著傳）。"
                       "Pornhub 請從瀏覽器「🖧 交給節點」（網站會擋程式直接開網頁）。"
                       "另外有測試任務（回聲、等待、故意失敗、測試檔）用來確認派送、取消、重試、傳回。").pack(anchor="w")
        bar = ttk.Frame(p)
        bar.pack(fill="x", pady=(8, 4))
        self.new_btn = ttk.Button(bar, text="＋ 新增任務", style="Accent.TButton", command=self.new_task)
        self.new_btn.pack(side="left")
        self.active_only = tk.BooleanVar(value=False)
        ttk.Checkbutton(bar, text="只看進行中", variable=self.active_only, command=self.refresh).pack(side="left", padx=12)
        self.status = ttk.Label(bar, text="", foreground=C["muted"])
        self.status.pack(side="left", padx=8)

        mid = ttk.Frame(p)
        mid.pack(fill="both", expand=True)
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(0, weight=1)
        cols = ("type", "what", "status", "node", "tries", "created")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=10, selectmode="browse")
        for c, t, w in (("type", "類型", 80), ("what", "內容", 150), ("status", "狀態", 150), ("node", "節點", 130),
                        ("tries", "嘗試", 50), ("created", "建立時間", 120)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=px(w), stretch=(c in ("what", "status")))
        self.tree.grid(row=0, column=0, sticky="nsew")
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=sb.set)
        for tag, color in (("ok", C["ok"]), ("bad", C["bad"]), ("warn", C["accent"]), ("muted", C["muted"])):
            self.tree.tag_configure(tag, foreground=color)
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        self.tree.bind("<Double-1>", lambda e: self.show_detail())

        btns = ttk.Frame(p)
        btns.pack(fill="x", pady=(6, 0))
        self.b = {}
        for key, text, cmd in (("detail", "🔍 詳情與排程理由", self.show_detail), ("cancel", "⏹ 取消", self.cancel),
                               ("fail", "✘ 標記失敗（狀態不明時）", self.mark_failed),
                               ("retry_return", "↩ 重新傳回", self.retry_return), ("refresh", "↻ 重新整理", self.refresh)):
            self.b[key] = ttk.Button(btns, text=text, command=cmd)
            self.b[key].pack(side="left", padx=(0, 6))

    # ---------- 資料 ----------
    def _ts(self):
        return self.svc.task_view()

    def _names(self):
        return {d["device_id"]: d["name"] for d in self.svc.devices()}

    def _selected(self):
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _update_buttons(self):
        t = self._selected()
        on = self.svc.running
        st = t["status"] if t else None
        self.b["detail"].state(["!disabled"] if t else ["disabled"])
        self.b["cancel"].state(["!disabled"] if on and st in ("CREATED", "QUEUED", "SCHEDULED", "DISPATCHED", "ACCEPTED",
                                                                    "PREPARING", "RUNNING", "UPLOADING") else ["disabled"])
        self.b["retry_return"].state(["!disabled"] if on and st == "FAILED" and t.get("error_code") in RETURN_ERRORS
                                     else ["disabled"])
        self.b["fail"].state(["!disabled"] if on and st == "UNKNOWN" else ["disabled"])
        self.new_btn.state(["!disabled"] if on else ["disabled"])

    def refresh(self):
        on = self.svc.running
        ts = self._ts()
        self.status.config(text="" if on else "Master 沒開：只能看以前的任務。要派任務請先到「🖧 節點」勾「啟用 Master」。")
        sel = self._selected()
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        if ts:
            names = self._names()
            for t in ts.list(active_only=self.active_only.get()):
                iid = self.tree.insert("", "end", tags=(TAG.get(t["status"], ""),), values=(
                    TYPE_NAME.get(t["type"], t["type"]), summary_of(t), self._state_text(t),
                    names.get(t["assigned_device_id"], "—") if t["assigned_device_id"] else "—",
                    f"{t['attempt_count']}/{t['max_attempts']}", local(t["created_at"])))
                self._rows[iid] = t
                if sel and sel["task_id"] == t["task_id"]:
                    self.tree.selection_set(iid)
        self._update_buttons()

    def _state_text(self, t: dict) -> str:
        st = STATE_NAME.get(t["status"], t["status"])
        if t["status"] == "UPLOADING":
            prog = self.svc.upload_progress(t["task_id"]) if hasattr(self.svc, "upload_progress") else None
            if prog and prog.get("disk_full"):
                return "傳回中（這台空間不夠，清出空間後自動繼續）"
            if prog:
                return f"傳回中 {int(prog['ratio'] * 100)}%（{fmt_size(prog['received'])}／{fmt_size(prog['size'])}）"
        if t["status"] == "FAILED" and t.get("error_code") in RETURN_ERRORS:
            return "傳回失敗（檔案留在節點）"
        return st

    # ---------- 操作 ----------
    def retry_return(self):
        t = self._selected()
        if not t:
            return
        try:
            self.svc.retry_return(t["task_id"])
            messagebox.showinfo("重新傳回", "好了。節點上線時會自動接著傳（已經收到的部分不用重傳）。", parent=self.page)
        except Exception as e:
            messagebox.showerror("重新傳回", getattr(e, "message", None) or str(e), parent=self.page)
        self.refresh()

    def new_task(self):
        if not self.svc.running:
            return
        NewTaskDialog(self.page, self, self.px)

    def submit(self, ttype, params, **opts):
        t = self.svc.tasks.create(ttype, params, actor="owner", **opts)
        self.refresh()
        return t

    def cancel(self):
        t = self._selected()
        if not t:
            return
        reason = simpledialog.askstring("取消任務", "為什麼取消？（會寫進稽核紀錄）", parent=self.page)
        if not reason or not reason.strip():
            return
        try:
            r = self.svc.tasks.cancel(t["task_id"], "owner", reason.strip())
            if r["status"] == "CANCELLING":
                messagebox.showinfo("取消中", "已通知節點停止；節點確認停下後才會變成「已取消」。", parent=self.page)
        except Exception as e:
            messagebox.showerror("取消失敗", str(e), parent=self.page)
        self.refresh()

    def mark_failed(self):
        t = self._selected()
        if not t or t["status"] != "UNKNOWN":
            return
        if not messagebox.askyesno("標記失敗", "這個任務的節點失聯了，不知道它有沒有跑完。\n確定要標記成失敗嗎？（不會自動重跑）",
                                   parent=self.page):
            return
        reason = simpledialog.askstring("標記失敗", "理由（例如：已確認節點關機）：", parent=self.page)
        if not reason or not reason.strip():
            return
        try:
            self.svc.tasks.mark_failed(t["task_id"], "owner", reason.strip())
        except Exception as e:
            messagebox.showerror("失敗", str(e), parent=self.page)
        self.refresh()

    def show_detail(self, task_id=None):
        t = self._selected() if task_id is None else {"task_id": task_id}
        ts = self._ts()
        if not t or not ts:
            return None
        return DetailWindow(self.page, ts.detail(t["task_id"]), self._names(), self.C, self.px)

    # ---------- 每 2 秒 ----------
    def _start_timer(self):
        self._stop_timer()
        self._tick()

    def _stop_timer(self):
        if self._after:
            try:
                self.page.after_cancel(self._after)
            except Exception:
                pass
            self._after = None

    def _tick(self):
        try:
            if self.page.winfo_viewable() and not UI_BUSY():     # 視窗拖動／改大小時先不重整（電腦版會接上）
                self.refresh()
        except tk.TclError:
            return
        self._after = self.page.after(2000, self._tick)


class NewTaskDialog:
    def __init__(self, parent, tab: TasksTab, px):
        self.tab = tab
        w = self.win = tk.Toplevel(parent)
        w.title("新增任務")
        w.transient(parent.winfo_toplevel())
        f = ttk.Frame(w, padding=14)
        f.pack(fill="both", expand=True)
        f.columnconfigure(1, weight=1)
        ttk.Label(f, text="類型").grid(row=0, column=0, sticky="w")
        # 下載影片放第一個（最常用），後面是測試任務
        order = sorted(adapters.ADAPTERS, key=lambda n: (not n.startswith("video."), n))
        self.types = {f"{TYPE_NAME[n]}（{n}）": n for n in order}
        self.type = ttk.Combobox(f, state="readonly", values=list(self.types), width=28)
        self.type.current(0)
        self.type.grid(row=0, column=1, sticky="we", pady=2)
        self.type.bind("<<ComboboxSelected>>", lambda e: self._switch())
        self.desc = ttk.Label(f, text="", foreground=tab.C["muted"], wraplength=px(380), justify="left")
        self.desc.grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 6))
        self.plabel = ttk.Label(f, text="")
        self.plabel.grid(row=2, column=0, sticky="w")
        self.pframe = ttk.Frame(f)
        self.pframe.grid(row=2, column=1, sticky="we", pady=2)
        ttk.Label(f, text="執行時間上限（秒）").grid(row=3, column=0, sticky="w")
        self.runtime = ttk.Spinbox(f, from_=1, to=7200, width=8)
        self.runtime.grid(row=3, column=1, sticky="w", pady=2)
        ttk.Label(f, text="最多嘗試幾次").grid(row=4, column=0, sticky="w")
        self.tries = ttk.Spinbox(f, from_=1, to=5, width=8)
        self.tries.set(2)
        self.tries.grid(row=4, column=1, sticky="w", pady=2)
        ttk.Label(f, text="重複送出檢查碼").grid(row=5, column=0, sticky="w")
        self.key = ttk.Entry(f)
        self.key.insert(0, str(uuid.uuid4())[:8])
        self.key.grid(row=5, column=1, sticky="we", pady=2)
        ttk.Label(f, foreground=tab.C["muted"], wraplength=px(380), justify="left",
                  text="同一個檢查碼再送一次，不會多建一個任務（會回到原本那個）；內容不同會被拒絕。").grid(
            row=6, column=0, columnspan=2, sticky="w")
        b = ttk.Frame(f)
        b.grid(row=7, column=0, columnspan=2, sticky="e", pady=(10, 0))
        ttk.Button(b, text="送出", style="Accent.TButton", command=self.ok).pack(side="left")
        ttk.Button(b, text="關閉", command=w.destroy).pack(side="left", padx=6)
        self._switch()

    def _switch(self):
        n = self.types[self.type.get()]
        spec = adapters.get(n)
        self.desc.config(text=spec.description)
        for c in self.pframe.winfo_children():
            c.destroy()
        self.quality = None
        self.ret = tk.BooleanVar(value=True)               # D2：預設傳回
        if n == "test.file":
            self.plabel.config(text="檔案大小（MB）")
            self.pv = ttk.Spinbox(self.pframe, from_=1, to=2048, width=8)
            self.pv.set(20)
            self.pv.pack(anchor="w")
            ttk.Checkbutton(self.pframe, text="寫完傳回這台", variable=self.ret).pack(anchor="w", pady=(4, 0))
            self.runtime.set(spec.default_runtime_seconds)
            return
        if n == "video.download":
            self.plabel.config(text="影片網址")
            self.pv = ttk.Entry(self.pframe, width=48)
            row = ttk.Frame(self.pframe)
            ttk.Label(row, text="畫質").pack(side="left")
            self.quality = ttk.Combobox(row, state="readonly", width=10,
                                        values=["最高", "1080p", "720p", "480p"])
            self.quality.current(0)
            self.quality.pack(side="left", padx=(6, 0))
            ttk.Label(row, text="　只支援能下載的 9 個網站",
                      foreground=self.tab.C["muted"]).pack(side="left")
            self.pv.pack(fill="x")
            row.pack(fill="x", pady=(4, 0))
            ttk.Checkbutton(self.pframe, text="下載完傳回這台（不勾＝檔案留在節點那台）", variable=self.ret).pack(anchor="w", pady=(4, 0))
            self.runtime.set(spec.default_runtime_seconds)
            return
        if n == "test.echo":
            self.plabel.config(text="文字")
            self.pv = ttk.Entry(self.pframe)
            self.pv.insert(0, "哈囉 Nexus")
        elif n == "test.sleep":
            self.plabel.config(text="等幾秒")
            self.pv = ttk.Spinbox(self.pframe, from_=1, to=600, width=8)
            self.pv.set(10)
        else:
            self.plabel.config(text="失敗原因")
            self.pv = ttk.Combobox(self.pframe, state="readonly", values=["ADAPTER_ERROR", "RESOURCE_LIMIT", "INVALID_INPUT"])
            self.pv.current(0)
        self.pv.pack(fill="x")
        self.runtime.set(spec.default_runtime_seconds if spec.default_runtime_seconds <= 900 else 900)

    def params(self):
        n = self.types[self.type.get()]
        v = self.pv.get()
        if n == "video.download":
            q = {"最高": "best", "1080p": "1080", "720p": "720", "480p": "480"}[self.quality.get()]
            return n, {"url": v.strip(), "quality": q, **({"return_to_master": True} if self.ret.get() else {})}
        if n == "test.file":
            return n, {"size_mb": int(v), **({"return_to_master": True} if self.ret.get() else {})}
        if n == "test.echo":
            return n, {"text": v}
        if n == "test.sleep":
            return n, {"seconds": int(v)}
        return n, {"code": v}

    def ok(self):
        try:
            n, p = self.params()
            if n == "video.download" and not p.get("media_url"):
                import urllib.parse

                from nexus.net import sites
                host = urllib.parse.urlsplit(p.get("url", "")).hostname or ""
                if sites.needs_browser(host) and not messagebox.askyesno(
                        "這個網站要從瀏覽器交", f"{sites.site_name(host)} 會擋程式直接開網頁，在這裡貼網址節點會下載失敗。\n\n"
                        "請改用瀏覽器：開影片頁 →「⬇ 下載」→ 畫質旁的「🖧 交給節點」。\n\n還是要照樣送出嗎？",
                        parent=self.win):
                    return
            t = self.tab.submit(n, p, idempotency_key=self.key.get().strip() or None, max_runtime_seconds=int(self.runtime.get()),
                                max_attempts=int(self.tries.get()))
        except ValueError:
            messagebox.showerror("新增任務", "數字格式不對", parent=self.win)
            return
        except Exception as e:
            messagebox.showerror("新增任務", getattr(e, "message", str(e)), parent=self.win)
            return
        if t["status"] == "REJECTED":
            messagebox.showwarning("任務不合格", t.get("error_message") or "內容不合格", parent=self.win)
        self.win.destroy()


class DetailWindow:
    def __init__(self, parent, d: dict, names: dict, C, px):
        t = d["task"]
        w = self.win = tk.Toplevel(parent)
        w.title(f"任務詳情　{TYPE_NAME.get(t['type'], t['type'])}　{t['task_id'][:8]}")
        w.geometry(f"{px(860)}x{px(600)}")
        f = ttk.Frame(w, padding=12)
        f.pack(fill="both", expand=True)
        res = ""
        node = names.get(t["assigned_device_id"], "節點")
        if t["result_json"] and t["type"] in FILE_TYPES:
            res = self._download_result(t["result_json"], node, t["status"])
            if t["error_message"]:
                res = f"錯誤：{t['error_code']}　{t['error_message'][:200]}\n" + res
        elif t["result_json"]:
            res = "結果：" + t["result_json"][:200]
        elif t["error_message"]:
            res = f"錯誤：{t['error_code']}　{t['error_message'][:200]}"
        ttk.Label(f, text=f"狀態：{STATE_NAME.get(t['status'], t['status'])}　節點：{names.get(t['assigned_device_id'], '—')}　"
                          f"嘗試：{t['attempt_count']}/{t['max_attempts']}　內容：{summary_of(t)}",
                  font=("", 10, "bold")).pack(anchor="w")
        if res:
            good = t["result_json"] and t["status"] in ("SUCCEEDED", "UPLOADING", "VERIFYING")
            ttk.Label(f, text=res, foreground=C["ok"] if good else C["bad"], wraplength=px(820)).pack(anchor="w", pady=(2, 0))
        nb = ttk.Notebook(f)
        nb.pack(fill="both", expand=True, pady=(8, 0))
        self.why = self._why_tab(nb, d, names, C, px)
        self.timeline = self._timeline_tab(nb, d, names, px)
        nb.add(self.why, text="  為什麼選這台  ")
        nb.add(self.timeline, text="  時間軸  ")

    @staticmethod
    def _download_result(raw: str, node: str, status: str = "SUCCEEDED") -> str:
        """下載結果：檔名、大小、存在哪台的哪裡（V4：傳回成功就寫這台的路徑；還沒傳完／失敗就寫留在節點）"""
        try:
            r = json.loads(raw)
        except Exception:
            return "結果：" + raw[:200]
        lines = [f"下載完成：{r.get('site', '')}　{str(r.get('title', ''))[:60]}".rstrip()]
        for f in r.get("files", [])[:5]:
            if r.get("returned"):
                lines.append(f"・{f.get('name')}（{fmt_size(f.get('size'))}）已傳回這台：{f.get('path', '')}")
            else:
                where = "還在傳回中，目前在" if status == "UPLOADING" else "存在"
                lines.append(f"・{f.get('name')}（{fmt_size(f.get('size'))}）{where}「{node}」：{f.get('path', '')}")
        return "\n".join(lines)

    @staticmethod
    def _why_tab(nb, d, names, C, px):
        page = ttk.Frame(nb, padding=8)
        decs = d["decisions"]
        if not decs:
            ttk.Label(page, text="還沒排程過。").pack(anchor="w")
            return page
        last = decs[-1]
        chosen = last["chosen_device_id"]
        w = WEIGHTS.get(last["weights_version"], {})
        ttk.Label(page, foreground=C["muted"], wraplength=px(800), justify="left",
                  text=f"最近一次排程：{local(last['decided_at'])}（權重版本 {last['weights_version']}：" +
                       "、".join(f"{SCORE_NAME[k]} {int(v * 100)}%" for k, v in w.items()) +
                       "）。先排除不合格的節點，再替合格的打 0～100 分，最高分的拿到；"
                       "同分時選目前任務較少的、再依節點 ID。").pack(anchor="w")
        cols = ("node", "result", "total", "detail")
        tv = ttk.Treeview(page, columns=cols, show="headings", height=8)
        for c, txt, wd in (("node", "節點", 140), ("result", "結果", 90), ("total", "分數", 60), ("detail", "子分數／排除原因", 480)):
            tv.heading(c, text=txt)
            tv.column(c, width=px(wd), stretch=(c == "detail"))
        tv.pack(fill="both", expand=True, pady=(6, 0))
        tv.tag_configure("chosen", foreground=C["ok"])
        tv.tag_configure("out", foreground=C["muted"])
        for cand in last["candidates"]:
            if cand["eligible"]:
                detail = "　".join(f"{SCORE_NAME[k]} {v:.2f}" for k, v in cand["scores"].items())
                result = "✔ 選中" if cand["device_id"] == chosen else "合格"
                total = f"{cand['total']:.1f}"
            else:
                detail = "；".join(REASONS.get(r, r) for r in cand["reasons"])
                result, total = "排除", "—"
            tv.insert("", "end", tags=("chosen" if cand["device_id"] == chosen else ("out" if not cand["eligible"] else ""),),
                      values=(names.get(cand["device_id"], cand["name"]), result, total, detail))
        if len(decs) > 1:
            ttk.Label(page, foreground=C["muted"],
                      text=f"（這個任務一共排程過 {len(decs)} 次，例如重試或節點拒收後重新分配）").pack(anchor="w")
        return page

    @staticmethod
    def _timeline_tab(nb, d, names, px):
        page = ttk.Frame(nb, padding=8)
        cols = ("ts", "who", "what", "note")
        tv = ttk.Treeview(page, columns=cols, show="headings")
        for c, txt, wd in (("ts", "時間", 130), ("who", "誰", 130), ("what", "發生什麼", 160), ("note", "說明", 380)):
            tv.heading(c, text=txt)
            tv.column(c, width=px(wd), stretch=(c == "note"))
        sb = ttk.Scrollbar(page, orient="vertical", command=tv.yview)
        tv.configure(yscrollcommand=sb.set)
        tv.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        rows = []
        for a in d["audit"]:
            st = a["action"].split(".", 1)[-1].upper()
            rows.append((a["ts"], _who(a["actor"], names), STATE_NAME.get(st, a["action"]), a["reason"] or ""))
        for e in d["events"]:
            if e["type"] == "PROGRESS":
                try:
                    pct = int(json.loads(e["data_json"]).get("progress", 0) * 100)
                except Exception:
                    pct = 0
                rows.append((e["received_at"], names.get(e["device_id"], "節點"), "進度", f"{pct}%"))
        for r in sorted(rows, key=lambda x: x[0]):
            tv.insert("", "end", values=(local(r[0]), *r[1:]))
        return page


def _who(actor: str, names: dict) -> str:
    if actor.startswith("worker:"):
        return "節點 " + names.get(actor[7:], actor[7:15])
    names = {"owner": "你", "browser": "瀏覽器（交給節點）", "scheduler": "排程器", "dispatcher": "派送", "master": "Master 驗證"}
    return names.get(actor, actor)

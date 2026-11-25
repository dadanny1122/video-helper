"""
「⚙ 進階設定 → 🖧 節點」分頁（Master 管理，只在本機 GUI，沒有任何網路管理端點）。
只呼叫 MasterService／DeviceRegistry，不碰資料庫。
    啟用開關、綁定位址（本機網卡下拉）、埠、CA 指紋、配對碼（倒數）、
    等待核准／已核准名單（核准、拒絕、暫停、恢復、撤銷都要確認＋理由）、稽核紀錄。
"""
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

from nexus.display import CONN_NAME, KIND_NAME
from nexus.display import DEVICE_STATE_NAME as STATE_NAME
from nexus.display import ago as _ago
from nexus.display import local_min as _local
from nexus.master.server import BindPolicy

__all__ = ["DevicesTab", "KIND_NAME", "STATE_NAME", "CONN_NAME", "_ago", "_local"]   # 舊名字照舊可用（S5 搬到 nexus/display.py）


def UI_BUSY():
    """電腦版會換成 video_helper_app.ui_busy（視窗正在被拖動／改大小）"""
    return False


class DevicesTab:
    def __init__(self, page, service, settings, colors, px=lambda n: n, log_path=""):
        self.page, self.svc, self.settings, self.C, self.px = page, service, settings, colors, px
        self.cfg = service.cfg
        self._after = None
        self._build()
        self.refresh()
        page.bind("<Map>", lambda e: self._start_timer(), add="+")
        page.bind("<Unmap>", lambda e: self._stop_timer(), add="+")

    # ---------- 畫面 ----------
    def _build(self):
        C, px = self.C, self.px
        p = self.page
        ttk.Label(p, foreground=C["muted"], wraplength=px(700), justify="left",
                  text="讓另一台電腦（跑 VideoHelper.exe --worker）安全地連到這台。核准後可以到「📋 節點任務」派測試任務給它，"
                       "還不會真的下載影片。兩台都要在同一個區域網路或 VPN；每台節點都要你在這裡按核准才算數。").pack(anchor="w")

        top = ttk.LabelFrame(p, text=" Master（這台電腦） ", padding=(10, 6))
        top.pack(fill="x", pady=(8, 4))
        top.columnconfigure(5, weight=1)
        self.enabled = tk.BooleanVar(value=bool(self.cfg["enabled"]))
        ttk.Checkbutton(top, text="啟用 Master（開一個 HTTPS 埠）", variable=self.enabled,
                        command=self.toggle).grid(row=0, column=0, columnspan=3, sticky="w")
        self.status_lbl = ttk.Label(top, text="", foreground=C["muted"])
        self.status_lbl.grid(row=0, column=3, columnspan=3, sticky="w", padx=8)

        ttk.Label(top, text="綁定位址").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.addr = ttk.Combobox(top, width=30, state="readonly")
        self.addr.grid(row=1, column=1, sticky="w", padx=6, pady=(6, 0))
        self.addr.bind("<<ComboboxSelected>>", lambda e: self._save_bind())
        ttk.Label(top, text="埠").grid(row=1, column=2, sticky="e", pady=(6, 0))
        self.port = ttk.Spinbox(top, from_=1024, to=65535, width=7, command=self._save_bind)
        self.port.set(int(self.cfg["port"]))
        self.port.bind("<FocusOut>", lambda e: self._save_bind())
        self.port.grid(row=1, column=3, sticky="w", padx=6, pady=(6, 0))
        ttk.Button(top, text="↻ 重抓網卡", command=self._fill_addrs).grid(row=1, column=4, sticky="w", pady=(6, 0))

        self.fp_lbl = ttk.Label(top, text="", foreground=C["muted"])
        self.fp_lbl.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))
        ttk.Label(top, foreground=C["muted"], wraplength=px(680), justify="left",
                  text="防火牆：第一次啟用 Windows 會問要不要允許 VideoHelper.exe，選「私人網路」允許即可。"
                       "節點那邊核准前，要核對這裡的 Master 指紋前 8 碼跟節點畫面上的一樣。").grid(row=3, column=0, columnspan=6,
                                                                                      sticky="w", pady=(4, 0))

        pair = ttk.Frame(top)
        pair.grid(row=4, column=0, columnspan=6, sticky="w", pady=(8, 0))
        self.pair_btn = ttk.Button(pair, text="產生配對碼", command=self.new_pair, style="Accent.TButton")
        self.pair_btn.pack(side="left")
        self.pair_lbl = ttk.Label(pair, text="", font=("Consolas", 16, "bold"))
        self.pair_lbl.pack(side="left", padx=12)
        self.pair_note = ttk.Label(pair, text="", foreground=C["muted"])
        self.pair_note.pack(side="left")

        # 名單
        mid = ttk.Frame(p)
        mid.pack(fill="both", expand=True, pady=(6, 0))
        mid.columnconfigure(0, weight=1)
        mid.rowconfigure(1, weight=1)
        ttk.Label(mid, text="節點名單（選一列再按下面的按鈕）", font=("", 10, "bold")).grid(row=0, column=0, sticky="w")
        cols = ("name", "status", "conn", "seen", "iso", "created", "ip", "fp")
        self.tree = ttk.Treeview(mid, columns=cols, show="headings", height=7, selectmode="browse")
        for c, t, w in (("name", "名稱", 130), ("status", "狀態", 80), ("conn", "連線", 80), ("seen", "最後心跳", 90),
                        ("iso", "沙箱", 110),
                        ("created", "配對時間", 110), ("ip", "來源 IP", 100), ("fp", "指紋（前 16）", 150)):
            self.tree.heading(c, text=t)
            self.tree.column(c, width=px(w), stretch=(c == "fp"))
        self.tree.grid(row=1, column=0, sticky="nsew")
        bar = ttk.Scrollbar(mid, orient="vertical", command=self.tree.yview)
        bar.grid(row=1, column=1, sticky="ns")
        self.tree.configure(yscrollcommand=bar.set)
        self.tree.tag_configure("PENDING", foreground=C["accent"])
        self.tree.tag_configure("ACTIVE", foreground=C["ok"])
        self.tree.tag_configure("REVOKED", foreground=C["bad"])
        self.tree.tag_configure("SUSPENDED", foreground=C["muted"])
        self.tree.tag_configure("REJECTED", foreground=C["muted"])
        self.tree.tag_configure("EXPIRED", foreground=C["bad"])

        btns = ttk.Frame(mid)
        btns.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.b = {}
        for key, text, cmd in (("approve", "✔ 核准", lambda: self.act("approve")), ("reject", "✘ 拒絕", lambda: self.act("reject")),
                               ("suspend", "⏸ 暫停", lambda: self.act("suspend")), ("resume", "▶ 恢復", lambda: self.act("resume")),
                               ("revoke", "⛔ 撤銷", lambda: self.act("revoke")), ("refresh", "↻ 重新整理", self.refresh),
                               ("audit", "📜 稽核紀錄", self.show_audit)):
            self.b[key] = ttk.Button(btns, text=text, command=cmd)
            self.b[key].pack(side="left", padx=(0, 6))
        self.tree.bind("<<TreeviewSelect>>", lambda e: self._update_buttons())
        if self.svc.error:
            self.status_lbl.config(text=f"啟動失敗：{self.svc.error}", foreground=C["bad"])
        self._fill_addrs()

    # ---------- 設定 ----------
    def _fill_addrs(self):
        addrs = BindPolicy.local_addresses()
        cur = str(self.cfg["bind_host"])
        if cur not in addrs and BindPolicy.classify(cur) != "invalid":
            addrs.append(cur)
        self._addr_map = {f"{a}　（{KIND_NAME.get(BindPolicy.classify(a), BindPolicy.classify(a))}）": a for a in addrs}
        self.addr["values"] = list(self._addr_map)
        for label, a in self._addr_map.items():
            if a == cur:
                self.addr.set(label)

    def _save_bind(self):
        host = self._addr_map.get(self.addr.get(), str(self.cfg["bind_host"]))
        try:
            port = int(self.port.get())
        except ValueError:
            port = int(self.cfg["port"])
        if (host, port) != (str(self.cfg["bind_host"]), int(self.cfg["port"])):
            self.cfg["bind_host"], self.cfg["port"] = host, port
            self.cfg.save()
            if self.svc.running:
                self.refresh()

    def toggle(self):
        want = self.enabled.get()
        if want:
            self._save_bind()
            if BindPolicy.classify(str(self.cfg["bind_host"])) == "lan" and not messagebox.askyesno(
                    "開放區域網路", f"Master 會在 {self.cfg['bind_host']}:{self.cfg['port']} 聽同一個網路裡的電腦。\n"
                    "只有你在這裡按「核准」的節點才能真的連上；其他人只會看到被拒絕。\n\n要啟用嗎？", parent=self.page):
                self.enabled.set(False)
                return
            try:
                self.svc.start()
            except Exception as e:
                self.enabled.set(False)
                self.status_lbl.config(text=f"啟動失敗：{e}", foreground=self.C["bad"])
                messagebox.showerror("Master 啟動失敗", str(e), parent=self.page)
                return
        else:
            self.svc.stop()
        self.cfg["enabled"] = bool(want)
        self.cfg.save()
        self.refresh()

    def new_pair(self):
        if not self.svc.running:
            messagebox.showinfo("先啟用 Master", "要先勾「啟用 Master」，節點才連得進來。", parent=self.page)
            return
        code = self.svc.registry.pair.new()
        self.pair_lbl.config(text=f"{code[:3]} {code[3:]}")
        self.refresh()

    def _isolation(self, d) -> str:
        """「沙箱」欄：基本（程序限制）／不能用＋原因／舊版沒回報"""
        cap = self.svc.capabilities(d["device_id"]) if hasattr(self.svc, "capabilities") else None
        res = (cap or {}).get("resources") or {}
        if "isolation" not in res:
            return "—（舊版節點）" if cap else "—"
        if res["isolation"] == "basic":
            return "基本（可下載）"
        return "不能用：" + (res.get("isolation_reason") or "原因不明")[:40]

    # ---------- 名單與操作 ----------
    def _selected(self):
        sel = self.tree.selection()
        return self._rows.get(sel[0]) if sel else None

    def _update_buttons(self):
        d = self._selected()
        st = d["status"] if d else None
        on = self.svc.running
        for key, ok in (("approve", st == "PENDING"), ("reject", st == "PENDING"), ("suspend", st == "ACTIVE"),
                        ("resume", st == "SUSPENDED"), ("revoke", st in ("ACTIVE", "SUSPENDED"))):
            self.b[key].state(["!disabled"] if ok and on else ["disabled"])

    def act(self, what):
        d = self._selected()
        if not d or not self.svc.running:
            return
        titles = {"approve": ("核准節點", "核准後這台節點會拿到憑證、可以跟 Master 心跳。\n請先核對節點畫面上的 Master 指紋。"),
                  "reject": ("拒絕節點", "拒絕後它要重新配對才能再申請。"),
                  "suspend": ("暫停節點", "暫停會立刻切斷它的連線，恢復前心跳都會被拒絕。"),
                  "resume": ("恢復節點", "恢復後它會自己接回來。"),
                  "revoke": ("撤銷節點", "⚠ 撤銷不能復原：連線立刻切斷、憑證永久作廢，就算它拿舊憑證也進不來；\n"
                                       "要再用得重新配對（新身分）。")}
        title, note = titles[what]
        if what == "approve":
            same = [o for o in self._rows.values() if o["device_id"] != d["device_id"] and o["name"] == d["name"]
                    and o["status"] in ("ACTIVE", "SUSPENDED", "PENDING")]
            if same:
                note += (f"\n\n⚠ 名單裡已經有 {len(same)} 筆也叫「{d['name']}」、還沒撤銷的節點"
                         "（通常是同一台重新配對留下的舊身分）。核准這筆之後，記得把舊的撤銷。")
        fp = d["public_key_fingerprint"][:16]
        if not messagebox.askyesno(title, f"{d['name']}\n指紋 {fp}\n來源 {d['source_ip_first'] or '—'}\n\n{note}\n\n確定要{title[:2]}嗎？",
                                   icon="warning" if what == "revoke" else "question", parent=self.page):
            return
        reason = simpledialog.askstring(title, "理由（會寫進稽核紀錄）：", parent=self.page)
        if not reason or not reason.strip():
            messagebox.showinfo(title, "沒有理由就不執行。", parent=self.page)
            return
        try:
            getattr(self.svc.registry, what)(d["device_id"], "owner", reason.strip())
        except Exception as e:
            messagebox.showerror(title, str(e), parent=self.page)
        self.refresh()

    def refresh(self):
        C = self.C
        on = self.svc.running
        if on:
            want = (str(self.cfg["bind_host"]), int(self.cfg["port"]))
            if (str(self.svc.host), int(self.svc.port)) != want:
                # 位址／埠改了但 Master 還在舊位址跑：每次重新整理都要提醒，不然會被「執行中」蓋掉
                self.status_lbl.config(
                    text=f"● 執行中 https://{self.svc.host}:{self.svc.port}　⚠ 設定已改成 {want[0]}:{want[1]}，"
                         "取消再勾「啟用 Master」才會換", foreground=C["bad"])
            else:
                self.status_lbl.config(text=f"● 執行中 https://{self.svc.host}:{self.svc.port}", foreground=C["ok"])
        elif not self.svc.error:
            self.status_lbl.config(text="○ 未啟用", foreground=C["muted"])
        self.enabled.set(on)
        try:
            self.fp_lbl.config(text=f"Master 指紋：{self.svc.ca_fingerprint()[:16]}…（節點核准前要核對前 8 碼）")
        except Exception as e:
            self.fp_lbl.config(text=f"Master 指紋：尚未建立（{type(e).__name__}）")
        # 配對碼
        rem = self.svc.registry.pair.remaining() if on and self.svc.registry else 0
        if rem:
            self.pair_note.config(text=f"{rem // 60}:{rem % 60:02d} 後失效，只能用一次")
        else:
            self.pair_lbl.config(text="")
            self.pair_note.config(text="在節點那邊輸入這 6 位數" if on else "")
        self.pair_btn.state(["!disabled"] if on else ["disabled"])
        # 名單
        sel = self._selected()
        self.tree.delete(*self.tree.get_children())
        self._rows = {}
        order = {"PENDING": 0, "ACTIVE": 1, "SUSPENDED": 2}            # 要處理的在上面、最新的在前
        devs = sorted(self.svc.devices(), key=lambda d: d["created_at"] or "", reverse=True)
        for d in sorted(devs, key=lambda d: order.get(d["status"], 9)):
            iid = self.tree.insert("", "end", tags=(d["status"],), values=(
                d["name"], STATE_NAME.get(d["status"], d["status"]),
                CONN_NAME.get(d["connection_state"], d["connection_state"]) if d["status"] == "ACTIVE" else "—",
                _ago(d["last_seen_at"]) if d["status"] == "ACTIVE" else "—",
                self._isolation(d) if d["status"] == "ACTIVE" else "—",
                _local(d["created_at"]), d["source_ip_first"] or "—", d["public_key_fingerprint"][:16]))
            self._rows[iid] = d
            if sel and sel["device_id"] == d["device_id"]:
                self.tree.selection_set(iid)
        self._update_buttons()

    def show_audit(self):
        win = tk.Toplevel(self.page)
        win.title("節點稽核紀錄（最近 200 筆，只能看不能改）")
        win.geometry(f"{self.px(820)}x{self.px(420)}")
        cols = ("ts", "action", "actor", "res", "outcome", "reason")
        t = ttk.Treeview(win, columns=cols, show="headings")
        for c, txt, w in (("ts", "時間（UTC）", 150), ("action", "動作", 170), ("actor", "誰", 110), ("res", "裝置", 120),
                          ("outcome", "結果", 60), ("reason", "理由／說明", 200)):
            t.heading(c, text=txt)
            t.column(c, width=self.px(w), stretch=(c == "reason"))
        bar = ttk.Scrollbar(win, orient="vertical", command=t.yview)
        t.configure(yscrollcommand=bar.set)
        t.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        names = {d["device_id"]: d["name"] for d in self.svc.devices()}
        for ev in self.svc.audit(200):
            t.insert("", "end", values=(ev["ts"][:19].replace("T", " "), ev["action"], ev["actor"],
                                        names.get(ev["resource_id"], (ev["resource_id"] or "")[:12]), ev["outcome"], ev["reason"] or ""))

    # ---------- 每 2 秒更新（只在分頁看得到時） ----------
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

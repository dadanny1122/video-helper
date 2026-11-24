"""新介面（v1.36.0 起，規格 docs/ui/00-介面改版規格.md）：用 Windows 內建的 WebView2 顯示 webui/ 裡的網頁。

只換畫面：下載、紀錄、Bridge、Nexus 都還是 video_helper_app.App 在做（跑在 Tk 執行緒）。
- pywebview 一定要在主執行緒 → 主執行緒跑網頁視窗，App 和 Tk 跑在 webview.start(func=) 開的執行緒
- 網頁只拿得到 Api 的 4 個函式；動作名稱有白名單，真的做事一律丟回 App.q 由 Tk 執行緒處理
- 網頁用 html= 直接塞進去（不開本機 HTTP 伺服器、不載入任何網址）
"""
import os
import sys
import threading
import time

WEBVIEW2_GUID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"     # 微軟文件：WebView2 Runtime 的登錄檔位置
# 網頁可以要求的動作（名稱 → 參數上限長度；None＝不收參數；"keys"＝紀錄編號清單；"dict"＝另外檢查）
ACTIONS = {
    "add": 20000, "paste": None, "toggle_pause": None, "retry_failed": None,
    "cancel": 100, "remove": 300, "play": 300, "reveal": 300,
    "open_folder": None, "old": 20, "pair": None, "check_update": None, "link": 20,
    "set_ui": 20, "answer": "dict", "clear_notices": None,
    # v1.37.0 下載管理頁
    "add_batch": "dict", "analyze": 2000, "enqueue_fmt": "dict", "clear_analysis": None, "clear_finished": None,
    "redownload": 300, "copy_url": 300, "subtitles": "keys", "delete_hist": "keys",
    # v1.38.0 設定頁、安裝擋廣告
    "set": "dict", "choose_dir": None, "reset_dir": None, "smart_report": None, "unpair_all": None, "stop_bridge": None,
    "update_blocklist": None, "set_ai_key": 300, "test_ai": None, "clear_ai_cache": None, "reset_speed": None,
    "export": None, "import": None, "open_backups": None, "open_log": None, "open_data": None, "setup_step": 2,
}
SETTING_KEYS = ("default_quality", "rate_limit", "skip_downloaded", "tray_always", "keep_history", "keep_stats",
                "smart_segments", "filename_template", "close_action", "theme")
MAX_KEYS = 500
QUALITIES = ("最高", "2160", "1440", "1080", "720", "480", "360")
LOAD_TIMEOUT = 25            # 秒：網頁這麼久還沒說「好了」→ 改開舊介面


def base_dir():
    return getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def webview2_version():
    """有裝 WebView2 Runtime 就回版本字串，沒有回 ""（只看登錄檔，不啟動任何東西）"""
    if sys.platform != "win32":
        return ""
    import winreg
    keys = [(winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
            (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}"),
            (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{WEBVIEW2_GUID}")]
    for hive, path in keys:
        try:
            with winreg.OpenKey(hive, path) as k:
                v = str(winreg.QueryValueEx(k, "pv")[0] or "")
                if v and v != "0.0.0.0":
                    return v
        except OSError:
            continue
    return ""


def available():
    """(能不能用, 原因)。不能用時電腦版自動開舊介面"""
    if sys.platform != "win32":
        return False, "新介面只支援 Windows"
    try:
        import webview  # noqa: F401
    except Exception as e:
        return False, f"這版打包少了 pywebview（{type(e).__name__}）"
    v = webview2_version()
    if not v:
        return False, "這台電腦沒有 WebView2（Windows 10 可以到微軟官網免費安裝「WebView2 Runtime」）"
    return True, f"WebView2 {v}"


def page_html():
    """把 index.html、app.css、app.js 合成一頁（打包進 EXE 的檔案，不連外）"""
    d = os.path.join(base_dir(), "webui")

    def read(name):
        with open(os.path.join(d, name), encoding="utf-8") as f:
            return f.read()
    css, js = read("app.css"), read("app.js")
    for bad in ("</style", "</script"):
        if bad in css.lower() or bad in js.lower():
            raise ValueError("app.css／app.js 裡不能有 " + bad)
    return read("index.html").replace("/*CSS*/", css, 1).replace("/*JS*/", js, 1)


def parse_changelog(text, n=3, items=3):
    """CHANGELOG.md 最上面 n 段 → [{ver, title, items}]（「最新消息」用）"""
    out, cur = [], None
    for line in text.splitlines():
        if line.startswith("## "):
            if len(out) >= n:
                break
            head = line[3:].strip()
            ver, _, title = head.partition(" ")
            cur = {"ver": ver.lstrip("v"), "title": title.strip(), "items": []}
            out.append(cur)
        elif cur is not None and line.startswith("- ") and len(cur["items"]) < items:
            t = line[2:].strip()
            cur["items"].append(t if len(t) <= 80 else t[:79] + "…")
    return out


def load_news():
    try:
        with open(os.path.join(base_dir(), "CHANGELOG.md"), encoding="utf-8") as f:
            return parse_changelog(f.read())
    except Exception:
        return []


def check_action(name, arg):
    """網頁送來的動作：不在白名單、參數型別／長度不對都拒絕。回傳整理過的參數"""
    if name not in ACTIONS:
        raise ValueError("不認得的動作")
    lim = ACTIONS[name]
    if lim is None:
        return None
    if lim == "keys":
        if not isinstance(arg, list) or not arg or len(arg) > MAX_KEYS or not all(isinstance(k, str) and len(k) <= 40 for k in arg):
            raise ValueError("參數不對")
        return list(arg)
    if lim == "dict":
        if not isinstance(arg, dict):
            raise ValueError("參數不對")
        if name in ("answer", "enqueue_fmt"):
            n = "choice" if name == "answer" else "i"
            if not isinstance(arg.get("id"), str) or type(arg.get(n)) is not int:
                raise ValueError("參數不對")
            return {"id": arg["id"][:200], n: arg[n]}
        if name == "set":
            k, v = arg.get("key"), arg.get("value")
            if k not in SETTING_KEYS or not isinstance(v, (str, bool, int, float)) or (isinstance(v, str) and len(v) > 100):
                raise ValueError("參數不對")
            return {"key": k, "value": v}
        if name == "add_batch":
            t, q = arg.get("text"), arg.get("quality")
            if not isinstance(t, str) or not t or len(t) > 20000 or q not in QUALITIES:
                raise ValueError("參數不對")
            return {"text": t, "quality": q}
        raise ValueError("參數不對")
    if not isinstance(arg, str) or not arg or len(arg) > lim:
        raise ValueError("參數不對")
    return arg


class Api:
    """網頁看得到的只有這幾個函式（pywebview 不會給底線開頭的屬性）"""

    def __init__(self, host):
        self._host = host

    def state(self):
        return self._host.state

    def static(self):
        return self._host.static

    def act(self, name, arg=None):
        return self._host.act(name, arg)

    def ready(self):
        self._host.loaded = time.time()
        return True

    def history(self, query=None):
        return self._host.history(query)

    def thumbs(self, keys=None):
        return self._host.thumbs(keys)


class Host:
    """網頁視窗這一邊：App 用它來顯示／藏起視窗、發布狀態；網頁用它丟動作給 App"""

    def __init__(self):
        self.window = None
        self.app = None              # App 建好後自己填
        self.state = {}              # App 每 0.5 秒換一份新的（整份換，不改裡面 → 不用鎖）
        self.static = {"news": load_news()}
        self.loaded = None           # 網頁說「好了」的時間
        self.hidden = False
        self.allow_close = False
        self.hist = None             # 下載紀錄（App 有變才整份換）：[{key, title, site, …}]
        self.files = {}              # 紀錄編號 → 影片檔路徑（縮圖用；網頁拿不到）
        self.thumb_dir = None
        self._thumbs = None

    def act(self, name, arg):
        try:
            arg = check_action(name, arg)
        except ValueError as e:
            return {"ok": False, "msg": str(e)}
        app = self.app
        if not app:
            return {"ok": False, "msg": "電腦版還在啟動，等一下再按"}
        ev = threading.Event()
        box = {}
        app.q.put(("web_action", (name, arg, box, ev)))
        ev.wait(5)                    # Tk 執行緒處理完會 set；太久就先回「處理中」
        return box.get("reply") or {"ok": True}

    def history(self, query):
        """下載管理頁：搜尋／篩選下載紀錄（在網頁的執行緒跑，只讀 App 換好的那一份）"""
        q = query if isinstance(query, dict) else {}
        text = str(q.get("text") or "")[:100].lower().strip()
        st = q.get("status") if q.get("status") in ("done", "fail") else ""
        site = str(q.get("site") or "")[:60]
        off = max(0, min(int(q.get("offset") or 0), 100000)) if isinstance(q.get("offset"), int) else 0
        lim = max(1, min(int(q.get("limit") or 100), 200)) if isinstance(q.get("limit"), int) else 100
        rows = self.hist or []
        sites = sorted({r["site"] for r in rows if r["site"]})
        counts = {"all": len(rows), "done": sum(r["st"] == "done" for r in rows)}
        counts["fail"] = counts["all"] - counts["done"]
        if st == "done":
            rows = [r for r in rows if r["st"] == "done"]
        elif st == "fail":
            rows = [r for r in rows if r["st"] != "done"]
        if site:
            rows = [r for r in rows if r["site"] == site]
        if text:
            rows = [r for r in rows if text in r["title"].lower() or text in r["site"]]
        return {"total": len(rows), "items": rows[off:off + lim], "sites": sites, "counts": counts}

    def thumbs(self, keys):
        """縮圖：有做好的回 data: 圖片，還沒做的排隊做（回 None），做不出來的回 ""（網頁就不再問）"""
        if not isinstance(keys, list):
            return {}
        if self._thumbs is None:
            from webui.thumbs import ThumbMaker
            self._thumbs = ThumbMaker(self.thumb_dir)
        out = {}
        for k in keys[:80]:
            if isinstance(k, str) and k in self.files:
                out[k] = self._thumbs.get(self.files[k])
        return out

    # ---- 視窗 ----
    def show(self):
        self.hidden = False
        if self.window:
            try:
                self.window.show()
                self.window.restore()
            except Exception:
                pass

    def hide(self):
        self.hidden = True
        if self.window:
            try:
                self.window.hide()
            except Exception:
                pass

    def close(self):
        """App 結束了 → 關掉網頁視窗，webview.start() 就會返回、程式結束"""
        self.allow_close = True
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def on_closing(self, *a):
        """按網頁視窗右上角的 X：交給 App 決定（縮到系統匣／問要不要結束）；回傳 False＝先不關"""
        if self.allow_close or not self.app:
            return True
        self.app.q.put(("web_close", None))
        return False


def run(start_app, title, size=(1280, 860)):
    """主執行緒呼叫：開網頁視窗；start_app(host) 會在另一個執行緒建 Tk 和 App 並跑 mainloop"""
    import webview
    webview.settings["ALLOW_DOWNLOADS"] = False
    webview.settings["OPEN_EXTERNAL_LINKS_IN_BROWSER"] = True
    host = Host()
    win = webview.create_window(title, html=page_html(), js_api=Api(host), width=size[0], height=size[1],
                                min_size=(980, 700), background_color="#0d1020", text_select=False)
    host.window = win
    win.events.closing += host.on_closing

    def tk_thread():
        try:
            start_app(host)
        finally:
            host.close()
    webview.start(tk_thread, gui="edgechromium", debug=False, private_mode=True)
    return host

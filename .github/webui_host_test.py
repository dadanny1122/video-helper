"""新介面（webui/）的電腦版那一半：
  python .github/webui_host_test.py          → 假的網頁視窗：白名單、狀態、動作、關視窗流程（Linux 用 xvfb-run 也能跑）
  python .github/webui_host_test.py --real   → Windows：真的開 WebView2，等網頁說好了、數卡片、截圖、按 X 關掉
"""
import json
import os
import sys
import tempfile
import threading
import time

os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
os.environ["VH_NEXUS_ROOT"] = os.path.join(os.environ["VIDEO_HELPER_HOME"], "nexus")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tkinter as tk  # noqa: E402

import video_helper_app as a  # noqa: E402
import webui  # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("✅ " if cond else "❌ ") + msg, flush=True)
    if not cond:
        fails += 1
        print(f"::error title=新介面測試::{msg}", flush=True)


# ---- 1. 不用視窗的部分 ----
ok(set(n for n in dir(webui.Api) if not n.startswith("_")) == {"state", "static", "act", "ready", "history", "thumbs", "nexus"}, "網頁只拿得到 7 個函式")
for name, arg in (("eval", "x"), ("add", ""), ("add", 5), ("add", "x" * 20001), ("cancel", None), ("answer", {"id": 1}),
                  ("old", "x" * 21), ("subtitles", "hab"), ("subtitles", []), ("subtitles", ["x" * 41]),
                  ("delete_hist", ["a"] * 501), ("add_batch", {"text": "x", "quality": "999"}), ("enqueue_fmt", {"id": "a", "i": "1"}),
                  ("enqueue_fmt", {"id": "a", "i": True}), ("set", {"key": "download_dir", "value": "C:/x"}),
                  ("set", {"key": "ai_key", "value": "x"}), ("set", {"key": "theme", "value": ["dark"]}), ("setup_step", "123")):
    try:
        webui.check_action(name, arg)
        ok(False, f"應該拒絕 {name}({arg!r:.30})")
    except ValueError:
        pass
ok(webui.check_action("answer", {"id": "a", "choice": 1, "x": 9}) == {"id": "a", "choice": 1}, "answer 只留 id、choice")
html = webui.page_html()
ok("/*CSS*/" not in html and "/*JS*/" not in html and "Content-Security-Policy" in html, "網頁合成一頁、有 CSP")
ok("http://" not in html.split("<script>")[0] and "https://" not in html.split("<script>")[0], "網頁沒有載入外部網址")
news = webui.parse_changelog("# 標題\n\n## v1.2.0 新東西\n- 一\n- 二\n- 三\n- 四\n\n## v1.1.0 舊\n- a\n\n## v1.0.0 x\n## v0.9 y\n")
ok([n["ver"] for n in news] == ["1.2.0", "1.1.0", "1.0.0"] and news[0]["items"] == ["一", "二", "三"], "最新消息讀 CHANGELOG 前 3 段")
ok(webui.load_news() and webui.load_news()[0]["ver"] == a.VERSION, "CHANGELOG 最上面就是這一版")


class FakeWindow:
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def restore(self):
        pass

    def hide(self):
        self.calls.append("hide")

    def destroy(self):
        self.calls.append("destroy")


def drain(app, root, sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def fake_mode():
    host = webui.Host()
    host.window = FakeWindow()
    root = tk.Tk()
    app = a.App(root, web=host)
    drain(app, root)
    ok(host.app is app and root.state() == "withdrawn", "原本的視窗藏起來、網頁接上 App")
    ok(host.state.get("start_page") == "setup", "第一次打開：新介面先帶去「安裝擋廣告」")
    st = host.state
    ok(st.get("version") == a.VERSION and len(st["kpis"]) == 4 and st["theme"] in ("dark", "light"), "狀態有版本、4 張數據卡、主題")
    json.dumps(st)                                                  # 一定要能轉成 JSON（pywebview 會轉）

    # 動作：要從別的執行緒呼叫（跟 pywebview 一樣），Tk 執行緒繼續跑
    def call(name, arg=None):
        box = {}
        th = threading.Thread(target=lambda: box.update(r=host.act(name, arg)))
        th.start()
        while th.is_alive():
            root.update()
            time.sleep(0.01)
        return box["r"]
    ok(call("nope")["ok"] is False, "不認得的動作被拒絕")
    r = call("add", "這不是網址")
    ok(r["ok"] is False and "網址" in r["msg"], "沒有網址 → 提示")
    ok(call("add", "\n".join(f"https://x.com/{i}" for i in range(51)))["ok"] is False, "一次最多 50 個")
    app.analyze_fn = lambda url: (_ for _ in ()).throw(RuntimeError("測試：不連網"))
    r = call("add", "https://www.xvideos.com/video1/a\nhttps://www.xvideos.com/video1/a")
    ok(r["ok"] and "1 部" in r["msg"], "重複網址只算一次")
    drain(app, root, 1.0)
    r = call("toggle_pause")
    ok(r["ok"] and app.queue_paused and host.state["paused"], "全部暫停")
    call("toggle_pause")
    ok(not app.queue_paused, "全部繼續")
    ok(call("play", "hnotexist")["ok"] is False, "找不到的紀錄不能播放")
    ok(call("remove", "q999")["ok"] is False and call("cancel", "q999")["ok"] is False, "找不到的任務不能移除／取消")
    # 紀錄裡的檔案
    f = os.path.join(os.environ["VIDEO_HELPER_HOME"], "v.mp4")
    open(f, "wb").write(b"x")
    app.history.add(title="<b>測試</b>", url="https://www.pornhub.com/view_video.php?viewkey=1", height=720,
                    status=a.DONE, file=f, size=1)
    app.web_publish()
    row = next(t for t in host.state["tasks"] if t["title"] == "<b>測試</b>")
    ok(row["open"] and row["st"] == "done" and row["key"].startswith("h"), "下載紀錄出現在任務列表、可以打開")
    opened = []
    a.open_path, real_open = (lambda p, select=False: opened.append((p, select))), a.open_path
    a.play_file, real_play = (lambda p: opened.append((p, "play"))), a.play_file
    call("reveal", row["key"])
    call("play", row["key"])
    ok(opened == [(f, True), (f, "play")], "📂 打開檔案位置、▶ 用播放器打開影片（不是打開資料夾）")
    a.open_path, a.play_file = real_open, real_play
    # ---- v1.37.0 下載管理 ----
    from PIL import Image
    made = []

    def fake_thumb(p):
        made.append(p)
        return Image.linear_gradient("L").convert("RGB").resize((320, 180)) if p == f else None   # 純色會被當成「全黑／全白」
    host.thumbs([])                                        # 建 ThumbMaker
    host._thumbs.make = fake_thumb
    got = host.thumbs([row["key"], "hnotexist"])
    ok(got.get(row["key"]) is None and "hnotexist" not in got, "縮圖：第一次排隊做、不認得的編號不理")
    for _ in range(50):
        time.sleep(0.05)
        got = host.thumbs([row["key"]])
        if got.get(row["key"]):
            break
    ok(str(got.get(row["key"])).startswith("data:image/jpeg;base64,"), "縮圖做好了（JPEG）")
    ok(len([n for n in os.listdir(host.thumb_dir) if n.endswith(".jpg")]) == 1, "縮圖存在設定資料夾 thumbs")
    res = host.history({"text": "測試"})
    ok(res["total"] == 1 and res["items"][0]["key"] == row["key"], "紀錄搜尋")
    ok(host.history({"status": "fail"})["total"] >= 1 and host.history({"site": "pornhub.com"})["total"] == 1, "紀錄篩選（失敗、網站）")
    ok(host.history({"offset": "x", "limit": 10**9})["total"] >= 2, "查詢參數亂給也不會出錯")
    fmts = [{"height": 1080, "size": 10_000_000, "exact": True, "direct": False},
            {"height": 720, "size": 5_000_000, "exact": False, "direct": True}]
    app.analyze_fn = lambda url: ("影片A", 125, fmts, 0)
    ok(call("analyze", "ftp://x")["ok"] is False, "讀取：網址要 http 開頭")
    call("analyze", "https://www.xvideos.com/video9/abc")
    drain(app, root, 0.5)
    an = host.state["analysis"]
    ok(an and an["state"] == "ok" and [x["label"] for x in an["formats"]] == ["1080p", "720p"] and an["duration"], "讀取 → 畫質清單")
    ok(call("enqueue_fmt", {"id": "舊的", "i": 0})["ok"] is False, "過期的畫質清單不能用")
    n0 = len(app.queue.tasks)
    r = call("enqueue_fmt", {"id": an["id"], "i": 1})
    ok(r["ok"] and len(app.queue.tasks) == n0 + 1 and app.queue.tasks[-1]["fmt"]["height"] == 720 and host.state["analysis"] is None,
       "選 720p 加入佇列")
    call("analyze", "https://www.xvideos.com/video9/abc")
    drain(app, root, 0.5)
    ok(call("enqueue_fmt", {"id": host.state["analysis"]["id"], "i": 1})["ok"] is False, "同一部同畫質不重複加")
    call("clear_analysis")
    app.queue.tasks[-1]["status"] = a.CANCELLED
    ok("清掉" in call("clear_finished")["msg"], "清掉已結束的")
    call("copy_url", row["key"])
    ok(root.clipboard_get() == "https://www.pornhub.com/view_video.php?viewkey=1", "複製網址")
    subs = []
    app.subtitle_dialog = lambda video=None, files=None: subs.append(files)
    ok(call("subtitles", [row["key"], "hnotexist"])["ok"] and subs == [[f]], "AI 字幕：只帶檔案還在的")
    want = []
    app.web_analyze = lambda url, want_height=None: want.append((url, want_height)) or {"ok": True}
    call("redownload", row["key"])
    ok(want == [("https://www.pornhub.com/view_video.php?viewkey=1", 720)], "重新下載：用原本的畫質讀取")
    del app.web_analyze
    n0 = len(app.history.items)
    call("delete_hist", [row["key"]])
    drain(app, root, 0.2)
    ok(len(app.history.items) == n0 - 1 and os.path.isfile(f) and not any(t["key"] == row["key"] for t in host.hist),
       "刪除紀錄（影片檔還在）")
    # ---- v1.38.0 設定頁 ----
    cfg = host.state["settings"]
    ok(cfg["default_quality"] == app.settings["default_quality"] and "ai_key" not in cfg and "sk-" not in json.dumps(cfg),
       "設定頁拿得到設定值，但沒有金鑰本身")
    ok(call("set", {"key": "default_quality", "value": "720"})["ok"] and app.settings["default_quality"] == "720"
       and app.q_var.get() == "720", "改預設畫質（原本視窗也跟著改）")
    ok(call("set", {"key": "default_quality", "value": "999"})["ok"] is False, "不認得的畫質不能存")
    ok(call("set", {"key": "rate_limit", "value": 20})["ok"] and app.settings["rate_limit_mbps"] == 20 and a.THROTTLE.bps > 0,
       "速度上限馬上生效")
    ok(call("set", {"key": "rate_limit", "value": -1})["ok"] is False and call("set", {"key": "rate_limit", "value": True})["ok"] is False,
       "速度上限不能是負的或 true／false")
    call("set", {"key": "rate_limit", "value": 0})
    ok(call("set", {"key": "keep_stats", "value": "yes"})["ok"] is False, "開關一定要 true／false")
    call("set", {"key": "keep_stats", "value": False})
    ok(app.settings["keep_stats"] is False, "開關 → 存起來")
    call("set", {"key": "theme", "value": "dark"})
    drain(app, root, 0.2)
    ok(app.settings["theme"] == "dark" and host.state["theme"] == "dark", "外觀換成深色（新介面跟著換）")
    ok(call("set", {"key": "close_action", "value": "亂寫"})["ok"] is False, "關閉視窗的選項要在清單裡")
    d2 = tempfile.mkdtemp()
    real_ask = a.filedialog.askdirectory
    a.filedialog.askdirectory = lambda **k: d2
    call("choose_dir")
    a.filedialog.askdirectory = real_ask
    drain(app, root, 0.2)
    ok(app.settings.download_dir == d2 and host.state["settings"]["download_dir"] == d2 and root.state() == "withdrawn",
       "選資料夾（用 Windows 對話框選，選完原本的視窗還是藏著）")
    call("reset_dir")
    ok(app.settings["download_dir"] == "", "下載資料夾換回預設")
    r = call("smart_report")
    ok(r["ok"] and isinstance(r.get("report"), str), "智慧分段報告")
    ok(call("set_ai_key", "壞掉的金鑰")["ok"] is False, "金鑰格式不對就不存")
    ok(call("set_ai_key", "sk-ant-test-" + "a" * 30)["ok"] and app.settings["ai_key"].startswith("sk-ant-test")
       and host.state["settings"]["ai_has_key"], "存金鑰（網頁只知道「有設定」）")
    call("set_ai_key", "-")
    ok(app.settings["ai_key"] == "", "清除金鑰")
    app.settings["bridge_tokens"] = ["x"]
    ok("1 個" in call("unpair_all")["msg"] and not app.settings["bridge_tokens"], "取消所有配對")
    opened2 = []
    a.webbrowser.open, real_wb = (lambda u: opened2.append(u)), a.webbrowser.open
    a.subprocess.Popen, real_popen = (lambda args, **k: opened2.append(args[-1])), a.subprocess.Popen
    call("setup_step", "1")
    call("setup_step", "3")
    a.webbrowser.open, a.subprocess.Popen = real_wb, real_popen
    ok(opened2 == [a.URL_VM_STORE, a.URL_SCRIPT] and host.state["setup"]["done"] == ["1", "3"], "安裝擋廣告：打開對的網頁、打勾")
    ok(call("setup_step", "9")["ok"] is False, "沒有第 9 步")
    call("old", "security")
    drain(app, root, 0.2)
    ok(app.adv.state() == "normal" and root.state() == "withdrawn", "🔒 安全中心：只開進階設定小視窗")
    app.adv.withdraw()
    app.add_notice("🖧", "節點傳回：測試", "dl")
    app2_notices = json.load(open(os.path.join(os.environ["VIDEO_HELPER_HOME"], "notices.json"), encoding="utf-8"))
    ok(app2_notices[0]["page"] == "dl", "通知存檔（重開還在）、記得要跳哪一頁")
    # ---- v1.39.0 Nexus 管理頁 ----
    import socket
    with socket.socket() as so:
        so.bind(("127.0.0.1", 0))
        free_port = so.getsockname()[1]

    def nexus_snapshot():
        host.want_nexus = time.time()
        app._web_nx_full_t = 0
        app.web_publish()
        return host.nexus_full
    nx = nexus_snapshot()
    ok(nx and nx["ok"] and nx["running"] is False and nx["types"], "Nexus 頁：Master 沒開也讀得到")
    ok(call("nx_bind", {"host": "8.8.8.8", "port": free_port})["ok"] is False, "綁定位址只能選本機網卡")
    ok(call("nx_bind", {"host": "127.0.0.1", "port": 80})["ok"] is False, "埠要 1024 以上")
    ok(call("nx_bind", {"host": "127.0.0.1", "port": free_port})["ok"], "存綁定位址和埠")
    ok(call("nx_pair")["ok"] is False, "Master 沒開不能產生配對碼")
    ok(call("nx_enable", "maybe")["ok"] is False, "nx_enable 只收 on／off")
    r = call("nx_enable", "on")
    ok(r["ok"] and app.nexus.running, "啟用 Master " + str(r.get("msg")))
    call("nx_pair")
    nx = nexus_snapshot()
    ok(nx["running"] and len(nx["pair"]) == 6 and nx["pair_left"] > 0 and nx["port"] == free_port, "配對碼、埠")
    ok(call("nx_device", {"op": "drop_table", "id": "x", "reason": "r"})["ok"] is False, "裝置操作只收 5 種")
    ok(call("nx_device", {"op": "approve", "id": "不存在", "reason": ""})["ok"] is False, "沒有理由不執行")
    ok(call("nx_task_new", {"type": "evil.run"})["ok"] is False, "不認得的任務類型不能送")
    r = call("nx_task_new", {"type": "test.echo", "text": "哈囉", "runtime": 30, "tries": 1})
    nx = nexus_snapshot()
    ok(r["ok"] and nx["tasks"] and nx["tasks"][0]["what"] == "哈囉", "新增節點任務（回聲）")
    tid = nx["tasks"][0]["id"]
    d = call("nx_detail", tid)
    ok(d["ok"] and "回聲" in d["detail"]["head"] and d["detail"]["timeline"], "任務詳情")
    ok(call("nx_cancel", {"id": tid, "reason": ""})["ok"] is False, "取消要理由")
    r = call("nx_cancel", {"id": tid, "reason": "測試取消"})
    ok(r["ok"] and nexus_snapshot()["tasks"][0]["status"] in ("CANCELLED", "CANCELLING"), "取消節點任務")
    ok(call("nx_audit")["ok"] and any(x[1].startswith("task.") for x in call("nx_audit")["audit"]), "稽核紀錄")
    call("nx_enable", "off")
    ok(not app.nexus.running and app.nexus.cfg["enabled"] is False, "關閉 Master")
    # ---- v1.41.0 安全與統計 ----
    app.settings["bridge_tokens"] = ["f" * 64]
    app.settings["bridge_meta"] = {"f" * 64: {"ua": "Chrome", "paired": time.time(), "last": time.time(), "uses": 3}}
    r = call("sec_view")
    ok(r["ok"] and r["sec"]["paired"][0]["ua"] == "Chrome" and r["sec"]["paired"][0]["id"] == "f" * 16 and "f" * 17 not in json.dumps(r),
       "安全頁：配對清單（只給編號前 16 碼）")
    ok(call("sec_unpair", "0" * 16)["ok"] is False and call("sec_unpair", "f" * 16)["ok"] and not app.settings["bridge_tokens"], "取消單一配對")
    ok(call("set", {"key": "log_keep_days", "value": 5})["ok"] is False and call("set", {"key": "log_keep_days", "value": 7})["ok"]
       and app.settings["log_keep_days"] == 7, "日誌天數只收 0/3/7/14/30")
    ok(call("clear_stats")["ok"] and not app.stats.items, "清除統計")
    ok(call("clear_logs")["ok"], "清空日誌")
    # 舊介面
    call("old", "set")
    drain(app, root)
    ok(root.state() == "normal" and app.nb.index("current") == 2, "還沒做好的「設定」→ 打開原本的視窗到 ③")
    root.withdraw()
    # 通知
    app.add_notice("✅", "測試通知")
    app.web_publish()
    ok(host.state["notices"][0]["text"] == "測試通知", "通知中心")
    call("clear_notices")
    ok(host.state["notices"] == [], "清除通知")
    # 關視窗：下載中 → 用網頁問；回到視窗 → 不關
    app.downloads_active = lambda: True
    ok(host.on_closing() is False, "按 X 先不關，交給 App")
    drain(app, root)
    ask = host.state["ask"]
    ok(ask and "還在下載" in ask["title"] and ask["buttons"][-1] == "回到視窗", "還在下載 → 網頁跳出問題")
    call("answer", {"id": ask["id"], "choice": len(ask["buttons"]) - 1})
    ok(host.state["ask"] is None and not app.quitting, "選「回到視窗」就不關")
    # 切回舊介面
    call("set_ui", "classic")
    drain(app, root)
    ok(app.web_fallback and app.settings["ui"] == "classic" and root.state() == "normal" and "hide" in host.window.calls,
       "切回舊介面：網頁藏起來、原本的視窗出來、設定記住")
    ok(app.settings["ui_chosen"] is True, "切回原本的視窗會記住是自己選的（下次不會被換回新介面）")
    st2 = a.Settings(os.path.join(tempfile.mkdtemp(), "s.json"))
    st2["ui"] = "classic"
    ok(a.prefer_web_ui(st2) and st2["ui"] == "web", "v1.40.0：舊版存的 classic、沒自己選過 → 換成新介面")
    st2["ui"], st2["ui_chosen"] = "classic", True
    ok(not a.prefer_web_ui(st2) and st2["ui"] == "classic", "自己選過原本的視窗 → 照他選的")
    app.web_fallback = False
    # 沒在下載 → 按 X 直接結束
    app.downloads_active = lambda: False
    host.on_closing()
    drain(app, root)
    ok(app.quitting, "沒在下載時按 X 就結束")


def real_mode():
    """Windows CI：真的開 WebView2"""
    ok_avail, why = webui.available()
    ok(ok_avail, f"WebView2 可以用（{why}）")
    if not ok_avail:
        return
    shots = os.path.join(ROOT, "webui-shots")
    os.makedirs(shots, exist_ok=True)
    result = {}

    def start_app(host):
        root = tk.Tk()
        app = a.App(root, web=host)
        t0 = time.time()

        def check():
            if not host.loaded and time.time() - t0 < 30:
                root.after(200, check)
                return
            result["load_s"] = round(time.time() - t0, 2) if host.loaded else None

            def probe():
                try:
                    time.sleep(1.5)                     # 讓第一次狀態畫上去
                    result["first"] = host.window.evaluate_js("!document.getElementById('page-setup').hidden")   # 第一次打開先到安裝擋廣告
                    host.window.evaluate_js("document.querySelector('#nav a[data-page=\"home\"]').click()")
                    time.sleep(1.0)
                    result["kpis"] = host.window.evaluate_js("document.querySelectorAll('.kpi').length")
                    result["ver"] = host.window.evaluate_js("document.getElementById('ver').textContent")
                    host.window.evaluate_js("document.querySelector('#nav a[data-page=\"dl\"]').click()")
                    time.sleep(1.0)
                    result["dl"] = host.window.evaluate_js("!document.getElementById('page-dl').hidden && document.querySelectorAll('#dlq option').length")
                    host.window.evaluate_js("document.querySelector('#nav a[data-page=\"set\"]').click()")
                    time.sleep(1.0)
                    result["set"] = host.window.evaluate_js("document.getElementById('s-q').options.length")
                    host.window.evaluate_js("document.querySelector('#nav a[data-page=\"nodes\"]').click()")
                    time.sleep(3.0)
                    result["nodes"] = host.window.evaluate_js("document.getElementById('nf-type').options.length")
                    from PIL import ImageGrab
                    w = host.window
                    ImageGrab.grab(bbox=(w.x, w.y, w.x + w.width, w.y + w.height), all_screens=True).save(
                        os.path.join(shots, "windows-webview2-nodes.png"))
                    result["shot"] = True
                except Exception as e:
                    result["err"] = repr(e)
                ok_close = host.on_closing()            # 跟按 X 一樣
                result["close_cancelled"] = ok_close is False
            threading.Thread(target=probe, daemon=True).start()
        root.after(200, check)
        root.mainloop()
        result["quit"] = app.quitting

    t = time.time()
    webui.run(start_app, "Video Helper 測試")
    print("結果：", result, flush=True)
    ok(result.get("load_s") is not None, f"網頁 30 秒內載入（{result.get('load_s')} 秒）")
    ok(result.get("first") is True, "第一次打開先到「安裝擋廣告」")
    ok(result.get("kpis") == 4, "WebView2 裡畫出 4 張數據卡")
    ok(result.get("ver") == "v" + a.VERSION, "版本號從電腦版傳到網頁")
    ok(result.get("dl") == 7, "下載管理頁打得開、畫質選單有 7 個")
    ok(result.get("set") == 7, "設定頁打得開、預設畫質有 7 個選項")
    ok((result.get("nodes") or 0) >= 4, f"Nexus 管理頁讀得到節點資料（任務類型 {result.get('nodes')} 種）")
    ok(result.get("shot"), "截圖 " + str(result.get("err", "")))
    # 縮圖：請 Windows 做一張（GitHub 的 Windows Server 不一定有影片縮圖功能 → 只記錄結果，不算失敗）
    try:
        import subprocess
        from webui.thumbs import fit, shell_thumbnail
        mp4 = os.path.join(os.environ["VIDEO_HELPER_HOME"], "thumb-test.mp4")
        subprocess.run([a.FFMPEG, "-y", "-loglevel", "error", "-i", os.path.join(ROOT, ".github", "fixtures", "sample.ts"),
                        "-c", "copy", "-bsf:a", "aac_adtstoasc", mp4], check=True)
        img = shell_thumbnail(mp4)
        if img is not None:
            fit(img).save(os.path.join(shots, "thumb.jpg"))
        print(f"::notice title=Windows 縮圖::{'做得出來 ' + str(img.size) if img is not None else '這台 Windows 做不出影片縮圖（會顯示網站色塊）'}", flush=True)
    except Exception as e:
        print(f"::warning title=Windows 縮圖::{e!r}", flush=True)
    ok(result.get("close_cancelled") and result.get("quit"), "按 X → 交給 App → 沒在下載就結束")
    ok(time.time() - t < 90, "整個流程 90 秒內結束")


if "--real" in sys.argv:
    real_mode()
else:
    fake_mode()
print("全部通過" if not fails else f"{fails} 項失敗", flush=True)
sys.exit(1 if fails else 0)

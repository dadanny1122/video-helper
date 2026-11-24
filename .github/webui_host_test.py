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
ok(set(n for n in dir(webui.Api) if not n.startswith("_")) == {"state", "static", "act", "ready"}, "網頁只拿得到 4 個函式")
for name, arg in (("eval", "x"), ("add", ""), ("add", 5), ("add", "x" * 20001), ("cancel", None), ("answer", {"id": 1}),
                  ("old", "x" * 21)):
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
                    result["kpis"] = host.window.evaluate_js("document.querySelectorAll('.kpi').length")
                    result["ver"] = host.window.evaluate_js("document.getElementById('ver').textContent")
                    from PIL import ImageGrab
                    w = host.window
                    ImageGrab.grab(bbox=(w.x, w.y, w.x + w.width, w.y + w.height), all_screens=True).save(
                        os.path.join(shots, "windows-webview2.png"))
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
    ok(result.get("kpis") == 4, "WebView2 裡畫出 4 張數據卡")
    ok(result.get("ver") == "v" + a.VERSION, "版本號從電腦版傳到網頁")
    ok(result.get("shot"), "截圖 " + str(result.get("err", "")))
    ok(result.get("close_cancelled") and result.get("quit"), "按 X → 交給 App → 沒在下載就結束")
    ok(time.time() - t < 90, "整個流程 90 秒內結束")


if "--real" in sys.argv:
    real_mode()
else:
    fake_mode()
print("全部通過" if not fails else f"{fails} 項失敗", flush=True)
sys.exit(1 if fails else 0)

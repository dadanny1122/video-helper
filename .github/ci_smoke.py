"""GitHub Actions 打包前的檢查：在 Windows 上打開視窗、走過各分頁，並確認內建 ffmpeg 能用"""
import os, sys, subprocess, tempfile
os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
import video_helper_app as a

# 1. 內建 ffmpeg：產生 2 秒測試影片，再換包裝一次
assert a.FFMPEG and os.path.exists(a.FFMPEG), "找不到內建 ffmpeg"
d = tempfile.mkdtemp()
src, dst = os.path.join(d, "src.mp4"), os.path.join(d, "dst.mp4")
subprocess.run([a.FFMPEG, "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=duration=2:size=320x240",
                "-pix_fmt", "yuv420p", src], check=True)
subprocess.run([a.FFMPEG, "-y", "-loglevel", "error", "-i", src, "-c", "copy", "-movflags", "+faststart", dst], check=True)
assert os.path.getsize(dst) > 1000, "ffmpeg 轉檔失敗"
print("FFMPEG_OK", a.FFMPEG)

# 2. 視窗：4 個分頁、安裝精靈 3 步
r = tk.Tk()
app = a.App(r)
r.update()
assert len(app.nb.tabs()) == 4, "分頁數量不對"
assert len(app.step_marks) == 3, "安裝精靈步驟數量不對"

# 3. 換成假的讀取／下載函式，走一遍佇列
import time
def wait(cond, sec=15):
    end = time.time() + sec
    while not cond():
        r.update(); time.sleep(0.02)
        assert time.time() < end, "等太久"

fmts = [{"id": h, "height": h, "size": h * 100000, "exact": False, "audio": True, "direct": False, "url": "", "headers": {}}
        for h in (1080, 720, 480)]
def fake_analyze(url):
    return (url, 634, fmts, 20.0)
def fake_download(url, fmt, out, on_progress=None, on_stage=None, cancel_event=None, name_template=None):
    if url.startswith("fail"):
        raise RuntimeError("HTTP 404")
    if url.startswith("slow"):
        while not cancel_event.is_set():
            on_progress(1, 10, 1e6, 5); time.sleep(0.05)
        raise a.Cancelled()
    on_progress(10, 10, 1e6, 0)
    return (dst, None)
app.analyze_fn, app.download_fn = fake_analyze, fake_download

app.settings["default_quality"] = "720"
app.on_analyzed(("https://example.com/v", "測試影片", 634, fmts, 20.0))
r.update()
assert app.tree.selection() == ("1",), "預設畫質沒有選到 720p"
assert "10:34" in app.title_lbl.cget("text"), "沒有顯示影片長度"

mk = lambda u: {"url": u, "title": u, "fmt": fmts[1], "size": 1, "out": d}
t_ok, t_fail, t_slow = app.enqueue(mk("ok")), app.enqueue(mk("fail")), app.enqueue(mk("slow"))
wait(lambda: t_slow["status"] == a.RUNNING)
assert str(app.btn_cancel["state"]) == "normal", "下載中取消按鈕應該可以按"
app.cancel_event.set()
wait(lambda: t_slow["status"] == a.CANCELLED)
assert [t["status"] for t in (t_ok, t_fail, t_slow)] == [a.DONE, a.FAILED, a.CANCELLED], "佇列狀態不對"
assert str(app.btn_retry["state"]) == "normal", "有失敗的應該可以重試"
assert [x["status"] for x in app.history.items] == ["取消", "失敗", "完成"], "下載紀錄不對"
assert len(app.qtree.get_children()) == 3 and len(app.htree.get_children()) == 3, "表格沒有顯示"

app.retry()
wait(lambda: t_fail["status"] == a.FAILED and app.cancel_event is None)

# 4. 一次加入多個網址
app.start_batch(["okA", "okB"])
wait(lambda: not app.batch_running and app.queue.count(a.WAITING, a.RUNNING) == 0)
assert app.queue.count(a.DONE) == 3, "批次加入的沒有下載完"

app.queue_clear(); r.update()
assert len(app.qtree.get_children()) == 0, "清除已結束沒有作用"
assert app.tpl_preview.cget("text").startswith("範例："), "檔名格式範例沒有顯示"

# 5. 匯出 → 改設定 → 匯入 → 畫面跟著更新
bk = os.path.join(d, "backup.json")
app.settings["default_quality"] = "480"
a.export_backup(bk, app.settings, app.history)
app.settings["default_quality"] = "最高"
sset, hist, warns = a.import_backup(bk)
app.settings.data.update(sset); app.apply_settings_to_ui(); r.update()
assert app.q_var.get() == "480", "匯入後畫面沒有更新"

# 6. 瀏覽器連動：配對 → 送影片 → 查進度（真的 HTTP 請求）
import json, urllib.request, urllib.error
port = app.bridge.start()
assert port, "連動服務沒有啟動"
def call(method, path, body=None, token=None, origin="chrome-extension://test"):
    h = {"Origin": origin}
    if body is not None:
        h["Content-Type"] = "application/json"
    if token:
        h["X-VH-Token"] = token
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method=method, headers=h,
                                 data=json.dumps(body).encode() if body is not None else None)
    try:
        with urllib.request.build_opener(urllib.request.ProxyHandler({})).open(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")
assert call("GET", "/ping", origin="https://evil.example")[0] == 403, "一般網頁應該被拒絕"
code = app.bridge.new_pair_code()
st, dd = call("POST", "/pair", {"code": code})
assert st == 200 and dd.get("token"), "配對失敗"
tok = dd["token"]
st, dd = call("POST", "/enqueue", {"url": "https://www.pornhub.com/view_video.php?viewkey=ci", "height": 480}, tok)
assert st == 200, "送影片失敗"
ticket = dd["ticket"]
wait(lambda: call("GET", f"/status?ticket={ticket}", token=tok)[1].get("status") == a.DONE)
assert app.queue.tasks[-1]["fmt"]["height"] == 480, "沒有用瀏覽器指定的畫質"
app.refresh_bridge_label(); r.update()
assert "執行中" in app.bridge_lbl.cget("text"), "設定頁沒有顯示連動狀態"

# 7. 剪貼簿裡有影片網址 → 出現提示列
r.clipboard_clear(); r.clipboard_append("https://www.xvideos.com/video123/test"); r.update()
app.check_clipboard(); r.update()
assert app.clip_bar.winfo_ismapped(), "剪貼簿提示列沒有出現"

# 8. 系統匣：關視窗 → 縮到系統匣 → 從系統匣打開 → 結束
assert a.pystray is not None, "pystray 沒有裝進去"
app.settings["close_action"] = "一律縮到系統匣"
app.on_close(); r.update()
assert app.tray is not None, "系統匣圖示沒有建立"
assert app.is_hidden(), "視窗沒有縮到系統匣"
app.q.put(("show_window", None))
wait(lambda: not app.is_hidden())
app.notify("測試通知")
app.quit_app()
print("GUI_OK")

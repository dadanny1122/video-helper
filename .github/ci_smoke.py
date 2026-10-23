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

# 3. 模擬讀取完成 → 依預設畫質選取
app.settings["default_quality"] = "720"
fmts = [{"id": h, "height": h, "size": h * 100000, "exact": False, "audio": True, "direct": False, "url": "", "headers": {}}
        for h in (1080, 720, 480)]
app.on_analyzed(("https://example.com/v", "測試影片", 634, fmts, 20.0))
r.update()
assert app.tree.selection() == ("1",), "預設畫質沒有選到 720p"
assert "10:34" in app.title_lbl.cget("text"), "沒有顯示影片長度"

# 4. 模擬下載完成 / 失敗 / 取消 → 紀錄與按鈕
app.current = {"url": "https://example.com/v", "title": "測試影片", "fmt": fmts[1], "size": 1, "out": d}
app.busy, app.cancel_event = True, object()
app.set_buttons(); r.update()
assert str(app.btn_cancel["state"]) == "normal", "下載中取消按鈕應該可以按"
app.on_done((dst, None)); r.update()
app.busy, app.cancel_event = True, object()
app.on_download_error("HTTP 404"); r.update()
assert str(app.btn_retry["state"]) == "normal", "失敗後應該可以重試"
app.busy, app.cancel_event = True, object()
app.on_cancelled(None); r.update()
assert [x["status"] for x in app.history.items] == ["取消", "失敗", "完成"], "下載紀錄不對"
assert len(app.htree.get_children()) == 3, "紀錄分頁沒有顯示"
r.destroy()
print("GUI_OK")

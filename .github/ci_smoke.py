"""GitHub Actions 打包前的檢查：在 Windows 上打開視窗、走過各分頁，並確認內建 ffmpeg 能用"""
import os, sys, subprocess, tempfile, traceback


def _report(exc_type, exc, tb):
    # 失敗原因印成 GitHub 的「註解」，在網頁和 API 都看得到
    lines = traceback.format_exception(exc_type, exc, tb)
    where = next((l.strip() for l in reversed(lines) if "ci_smoke.py" in l), "")
    msg = f"{exc_type.__name__}: {exc} | {where}".replace("\n", " ")[:900]
    print(f"::error title=Smoke test failed::{msg}", flush=True)
    sys.__excepthook__(exc_type, exc, tb)


sys.excepthook = _report
os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import tkinter as tk
import video_helper_app as a

# 1. 迷你 ffmpeg：把 TS 切片換成 MP4（跟 yt-dlp 的修正步驟用一樣的參數），再用 ffprobe 確認內容
assert a.FFMPEG and os.path.exists(a.FFMPEG), "找不到 ffmpeg"
assert "ffmpeg_bin" in a.FFMPEG, f"用到的不是迷你 ffmpeg：{a.FFMPEG}"
d = tempfile.mkdtemp()
src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures", "sample.ts")
dst = os.path.join(d, "dst.mp4")
subprocess.run([a.FFMPEG, "-y", "-loglevel", "error", "-i", src, "-map", "0", "-dn", "-ignore_unknown", "-c", "copy",
                "-f", "mp4", "-bsf:a", "aac_adtstoasc", "-movflags", "+faststart", dst], check=True)
assert os.path.getsize(dst) > 10000, "ffmpeg 轉檔失敗"
probe = os.path.join(os.path.dirname(a.FFMPEG), "ffprobe.exe")
info = subprocess.run([probe, "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", dst],
                      capture_output=True, text=True, check=True).stdout.split()
assert sorted(info) == ["audio", "video"], f"轉出來的 MP4 內容不對：{info}"
print("FFMPEG_OK", a.FFMPEG)

# 2. 視窗：4 個分頁、安裝精靈 3 步、進階設定
r = tk.Tk()
app = a.App(r)
from tkinter import ttk as _ttk
assert _ttk.Style().theme_use().startswith("sun-valley"), "Sun Valley 主題沒有載入：" + _ttk.Style().theme_use()
app.theme_var.set("深色"); app.change_theme(); app.theme_var.set("淺色"); app.change_theme()
assert a.C["bg"] == a.PALETTE["light"]["bg"]
_bg = _ttk.Style().lookup(".", "background")
assert str(_bg).lower() == a.C["bg"], f"主題底色沒套上（{_bg}）：會變成淺灰底黑字"
import time
_tab0 = app.nb.index("current")
app.show_home(); r.update()
assert app.home.visible()
app.home.leaving = (1, time.time() - 1); app.home._run(); r.update()
assert not app.home.visible() and app.nb.index("current") == 1, "點首頁卡片要進到那個分頁"
_k = app.home_kpis()
assert len(_k) == 4 and len(_k[0]["spark"]) == 7, _k
app.home.draw()
app.nav(2); r.update()
assert app.nb.index("current") == 2 and app.header.active == 2, "頂部選單要能切到設定"
app.nav(-1); r.update()
assert app.home.visible() and app.header.active == -1
app.nav(_tab0); r.update()
assert not app.home.visible()
assert [x[0] for x in _ttk.Style().layout("Hidden.TNotebook.Tab")] in ([], ["null"]), "主視窗的分頁籤要藏起來（換主題後也是）"
app.nb.select(_tab0); r.update()
print("::notice title=外觀::Sun Valley 主題、深淺色切換、首頁卡片 OK")
r.update()
assert len(app.nb.tabs()) == 4, "分頁數量不對"
assert len(app.step_marks) == 3, "安裝精靈步驟數量不對"
assert app.nb.select() == str(app.tab_setup), "第一次打開應該先到「安裝擋廣告」"
assert app.settings["setup_seen"], "看過安裝頁應該記下來"
# 進階設定：一開始藏著，按了才出現，關掉又藏起來
assert app.adv.state() == "withdrawn", "進階設定一開始應該藏起來"
app.show_advanced(); r.update()
assert app.adv.state() == "normal" and len(app.adv_nb.tabs()) == 7, "進階設定打不開"
app.adv.withdraw(); r.update()
assert not app.btn_retry.winfo_manager(), "沒有失敗的時候不該出現「重試」"
app.nb.select(app.tab_dl); r.update()

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
fail_counts = {}
def fake_download(url, fmt, out, on_progress=None, on_stage=None, cancel_event=None, name_template=None, stats=None, keep_partials=None):
    if url.startswith("fail"):
        raise RuntimeError("HTTP 404")
    if url.startswith("f429") or url.startswith("fdisk"):          # 第一次失敗、第二次成功（測自動重試）
        fail_counts[url] = fail_counts.get(url, 0) + 1
        if fail_counts[url] == 1:
            raise RuntimeError("HTTP Error 429: Too Many Requests; Retry-After: 1" if url.startswith("f429")
                               else "[Errno 28] No space left on device")
    if url.startswith("slow"):
        while not cancel_event.is_set():
            on_progress(1, 10, 1e6, 5); time.sleep(0.05)
        raise a.Cancelled()
    on_progress(10, 10, 1e6, 0)
    if stats is not None:
        stats.update(bytes=fmt["size"] * 2, seconds=2.0)     # 假裝實際是預估的 2 倍大
    return (dst, None)
media_calls = []
def fake_analyze_media(media, h):
    media_calls.append(media)
    return (media["title"], 634, [dict(f, media=media) for f in fmts], 20.0)
app.analyze_fn, app.download_fn, app.analyze_media_fn = fake_analyze, fake_download, fake_analyze_media

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
assert [t["status"] for t in (t_ok, t_fail, t_slow)] == [a.DONE, a.NEEDS_ACTION, a.CANCELLED], \
    "佇列狀態不對（404 應該是「需要處理」，不是傻傻重試）"
assert str(app.btn_retry["state"]) == "normal", "有失敗的應該可以重試"
assert app.btn_retry.winfo_manager() == "pack", "有失敗的應該出現「重試」按鈕"
assert [x["status"] for x in app.history.items] == ["取消", "失敗", "完成"], "下載紀錄不對"
assert len(app.qtree.get_children()) == 3 and len(app.htree.get_children()) == 3, "表格沒有顯示"

app.retry()
wait(lambda: t_fail["status"] == a.NEEDS_ACTION and app.cancel_event is None)

# 4. 一次加入多個網址
app.start_batch(["okA", "okB"])
wait(lambda: not app.batch_running and app.queue.count(a.WAITING, a.RUNNING) == 0)
assert app.queue.count(a.DONE) == 3, "批次加入的沒有下載完"

# 統計：上面完成了好幾部 → 統計分頁有資料、預估開始校正
app.refresh_stats(); r.update()
assert len(app.stree.get_children()) >= 1, "統計分頁沒有資料"
assert "倍" in app.stats_lbl.cget("text"), "統計沒有算出校正倍數"
app.on_analyzed(("https://www.pornhub.com/view_video.php?viewkey=again", "再讀一次", 634, fmts, 20.0)); r.update()
assert "校正" in app.speed_lbl.cget("text"), "預估沒有標示已校正"

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
def call(method, path, body=None, token=None, origin="chrome-extension://test", xvh=True):
    h = {"Origin": origin} if origin else {}
    if xvh:
        h["X-VH"] = "1"
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
assert call("GET", "/ping", origin=None, xvh=False)[0] == 403, "沒 Origin 也沒自訂標頭（網頁 no-cors）應該被拒絕"
assert call("GET", "/ping", origin=None)[0] == 200, "腳本（帶自訂標頭）應該可以"
st, dd = call("GET", "/ping", origin=None)
assert dd.get("api") == 1 and "enqueue" in dd.get("features", []) and "spankbang.com" in dd.get("sites", []), dd
assert call("POST", "/pair", {"code": "x"}, origin=None, xvh=False)[0] == 403
code = app.bridge.new_pair_code()
st, dd = call("POST", "/pair", {"code": code})
assert st == 200 and dd.get("token"), "配對失敗"
tok = dd["token"]
st, dd = call("POST", "/enqueue", {"url": "https://www.pornhub.com/view_video.php?viewkey=ci", "height": 480}, tok)
assert st == 200, "送影片失敗"
ticket = dd["ticket"]
# 不讓主畫面處理任何事，馬上問進度：一定要查得到（v1.9.0 這裡會回 404）
st, dd = call("GET", f"/status?ticket={ticket}", token=tok)
assert st == 200, f"剛送出就問進度應該查得到，卻回 {st}"
wait(lambda: call("GET", f"/status?ticket={ticket}", token=tok)[1].get("status") == a.DONE)
assert app.queue.tasks[-1]["fmt"]["height"] == 480, "沒有用瀏覽器指定的畫質"
# 瀏覽器一起送來影片位址 → 直接用，不開網頁（Pornhub 會對程式開網頁回 410）
page2 = "https://www.pornhub.com/view_video.php?viewkey=ci2"
st, dd = call("POST", "/enqueue", {"url": page2, "height": 720, "title": "CI 標題",
                                   "media": "https://ev-h.phncdn.com/hls/ci/master.m3u8"}, tok)
assert st == 200, "送影片位址失敗"
t2 = dd["ticket"]
wait(lambda: call("GET", f"/status?ticket={t2}", token=tok)[1].get("status") == a.DONE)
assert media_calls and media_calls[-1]["referer"] == page2, "沒有用瀏覽器給的影片位址"
assert app.queue.tasks[-1]["fmt"].get("media") and app.queue.tasks[-1]["title"] == "CI 標題", "下載沒有走影片位址"
st, _ = call("POST", "/enqueue", {"url": page2, "height": 720, "media": "https://evil.example/x.m3u8"}, tok)
assert st == 400, "不是網站影片伺服器的位址應該被拒絕"
app.refresh_bridge_label(); r.update()
assert "執行中" in app.bridge_lbl.cget("text"), "設定頁沒有顯示連動狀態"

# 6b. 廣告判斷：先查免費的公開清單（用假的本機清單，不連網）；沒金鑰就只用清單；有金鑰才問 AI
_bl = os.path.join(tempfile.mkdtemp(), "blocklist.txt")
with open(_bl, "w", encoding="utf-8") as f:
    f.write("# test\n" + "".join(f"0.0.0.0 filler{i}.example\n" for i in range(1200)) +
            "0.0.0.0 s.pemsrv.com\n0.0.0.0 tracker.example.org\n")
app.blocklist = a.BlockList(path=_bl, url="http://127.0.0.1:9/none")
assert app.blocklist.match("s.pemsrv.com") == "s.pemsrv.com" and app.blocklist.match("x.tracker.example.org")
assert not app.blocklist.match("pemsrv.com") and not app.blocklist.match("phncdn.com")
ai_calls = []
def fake_ai(key, site, domains):
    ai_calls.append((key, site, list(domains)))
    return {d: {"verdict": "ad" if "ads" in d else "cdn", "reason": "測試"} for d in domains}
app.ai_fn = fake_ai
app.settings["ai_key"] = ""
st, dd = call("POST", "/classify", {"site": "XVideos", "domains": ["pemsrv.com", "phncdn.com", "<script>", "x" * 300],
                                   "hosts": {"pemsrv.com": ["s.pemsrv.com", "evil.com"], "phncdn.com": ["ev.phncdn.com"]}}, tok)
assert st == 200, f"沒金鑰也要能用清單：{st} {dd}"
res = dd["results"]
assert set(res) == {"pemsrv.com", "phncdn.com"}, f"不合格的網域不該處理：{res}"
assert res["pemsrv.com"]["block"] and res["pemsrv.com"]["label"] == "已知廣告", f"清單裡的子網址要找得到：{res}"
assert not res["phncdn.com"]["block"] and res["phncdn.com"]["label"] == "不確定", f"清單沒有的應該是不確定：{res}"
assert not ai_calls, "沒金鑰不該問 AI"
app.settings["ai_key"] = "sk-test"
st, dd = call("POST", "/classify", {"site": "XVideos", "domains": ["pemsrv.com", "ads.example.com", "img.example-cdn.com"],
                                   "hosts": {"pemsrv.com": ["s.pemsrv.com"]}}, tok)
assert st == 200, f"判斷失敗：{st} {dd}"
res = dd["results"]
assert ai_calls[-1][2] == ["ads.example.com", "img.example-cdn.com"] and ai_calls[-1][1] == "XVideos", "清單查得到的不該再問 AI"
assert res["ads.example.com"]["block"] and not res["img.example-cdn.com"]["block"], "建議封鎖判斷錯"
n = len(ai_calls)
st, dd = call("POST", "/classify", {"site": "XVideos", "domains": ["ads.example.com"]}, tok)
assert st == 200 and len(ai_calls) == n, "問過 AI 的應該用記憶"
assert call("POST", "/classify", {"domains": ["ads.example.com"]})[0] == 401, "沒配對不能用"
app.settings["ai_key"] = ""
_bk = os.path.join(tempfile.mkdtemp(), "b.json"); a.export_backup(_bk, app.settings)
assert "ai_key" not in json.load(open(_bk, encoding="utf-8"))["settings"], "金鑰不該被匯出"
# 真的 ai_classify：接一個假的 AI 伺服器，看送出去的內容和解析
import http.server as _hs2, threading as _th2
_seen = {}
class _AiH(_hs2.BaseHTTPRequestHandler):
    def log_message(self, *x): pass
    def do_POST(self):
        _seen["key"] = self.headers.get("x-api-key")
        _seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        out = json.dumps({"content": [{"type": "text", "text": '```json\n[{"domain":"pemsrv.com","verdict":"ad","reason":"ExoClick 廣告"},'
                                       '{"domain":"evil.com","verdict":"ad"}]\n```'}]}).encode()
        self.send_response(200); self.send_header("Content-Length", str(len(out))); self.end_headers(); self.wfile.write(out)
_as = _hs2.ThreadingHTTPServer(("127.0.0.1", 0), _AiH)
_th2.Thread(target=_as.serve_forever, daemon=True).start()
_old = a.AI_API; a.AI_API = f"http://127.0.0.1:{_as.server_port}/v1/messages"
_r = a.ai_classify("sk-x", "XVideos", ["pemsrv.com", "xvideos-cdn.com"])
a.AI_API = _old; _as.shutdown()
assert _seen["key"] == "sk-x" and _seen["body"]["model"] == a.AI_MODEL, "送給 AI 的格式不對"
_txt = _seen["body"]["messages"][0]["content"]
assert "pemsrv.com" in _txt and "XVideos" in _txt and "viewkey" not in _txt, "送給 AI 的內容不對"
assert _r == {"pemsrv.com": {"verdict": "ad", "reason": "ExoClick 廣告"}}, f"AI 回答解析錯：{_r}"

# 7. 剪貼簿裡有影片網址 → 出現提示列
r.clipboard_clear(); r.clipboard_append("https://www.xvideos.com/video123/test"); r.update()
app.check_clipboard(); r.update()
assert app.clip_bar.winfo_manager() == "pack" and "xvideos.com" in app.clip_lbl.cget("text"), "剪貼簿提示列沒有放上去"
app.nb.select(app.tab_dl); r.update()
assert app.clip_bar.winfo_ismapped(), "切到「② 下載影片」時看不到剪貼簿提示列"
app.nb.select(app.nb.tabs()[-1]); r.update()                 # 換到最後一個分頁（安裝擋廣告）
assert app.clip_bar.winfo_ismapped(), "在別的分頁也要看得到剪貼簿提示列"
app.nb.select(app.tab_dl); r.update()
app.check_clipboard(); r.update()
assert app.clip_bar.winfo_ismapped(), "同一個網址不該讓提示列消失"

# 7b. MP4 分段下載（6 條線同時抓再接回去）：內容要跟原檔一模一樣，取消不留殘檔
import http.server as _hs, re as _re, threading as _th, hashlib as _hl, tempfile as _tf
_blob = os.urandom(12_000_000)
class _RangeH(_hs.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *x): pass
    def do_GET(self):
        m = _re.match(r"bytes=(\d+)-(\d*)", self.headers.get("Range") or "")
        s0 = int(m.group(1)) if m else 0
        e0 = int(m.group(2) or len(_blob) - 1) if m else len(_blob) - 1
        body = _blob[s0:e0 + 1]
        self.send_response(206 if m else 200)
        if m:
            self.send_header("Content-Range", f"bytes {s0}-{e0}/{len(_blob)}")
        self.send_header("Content-Length", str(len(body))); self.end_headers()
        try:
            for i in range(0, len(body), 65536):
                self.wfile.write(body[i:i + 65536]); time.sleep(0.03)
        except OSError:
            pass
_srv = _hs.ThreadingHTTPServer(("127.0.0.1", 0), _RangeH)
_th.Thread(target=_srv.serve_forever, daemon=True).start()
_dir = _tf.mkdtemp()
_dest = os.path.join(_dir, "分段 測試 [720p].mp4")
_res = a.parallel_download(f"http://127.0.0.1:{_srv.server_port}/v.mp4", {}, _dest)
assert _res and _hl.md5(open(_dest, "rb").read()).digest() == _hl.md5(_blob).digest(), "分段下載的檔案跟原檔不一樣"
assert sorted(os.listdir(_dir)) == ["分段 測試 [720p].mp4"], f"分段下載留下暫存檔：{os.listdir(_dir)}"
_ev = _th.Event(); _th.Timer(0.2, _ev.set).start()
_d2 = os.path.join(_dir, "取消.mp4"); _tch = set()
try:
    a.parallel_download(f"http://127.0.0.1:{_srv.server_port}/v.mp4", {}, _d2, cancel_event=_ev, touched=_tch)
    raise AssertionError("分段下載取消沒有停")
except a.Cancelled:
    a.cleanup_partials(_tch, include_complete=True)
assert sorted(os.listdir(_dir)) == ["分段 測試 [720p].mp4"], f"取消後留下暫存檔：{os.listdir(_dir)}"
_srv.shutdown()

# 7b. 🔒 安全分頁
app.show_advanced(); r.update()
# 🔒 安全分頁：配對清單、AI 紀錄、連線紀錄、元件指紋都畫得出來
_m = time.strftime("%Y-%m")
_before = ((app.settings["ai_usage"] or {}).get(_m, {}).get("classify") or {}).get("calls", 0)
app.note_ai_usage("classify", {"input_tokens": 120, "output_tokens": 30})
app.refresh_security(); r.update()
wait(lambda: "yt-dlp" in app.sec_comp.cget("text"))
print("::notice title=安全分頁 元件::" + app.sec_comp.cget("text").replace("\n", " | ")[:600])
assert app.settings["ai_usage"][_m]["classify"]["calls"] == _before + 1, "AI 使用紀錄沒記到"
app.adv.withdraw(); r.update()

# 7c. v1.20 暫停／繼續、速度上限、重複偵測的介面
app.toggle_pause(); r.update()
assert app.queue_paused and app.btn_pause.cget("text") == "▶ 繼續", "暫停按鈕沒反應"
app.toggle_pause(); r.update()
assert not app.queue_paused and app.btn_pause.cget("text") == "⏸ 暫停", "繼續按鈕沒反應"
app.rate_var.set("25"); app.apply_rate_limit()
assert abs(a.THROTTLE.bps - 25e6 / 8) < 1 and app.settings["rate_limit_mbps"] == 25, "速度上限沒生效"
app.rate_var.set("不限制"); app.apply_rate_limit()
assert a.THROTTLE.bps == 0
assert a.video_key("https://www.pornhub.com/view_video.php?viewkey=x1&t=3") == a.video_key("https://cn.pornhub.com/view_video.php?viewkey=x1")
print("::notice title=v1.20::暫停／繼續按鈕、速度上限、重複偵測 OK")

# 7d. v1.21 AI 字幕視窗：單部、多部都打得開（格式、雙語選項都在）
_v1 = os.path.join(d, "sub_a.mp4"); _v2 = os.path.join(d, "sub_b.mp4")
for _p in (_v1, _v2):
    open(_p, "wb").write(b"x")
app.subtitle_dialog(video=_v1); r.update()
assert app.sub_win.winfo_exists(), "AI 字幕視窗打不開"
app.sub_win.destroy(); r.update()
app.history.add(url="https://example.com/a", title="a", file=_v1, status=a.DONE, height=720)
app.history.add(url="https://example.com/b", title="b", file=_v2, status=a.DONE, height=720)
app.refresh_history(); r.update()
app.htree.selection_set(("0", "1")); r.update()
app.subtitle_dialog(); r.update()
_texts = [w.cget("text") for w in app.sub_win.winfo_children()[0].winfo_children() if isinstance(w, a.ttk.Label)]
assert any("共 2 部" in t for t in _texts), _texts
app.sub_win.destroy(); r.update()
open(_v1[:-4] + ".srt", "w").write("1")
assert a.has_subtitles(_v1) and not a.has_subtitles(_v2)
print("::notice title=v1.21::AI 字幕視窗（單部、多選 2 部）、有沒有字幕的判斷 OK")

# 7e. 自動化核心：429 自動重試（照 Retry-After）、磁碟滿 → 佇列暫停
t429 = app.enqueue(mk("f429-auto"))
wait(lambda: t429["status"] == a.RETRY_WAIT)
assert t429["tries"] == 1 and 0 < t429["retry_at"] - time.time() < 10, "429 應該照 Retry-After 排 1～6 秒後重試"
wait(lambda: t429["status"] == a.DONE, sec=20)                     # 時間到自動重試 → 成功
assert t429["tries"] == 0, "成功後重試次數應該歸零"
t_disk = app.enqueue(mk("fdisk-full"))
wait(lambda: app.queue_paused and t_disk["status"] == a.WAITING)   # 磁碟滿：佇列暫停、任務留著
app.toggle_pause()                                                  # 假裝清出空間 → 繼續
wait(lambda: t_disk["status"] == a.DONE, sec=20)
import json as _json
_dec = [_json.loads(x) for x in open(os.path.join(a.data_dir(), "decisions.jsonl"), encoding="utf-8").read().splitlines()]
assert [x["action"] for x in _dec].count("retry") >= 1 and any(x["action"] == "pause_queue" for x in _dec), _dec[-4:]
print("::notice title=自動化核心::404→需要處理、429→自動重試成功、磁碟滿→暫停佇列、決策日誌 OK")

# 7f. 智慧下載（v1.24.0）：一個網站 429 → 只冷卻那個網站，其他網站照常；決策紀錄有任務結果
t_c = app.enqueue(mk("f429-cool"))
wait(lambda: t_c["status"] == a.RETRY_WAIT)
assert a.HOSTS.cooling("?") > 0, "429 應該讓那個網站冷卻"
t_same = app.enqueue(mk("same-site-later"))
r.update()
assert t_same["status"] == a.WAITING, "冷卻中的網站，排隊的要先等"
t_other = app.enqueue(mk("https://www.xvideos.com/video.ci_other/x"))
wait(lambda: t_other["status"] == a.DONE, sec=5)
assert t_c["status"] == a.RETRY_WAIT, "其他網站應該在冷卻期間就下載完"
wait(lambda: t_c["status"] == a.DONE and t_same["status"] == a.DONE, sec=20)
assert a.HOSTS.cooling("?") == 0
_dec = [_json.loads(x) for x in open(os.path.join(a.data_dir(), "decisions.jsonl"), encoding="utf-8").read().splitlines()]
assert any(x["action"] == "host_cooldown" for x in _dec) and any(x["action"] == "task_result" and x.get("mode") == "fixed" for x in _dec)
assert app.settings["smart_segments"] == "observe" and "固定 6 段" in a.smart_report()
app.settings["smart_segments"] = "auto"
app.settings["seg_state"]["?"] = {"level": 1, "stable": 0, "hold": 0}      # 假裝之前學到「2 段」
_seen = {}
_orig = app.download_fn
app.download_fn = lambda *x, **k: (_seen.update(parts=k["stats"]["parts"]), _orig(*x, **k))[1]
t_auto = app.enqueue(mk("auto-parts"))
wait(lambda: t_auto["status"] == a.DONE)
app.download_fn = _orig
assert _seen["parts"] == 2, _seen
app.settings["smart_segments"] = "observe"
print("::notice title=智慧下載::單一網站 429 只冷卻那個網站、自動模式照學到的段數、決策紀錄 OK")

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

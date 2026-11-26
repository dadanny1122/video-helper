"""v1.48.0 五個新功能接到電腦版之後的測試（假的網頁視窗；Linux 用 xvfb-run）：python .github/extras_app_test.py"""
# ruff: noqa: E501
import json
import os
import sys
import tempfile
import threading
import time
from datetime import datetime

os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
os.environ["VH_NEXUS_ROOT"] = os.path.join(os.environ["VIDEO_HELPER_HOME"], "nexus")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import tkinter as tk  # noqa: E402

import vh_extras as VX  # noqa: E402
import video_helper_app as a  # noqa: E402
import webui  # noqa: E402

fails = 0


def ok(cond, msg):
    global fails
    print(("✅ " if cond else "❌ ") + msg, flush=True)
    if not cond:
        fails += 1
        print(f"::error title=新功能測試::{msg}", flush=True)


class FakeWindow:
    def show(self): pass
    def hide(self): pass
    def restore(self): pass
    def destroy(self): pass


host = webui.Host()
host.window = FakeWindow()
root = tk.Tk()
app = a.App(root, web=host)


def drain(sec=0.3):
    end = time.time() + sec
    while time.time() < end:
        root.update()
        time.sleep(0.02)


def call(name, arg=None):
    box = {}
    th = threading.Thread(target=lambda: box.update(r=host.act(name, arg)))
    th.start()
    while th.is_alive():
        root.update()
        time.sleep(0.01)
    return box["r"]


drain()
app.analyze_fn = lambda url: (_ for _ in ()).throw(RuntimeError("測試：不連網"))
ok(not host.state.get("locked") and host.state["settings"]["lock_on"] is False, "一開始沒有密碼鎖")

# ---- 1. 密碼鎖 ----
for bad in ({"pin": 1234}, {"pin": "1" * 13}, "1234"):
    try:
        webui.check_action("lock_set", bad)
        ok(False, f"lock_set 應該拒絕 {bad!r}")
    except ValueError:
        pass
ok(call("lock_set", {"pin": "12a4", "old": ""})["ok"] is False, "密碼要數字")
ok(call("lock_set", {"pin": "2468", "old": ""})["ok"], "設定密碼")
saved = json.load(open(os.path.join(os.environ["VIDEO_HELPER_HOME"], "settings.json"), encoding="utf-8"))
ok("2468" not in json.dumps(saved) and saved["lock_pin"]["hash"], "設定檔裡沒有原本的密碼")
ok(call("lock_set", {"pin": "1357", "old": "0000"})["ok"] is False, "改密碼要對的舊密碼")
f = os.path.join(os.environ["VIDEO_HELPER_HOME"], "secret.mp4")
open(f, "wb").write(b"x")
app.history.add(title="祕密標題", url="https://www.xvideos.com/video1/a", height=720, status=a.DONE, file=f, size=1)
app.add_notice("✅", "下載完成：祕密標題")
app.web_publish()
ok(host.history({})["total"] == 1, "解鎖時看得到紀錄")
ok(call("lock")["ok"] and app.lock.locked and host.locked, "按 🔒 鎖住")
blob = json.dumps(host.state, ensure_ascii=False)
ok(host.state.get("locked") and "祕密" not in blob and not host.state["tasks"] and not host.state["notices"], "鎖住時狀態裡沒有標題、紀錄、通知")
ok(host.history({})["total"] == 0 and host.thumbs(["x"]) == {} and webui.Api(host).later() == [] and webui.Api(host).nexus() is None, "鎖住時紀錄、縮圖、稍後清單、Nexus 都拿不到")
ok(call("toggle_pause")["msg"] == "🔒 已上鎖" and not app.queue_paused, "鎖住時其他動作都不收")
app.lock.locked = True
box, ev = {}, threading.Event()
app.on_web_action(("delete_hist", ["x"], box, ev))
ok(box["reply"]["ok"] is False, "電腦版那邊也再擋一次")
ok(call("unlock", "1111")["ok"] is False and app.lock.locked, "錯的密碼解不開")
ok(call("unlock", "2468")["ok"] and not app.lock.locked, "對的密碼解鎖")
drain(0.6)
ok("祕密標題" in json.dumps(host.state, ensure_ascii=False), "解鎖後看得到")
ok(call("lock_clear", "0000")["ok"] is False, "關密碼鎖要對的密碼")
ok(call("lock_clear", "2468")["ok"] and not a.VX.has_pin(app.settings["lock_pin"]), "關閉密碼鎖")

# ---- 2. 稍後清單（從 Bridge 進來）----
ok(app.bridge.on_later is not None, "Bridge 接上 ⭐ 稍後下載")
app.bridge.on_later("https://www.xvideos.com/video9/b", "<b>稍後A</b>")
app.bridge.on_later("https://www.pornhub.com/view_video.php?viewkey=ph1", "稍後B")
ok(not app.bridge.on_later("https://www.xvideos.com/video9/b?x=1", "重複"), "同一部不重複")
drain(0.6)
rows = webui.Api(host).later()
ok(len(rows) == 2 and rows[0]["browser"] is True and rows[1]["browser"] is False, "清單有 2 部、Pornhub 標「要用瀏覽器」")
opened = []
a.webbrowser.open = lambda u: opened.append(u)
r = call("later_all")
ok(r["ok"] and opened == ["https://www.pornhub.com/view_video.php?viewkey=ph1#vh-later"], "全部下載：Pornhub 用瀏覽器開 #vh-later")
drain(1.0)
app.enqueue({"url": "https://www.pornhub.com/view_video.php?viewkey=ph1", "title": "B", "fmt": {"height": 720, "size": 1},
             "size": 1, "out": app.settings.download_dir})
ok(all("ph1" not in r["url"] for r in app.later.items), "加入佇列後從清單拿掉")
app.queue.tasks.clear()
app.cancel_event = None

# ---- 3. 定時下載 ----
app.download_fn = lambda *x, **k: (_ for _ in ()).throw(a.Cancelled())
now_h = datetime.now().hour
call("set", {"key": "sched_mode", "value": "window"})
call("set", {"key": "sched_start", "value": (now_h + 2) % 24})
call("set", {"key": "sched_end", "value": (now_h + 3) % 24})
app.enqueue({"url": "https://www.xvideos.com/video1/zz", "title": "Z", "fmt": {"height": 360, "size": 1}, "size": 1,
             "out": app.settings.download_dir})
ok(app.queue.running() is None and "定時下載" in app.status.cget("text"), "時段外不開始下載")
drain(0.6)
ok("定時下載" in (host.state.get("sched") or ""), "網頁上方顯示定時下載")
ok(call("sched_now")["ok"], "▶ 現在就下載")
drain(0.5)
ok(not app.queue.count(a.WAITING), "略過一次：開始下載了")
drain(0.5)
ok(not app.sched_override, "佇列空了以後又照時段")
call("set", {"key": "sched_mode", "value": "fast_night"})
call("set", {"key": "sched_day_mbps", "value": 2})
ok(a.THROTTLE.bps == 2e6 / 8, "時段外限速 2 Mbps")
ok(call("set", {"key": "sched_day_mbps", "value": 3})["ok"] is False and call("set", {"key": "sched_start", "value": 24})["ok"] is False, "不合法的值不收")
call("set", {"key": "sched_mode", "value": "off"})
ok(a.THROTTLE.bps == 0, "關掉就不限速")
app.queue.tasks.clear()

# ---- 4. 自動整理 ----
call("set", {"key": "organize", "value": "site_month"})
t = app.enqueue({"url": "https://www.xvideos.com/video1/org", "title": "O", "fmt": {"height": 360, "size": 1}, "size": 1,
                 "out": app.settings.download_dir})
ok(t["out"] == os.path.join(app.settings.download_dir, "XVideos", datetime.now().strftime("%Y-%m")) and os.path.isdir(t["out"]), "依網站＋月份分資料夾")
other = tempfile.mkdtemp()
t = app.enqueue({"url": "https://www.xvideos.com/video1/org2", "title": "O2", "fmt": {"height": 360, "size": 1}, "size": 1, "out": other})
ok(t["out"] == other, "自己選的資料夾不改")
call("set", {"key": "organize", "value": "off"})
drain(0.5)
app.queue.tasks.clear()
f1, f2 = os.path.join(other, "a480.mp4"), os.path.join(other, "a1080.mp4")
for p in (f1, f2):
    open(p, "wb").write(b"x")
app.history.add(title="重複", url="https://www.xvideos.com/video77/x", height=480, status=a.DONE, file=f1, size=1)
app.history.add(title="重複", url="https://www.xvideos.com/video77/x", height=1080, status=a.DONE, file=f2, size=1)
r = call("dup_scan")
g = r["groups"][0]
ok(len(r["groups"]) == 1 and g[0]["keep"] and g[0]["quality"].startswith("1080") and not g[1]["keep"], "找重複：保留畫質最高的")
if a.VX.recycle_supported():
    r = call("dup_delete", [x["key"] for x in g])
    ok(r["ok"] and os.path.exists(f2) and not os.path.exists(f1), "整組都勾也留一個；丟到資源回收筒")
else:
    ok(call("dup_delete", [g[1]["key"]])["ok"] is False and os.path.exists(f1), "不是 Windows 不能刪")

# ---- 5. 通知手機 ----
ok(call("notify_set", "https://evil.com/api/webhooks/1/abc")["ok"] is False, "壞的 Webhook 不收")
hook = "https://discord.com/api/webhooks/123456789012345678/" + "b" * 68
ok(call("notify_set", hook)["ok"] and app.settings["notify_mode"] == "all_done", "儲存 Webhook、自動打開通知")
ok(hook not in json.dumps(host.state) and host.state["settings"]["notify_has"], "網頁拿不到 Webhook 網址")
sent = []
a.VX.send_webhook = lambda url, text, **k: sent.append((url, text))
app.batch = VX.Batch()
app.batch.add(True, 1_500_000_000)
app.batch.add(True, 600_000_000)
app.batch_check()
drain(0.3)
ok(sent and sent[-1] == (hook, "✅ Video Helper：下載完成 2 部，共 2.1 GB"), "佇列做完：通知只有數字")
call("notify_set", "-")
ok(not app.settings["notify_webhook"], "清除 Webhook")

app.quitting = True
try:
    app.quit_app()
except SystemExit:
    pass
print("失敗" if fails else "全部通過", fails)
sys.exit(1 if fails else 0)

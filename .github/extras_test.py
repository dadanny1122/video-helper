"""v1.48.0 五個新功能的測試（vh_extras.py，不用視窗）：python3 -I .github/extras_test.py"""
# ruff: noqa: E501
import os
import sys
import tempfile
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vh_extras as VX  # noqa: E402

N = 0


def ok(cond, msg):
    global N
    if not cond:
        raise SystemExit("❌ " + msg)
    N += 1


# ---- 1. 密碼鎖 ----
rec = VX.make_pin("1234", iterations=1000)
ok("1234" not in str(rec) and len(rec["salt"]) == 32, "不存原本的密碼")
ok(VX.check_pin(rec, "1234") and not VX.check_pin(rec, "1235"), "對的過、錯的不過")
ok(not VX.check_pin(rec, "12a4") and not VX.check_pin(None, "1234") and not VX.check_pin({"x": 1}, "1234"), "壞資料不過")
for bad in ("123", "1234567890123", "abcd", "", None):
    try:
        VX.make_pin(bad)
        ok(False, f"{bad!r} 不能當密碼")
    except ValueError:
        ok(True, "")
ok(VX.make_pin("1234", 1000)["salt"] != rec["salt"], "每次 salt 不一樣")
t = [1000.0]
lk = VX.Lock(clock=lambda: t[0])
lk.locked = True
for i in range(4):
    ok(not lk.try_unlock(rec, "0000")[0] and lk.wait_left() == 0, f"前 4 次錯不用等（{i}）")
r = lk.try_unlock(rec, "0000")
ok(not r[0] and lk.wait_left() == 30, "第 5 次錯等 30 秒")
ok(not lk.try_unlock(rec, "1234")[0] and lk.locked, "等的時候對的也不收")
t[0] += 31
ok(not lk.try_unlock(rec, "0000")[0] and lk.wait_left() == 60, "之後加倍")
t[0] += 10 ** 6
for _ in range(20):
    lk.try_unlock(rec, "0000")
    t[0] += 10 ** 6
ok(lk.until - (t[0] - 10 ** 6) <= VX.WAIT_MAX, "最多 15 分鐘")
ok(lk.try_unlock(rec, "1234")[0] and not lk.locked and lk.wrong == 0, "解鎖後歸零")

# ---- 2. 稍後清單 ----
d = tempfile.mkdtemp()
key = lambda u: u.split("?")[0].rstrip("/")  # noqa: E731
L = VX.LaterList(os.path.join(d, "later.json"), key)
ok(L.add("https://www.xvideos.com/video1/a", "A", "XVideos"), "新加入")
ok(not L.add("https://www.xvideos.com/video1/a?x=1", "A2"), "同一部不重複")
L.add("https://www.pornhub.com/view_video.php", "B")
ok(len(L.items) == 2 and L.items[0]["title"] == "B", "新的在前")
L2 = VX.LaterList(os.path.join(d, "later.json"), key)
ok(len(L2.items) == 2, "存檔讀得回來")
ok(L.remove_key("https://www.xvideos.com/video1/a/") == 1 and len(L.items) == 1, "加入佇列後拿掉")
for i in range(VX.LATER_MAX + 5):
    L.add(f"https://x.com/{i}")
ok(len(L.items) == VX.LATER_MAX, "最多 300 部")
ok(VX.later_open_url("https://a.com/v#x") == "https://a.com/v#vh-later", "#vh-later")
with open(os.path.join(d, "bad.json"), "w") as f:
    f.write("[1, {\"url\": 3}, {\"url\": \"https://ok\"}]")
ok(len(VX.LaterList(os.path.join(d, "bad.json"), key).items) == 1, "壞資料跳過")

# ---- 3. 定時下載 ----
ok(VX.in_window(2, 1, 7) and not VX.in_window(7, 1, 7) and not VX.in_window(0, 1, 7), "一般時段")
ok(VX.in_window(23, 23, 7) and VX.in_window(3, 23, 7) and not VX.in_window(12, 23, 7), "跨半夜")
ok(VX.in_window(12, 5, 5), "開始＝結束＝整天")
at = lambda h: datetime(2026, 11, 26, h, 30)  # noqa: E731
st = {"sched_mode": "window", "sched_start": 1, "sched_end": 7, "rate_limit_mbps": 0}
ok(VX.sched_state(st, at(3))[0] and not VX.sched_state(st, at(12))[0], "window：時段外不下載")
ok("1:00" in VX.sched_state(st, at(12))[2], "說明寫時段")
st = {"sched_mode": "fast_night", "sched_start": 1, "sched_end": 7, "sched_day_mbps": 5, "rate_limit_mbps": 0}
ok(VX.sched_state(st, at(12)) [:2] == (True, 5.0), "fast_night：白天限速")
ok(VX.sched_state(st, at(3))[:2] == (True, 0.0), "fast_night：晚上全速")
st["rate_limit_mbps"] = 3
ok(VX.sched_state(st, at(12))[1] == 3 and VX.sched_state(st, at(3))[1] == 3, "跟速度上限取小的")
ok(VX.sched_state({"rate_limit_mbps": 8}, at(12)) == (True, 8.0, ""), "關閉＝照原本")

# ---- 4. 整理、找重複 ----
now = datetime(2026, 11, 26)
ok(VX.organize_dir("D", "off", "Pornhub") == "D", "off 不變")
ok(VX.organize_dir("D", "site", "Pornhub") == os.path.join("D", "Pornhub"), "依網站")
ok(VX.organize_dir("D", "site_month", "XVideos", now) == os.path.join("D", "XVideos", "2026-11"), "網站＋月份")
ok(VX.safe_folder('a/b\\c:..') == "a_b_c_" and VX.safe_folder("..") == "其他" and VX.safe_folder("C:") == "C_", "資料夾名稱安全")
files = {f"f{i}" for i in range(5)}
items = [
    {"status": "完成", "url": "u1", "file": "f1", "height": 480},
    {"status": "完成", "url": "u1?x", "file": "f2", "height": 1080},
    {"status": "完成", "url": "u1", "file": "f2", "height": 1080},        # 同一個檔案的重複紀錄
    {"status": "失敗", "url": "u1", "file": "f3"},
    {"status": "完成", "url": "u2", "file": "f4", "height": 720},
    {"status": "完成", "url": "u2", "file": "gone", "height": 360},        # 檔案不在了
]
g = VX.find_duplicates(items, key, exists=lambda p: os.path.basename(p) in files)
ok(len(g) == 1 and [r["file"] for _i, r in g[0]] == ["f2", "f1"], "畫質高的在前、同檔只算一次、失敗和不在的不算")
ok(not VX.recycle_supported() or sys.platform == "win32", "資源回收筒只在 Windows")
if not VX.recycle_supported():
    try:
        VX.to_recycle_bin(__file__)
        ok(False, "不是 Windows 要拒絕")
    except OSError:
        ok(os.path.exists(__file__), "不是 Windows：不刪")
else:
    p = os.path.join(d, "dup.txt")
    open(p, "w").write("x")
    ok(VX.to_recycle_bin(p) and not os.path.exists(p), "丟到資源回收筒")

# ---- 5. 通知手機 ----
good = "https://discord.com/api/webhooks/123456789012345678/" + "a" * 68
ok(VX.webhook_ok(good) and VX.webhook_ok(good.replace("discord.com", "canary.discord.com")), "Webhook 網址")
for bad in ("http://discord.com/api/webhooks/1234567/" + "a" * 30, good.replace("discord.com", "evil.com"),
            good + "?wait=1", good.replace("https://", "https://user@"), "https://discord.com.evil.com/api/webhooks/123456/" + "a" * 30, None):
    ok(not VX.webhook_ok(bad), f"不收 {str(bad)[:50]}")
txt = VX.notify_text(3, 1, 2_100_000_000)
ok(txt == "✅ Video Helper：下載完成 3 部，共 2.1 GB（失敗 1 部）", txt)
ok("失敗" in VX.notify_text(0, 2, 0), "只有失敗")
b = VX.Batch()
ok(not b, "空的")
b.add(True, 100)
b.add(False)
ok(bool(b) and b.take() == (1, 1, 100) and not b, "這一批")
sent = {}


class Resp:
    status = 204

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def opener(req, timeout):
    import json
    sent["body"] = json.loads(req.data)
    sent["url"] = req.full_url
    return Resp()


VX.send_webhook(good, "hi @everyone", opener=opener)
ok(sent["body"]["allowed_mentions"] == {"parse": []} and sent["url"] == good, "不會 @ 人")
try:
    VX.send_webhook("https://evil.com/x", "hi", opener=opener)
    ok(False, "壞網址不送")
except ValueError:
    ok(True, "")
print(f"✅ 五個新功能：{N} 項都通過")

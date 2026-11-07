"""網路流量測試（在 GitHub 的 Windows 機器上跑）：
把電腦版「所有」對外連線記下來（在最底層的 socket 攔），然後實際走一遍會連網的功能：
打開視窗、檢查更新、下載廣告清單、AI 字幕的程式和模型、AI 判斷廣告和翻譯（用假金鑰）。
連到 SECURITY.md 第 1 節清單以外的地方 → 測試失敗。
"""
import os
import socket
import sys
import tempfile
import time

os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SEEN = {}
_gai, _cc = socket.getaddrinfo, socket.create_connection


def _note(host, how):
    h = str(host).lower().rstrip(".")
    SEEN.setdefault(h, set()).add(how)


def getaddrinfo(host, *a, **k):
    _note(host, "dns")
    return _gai(host, *a, **k)


def create_connection(addr, *a, **k):
    _note(addr[0], "connect")
    return _cc(addr, *a, **k)


socket.getaddrinfo, socket.create_connection = getaddrinfo, create_connection

# SECURITY.md 第 1 節寫的地方（後面的字＝網域結尾）
ALLOWED = ("github.com", "githubusercontent.com",        # 檢查更新、廣告清單、whisper.cpp
           "huggingface.co", "hf.co", "hf-mirror.com",   # AI 模型（Hugging Face 會轉到自己的下載伺服器）
           "anthropic.com",                              # AI（只有填金鑰才會）
           "localhost", "127.0.0.1")                     # 電腦版自己的連線服務


def allowed(h):
    return any(h == d or h.endswith("." + d) for d in ALLOWED)


import tkinter as tk  # noqa: E402
import video_helper_app as a  # noqa: E402

steps = []


def step(name, fn):
    try:
        fn()
        steps.append(f"✅ {name}")
    except Exception as e:
        steps.append(f"・{name}（{type(e).__name__}：{str(e)[:60]}）")   # 失敗沒關係，這裡只看「連到哪」


# 1. 打開視窗，讓它自己跑 15 秒（啟動時會檢查更新、開連線服務…）
r = tk.Tk()
app = a.App(r)
t0 = time.time()
while time.time() - t0 < 15:
    r.update()
    time.sleep(0.05)
steps.append("✅ 打開視窗 15 秒")

# 2. 各個會連網的功能
step("檢查更新", lambda: a.check_update(app.q, manual=True))
step("下載廣告清單", lambda: a.BlockList().download())
step("下載 AI 字幕程式", lambda: a.install_whisper())
step("下載 AI 模型（tiny）並核對指紋", lambda: a.ensure_model("tiny"))
step("AI 判斷廣告（假金鑰）", lambda: a.ai_classify("sk-ant-fake", "Pornhub", ["doubleclick.net"]))
step("AI 翻譯字幕（假金鑰）", lambda: a.ai_translate("sk-ant-fake", ["hello"], "en"))
r.destroy()

bad = sorted(h for h in SEEN if not allowed(h))
print("::notice title=網路流量測試 功能::" + "　".join(steps))
print("::notice title=網路流量測試 連到的地方::" + "、".join(f"{h}" for h in sorted(SEEN)))
if bad:
    print("::error title=網路流量測試::連到清單以外的地方：" + "、".join(bad))
    sys.exit(1)
print("::notice title=網路流量測試::全部都在 SECURITY.md 的清單裡")

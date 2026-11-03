"""
即時介面（docs/dash/index.html）截圖 → Discord #看板：只有一則訊息，每次修改它換成最新的圖。
網頁本身從 raw.githubusercontent.com 讀最新資料，所以直接開本機的 html 就是最新狀態。

用法：python dash_snapshot.py 狀態檔.json
環境變數：DISCORD_DASH_WEBHOOK（沒設定就只截圖不發）
"""
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")
PAGE = "file://" + os.path.abspath(os.path.join(ROOT, "docs", "dash", "index.html"))
URL = "https://dadanny1122.github.io/video-helper/dash/"
IMG = "/tmp/dash.png"


def shoot():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        page = b.new_page(viewport={"width": 1200, "height": 900}, device_scale_factor=1,
                          color_scheme="dark")      # Discord 多半是深色，截深色版比較好看
        page.goto(PAGE)
        page.wait_for_timeout(6000)                 # 等四格資料都讀完
        page.screenshot(path=IMG, full_page=True)
        b.close()


def send(hook, state):
    base = hook.split("?")[0]
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M")
    embed = {"title": "🤖 即時介面", "url": URL, "color": 0xF57C00,
             "description": f"🕒 截圖時間：{now}（自動更新，約每 10 分鐘）\n"
                            f"👉 [打開網頁版（可以點連結、每分鐘刷新）]({URL})",
             "image": {"url": "attachment://dash.png"}}
    payload = {"username": "Video Helper 即時介面", "embeds": [embed],
               "attachments": [{"id": 0, "filename": "dash.png"}]}
    boundary = "----vhdash" + str(int(time.time()))
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n").encode() + json.dumps(payload).encode() + \
           (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"dash.png\"\r\n"
            f"Content-Type: image/png\r\n\r\n").encode() + open(IMG, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    headers = {"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "VideoHelper-CI"}
    mid = state.get("message_id")
    if mid:                                        # 改同一則訊息（頻道永遠只有一張最新的圖）
        try:
            urllib.request.urlopen(urllib.request.Request(f"{base}/messages/{mid}", data=body, method="PATCH",
                                                          headers=headers), timeout=30).read()
            return
        except Exception as e:
            print(f"改不到舊訊息（{e}），重新貼一則")
    r = urllib.request.urlopen(urllib.request.Request(f"{base}?wait=true", data=body, method="POST",
                                                      headers=headers), timeout=30)
    state["message_id"] = json.loads(r.read())["id"]


def reply_command(app_id, token):
    """回覆 Discord 的 /快照 指令：把「截圖中…」那則訊息直接換成圖片"""
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%m/%d %H:%M:%S")
    payload = {"content": "", "embeds": [{"title": "🤖 即時介面（/快照）", "url": URL, "color": 0xF57C00,
                                          "description": f"🕒 {now}｜👉 [打開網頁版]({URL})",
                                          "image": {"url": "attachment://dash.png"}}],
               "attachments": [{"id": 0, "filename": "dash.png"}]}
    boundary = "----vhsnap" + str(int(time.time()))
    body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"payload_json\"\r\n"
            f"Content-Type: application/json\r\n\r\n").encode() + json.dumps(payload).encode() + \
           (f"\r\n--{boundary}\r\nContent-Disposition: form-data; name=\"files[0]\"; filename=\"dash.png\"\r\n"
            f"Content-Type: image/png\r\n\r\n").encode() + open(IMG, "rb").read() + f"\r\n--{boundary}--\r\n".encode()
    urllib.request.urlopen(urllib.request.Request(
        f"https://discord.com/api/v10/webhooks/{app_id}/{token}/messages/@original", data=body, method="PATCH",
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}", "User-Agent": "VideoHelper-CI"}),
        timeout=30).read()
    print("::notice title=即時介面::已回覆 /快照 指令")


def main(path):
    try:
        state = json.load(open(path, encoding="utf-8"))
    except Exception:
        state = {}
    shoot()
    tok, app = os.environ.get("INTERACTION", "").strip(), os.environ.get("APP_ID", "").strip()
    if tok and app:                                 # 有人在 Discord 打 /快照 → 先回覆他
        try:
            reply_command(app, tok)
        except Exception as e:
            print(f"::warning title=即時介面::回覆 /快照 失敗：{e}")
    hook = os.environ.get("DISCORD_DASH_WEBHOOK", "").strip()
    if not hook:
        print("沒有設定 DISCORD_DASH_WEBHOOK，只截圖")
        return
    send(hook, state)
    json.dump(state, open(path, "w", encoding="utf-8"))
    print("::notice title=即時介面::已更新")


if __name__ == "__main__":
    main(sys.argv[1])

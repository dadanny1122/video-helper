"""
把更新內容貼到 Discord 頻道（用 Webhook）。
用法：python discord_notify.py "標題" 內容檔案.md "連結網址"
Webhook 網址放在 GitHub 的 Secret：DISCORD_WEBHOOK；沒設定就什麼都不做（不會讓建置失敗）。
"""
import json
import os
import sys
import urllib.request

hook = os.environ.get("DISCORD_WEBHOOK", "").strip()
if not hook:
    print("沒有設定 DISCORD_WEBHOOK，略過")
    sys.exit(0)

title, body_path, link = sys.argv[1], sys.argv[2], sys.argv[3]
body = open(body_path, encoding="utf-8").read().strip() or "（沒有寫更新內容）"
if len(body) > 3800:                       # Discord 一則說明最多 4096 字
    body = body[:3800].rstrip() + "\n…（完整內容請看連結）"

payload = {
    "username": "Video Helper",
    "embeds": [{"title": title[:250], "url": link, "description": body, "color": 0xC62828}],
}
req = urllib.request.Request(hook, data=json.dumps(payload).encode("utf-8"), method="POST",
                             headers={"Content-Type": "application/json", "User-Agent": "VideoHelper-CI"})
try:
    with urllib.request.urlopen(req, timeout=20) as r:
        print("已貼到 Discord：", r.status)
except Exception as e:                     # 通知失敗不影響發布
    print(f"::warning title=Discord 通知失敗::{e}")

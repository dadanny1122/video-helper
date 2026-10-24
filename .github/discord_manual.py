"""
把說明書（docs/manual/index.html）轉成 Discord 訊息，貼到「使用手冊」頻道。
每一章一則訊息；之後說明書改了，就「修改原本那幾則」，頻道裡永遠只有一份最新的。

用法：
  python discord_manual.py preview            只印出轉好的內容（不貼）
  python discord_manual.py post ids.json      貼到 Discord（Webhook 放在環境變數 DISCORD_MANUAL_WEBHOOK）
ids.json 記住上次貼的訊息編號，下次才改得到。
"""
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser

MANUAL = os.path.join(os.path.dirname(__file__), "..", "docs", "manual", "index.html")
WEB = "https://dadanny1122.github.io/video-helper/manual/"
COLOR = 0xC62828
DESC_MAX = 4000                     # Discord 一則說明最多 4096 字


class Manual(HTMLParser):
    """只抓需要的：h1 標題、h2 分章、段落、清單、表格、問答，連結轉成 [文字](網址)"""

    def __init__(self):
        super().__init__()
        self.sections = []          # [(標題, [一行一行])]
        self.title = ""
        self.intro = []
        self.buf = ""
        self.tag_stack = []
        self.href = None
        self.link_text = ""
        self.row = None
        self.in_nav = False
        self.list_depth = 0
        self.ol_count = []

    def lines(self):
        return self.sections[-1][1] if self.sections else self.intro

    def flush(self, prefix=""):
        t = " ".join(self.buf.split())
        if t:
            self.lines().append(prefix + t)
        self.buf = ""

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in ("style", "script", "title", "footer"):
            self.tag_stack.append(tag)
        if tag == "nav":
            self.in_nav = True
        if self.in_nav or self.skipping():
            return
        if tag in ("h1", "h2", "h3", "p", "summary", "div") and self.row is None:
            self.flush()
        if tag in ("ul", "ol"):
            self.flush()
            self.list_depth += 1
            self.ol_count.append(0 if tag == "ol" else None)
        elif tag == "li":
            self.flush()
            if self.ol_count and self.ol_count[-1] is not None:
                self.ol_count[-1] += 1
        elif tag == "tr":
            self.row = []
        elif tag in ("td", "th"):
            self.buf = ""
        elif tag == "a":
            self.href = a.get("href")
            self.link_text = ""
        elif tag == "br":
            self.buf += "\n"
        elif tag == "details":
            self.flush()
        elif tag == "span" and "t" in (a.get("class") or "").split():
            self.flush()
            self.buf = "**"                                  # 卡片的標題（例如「① 腳本」）
            self.tag_stack.append("span.t")
        self.tag_stack.append(tag) if tag in ("h1", "h2", "h3", "summary", "b") else None

    def skipping(self):
        return any(t in ("style", "script", "title", "footer") for t in self.tag_stack)

    def handle_endtag(self, tag):
        if tag == "nav":
            self.in_nav = False
            return
        if tag in ("style", "script", "title", "footer"):
            if tag in self.tag_stack:
                self.tag_stack.remove(tag)
            return
        if self.in_nav or self.skipping():
            return
        if tag == "span" and "span.t" in self.tag_stack:
            self.tag_stack.remove("span.t")
            self.flush()
            self.lines()[-1] += "**"
            return
        if tag == "a":
            text = self.link_text.strip()
            if self.href and self.href.startswith("http") and text:
                self.buf += f"[{text}]({self.href})"
            else:
                self.buf += text
            self.href = None
            return
        if tag == "h1":
            self.title = " ".join(self.buf.split())
            self.buf = ""
        elif tag == "h2":
            self.sections.append((" ".join(self.buf.split()), []))
            self.buf = ""
        elif tag == "h3":
            t = " ".join(self.buf.split())
            self.buf = ""
            if t:
                self.lines().append(f"**{t}**")
        elif tag == "summary":
            t = " ".join(self.buf.split())
            self.buf = ""
            self.lines().append(f"**❓ {t}**")
        elif tag == "li":
            n = self.ol_count[-1] if self.ol_count else None
            indent = "  " * (self.list_depth - 1)
            self.flush(f"{indent}{n}. " if n else f"{indent}• ")
        elif tag in ("ul", "ol"):
            self.flush()
            self.list_depth -= 1
            self.ol_count.pop()
        elif tag in ("td", "th"):
            self.row.append((" ".join(self.buf.split()), tag))
            self.buf = ""
        elif tag == "tr":
            cells = self.row or []
            self.row = None
            if cells and all(k == "th" for _, k in cells):
                return                                      # 表頭不用
            if cells:
                first, rest = cells[0][0], [c for c, _ in cells[1:] if c]
                self.lines().append(f"• **{first}**：" + "｜".join(rest) if rest else f"• {first}")
        elif tag in ("p", "div", "details"):
            self.flush()
        if tag in ("h1", "h2", "h3", "summary", "b") and tag in self.tag_stack:
            self.tag_stack.remove(tag)

    def handle_data(self, data):
        if self.in_nav or self.skipping():
            return
        if self.href is not None:
            self.link_text += data
        else:
            self.buf += data


def build():
    m = Manual()
    m.feed(open(MANUAL, encoding="utf-8").read())
    m.close()
    tw = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
    head = {
        "title": f"📖 {m.title or 'Video Helper 說明書'}",
        "url": WEB,
        "description": "\n".join(m.intro) + f"\n\n🌐 網頁版（圖比較清楚）：{WEB}\n🕒 最後更新：{tw}（台灣時間）\n"
                       "這個頻道的內容會跟著說明書自動更新。",
        "color": COLOR,
    }
    embeds = [head]
    for title, lines in m.sections:
        text = "\n".join(lines).strip()
        while text:                                         # 太長就拆成好幾則
            part = text[:DESC_MAX]
            if len(text) > DESC_MAX:
                cut = part.rfind("\n")
                part = part[:cut if cut > 1000 else DESC_MAX]
            embeds.append({"title": title if part is text[:len(part)] and embeds[-1].get("title") != title
                           else f"{title}（續）", "description": part.strip(), "color": COLOR})
            text = text[len(part):].strip()
    return embeds


def call(method, url, payload=None):
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json", "User-Agent": "VideoHelper-CI"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read()
                return json.loads(body) if body else {}
        except urllib.error.HTTPError as e:
            if e.code == 429:                               # 太快了：照 Discord 說的等一下
                wait = float(json.loads(e.read() or b"{}").get("retry_after", 2))
                time.sleep(min(wait, 30) + 0.5)
                continue
            raise
    raise RuntimeError("Discord 一直說太快，放棄")


def post(ids_path):
    hook = os.environ.get("DISCORD_MANUAL_WEBHOOK", "").strip()
    if not hook:
        print("沒有設定 DISCORD_MANUAL_WEBHOOK，略過")
        return
    embeds = build()
    try:
        old = json.load(open(ids_path, encoding="utf-8"))
    except Exception:
        old = []
    base = hook.split("?")[0]
    new_ids = []
    if len(old) == len(embeds):
        # 則數一樣 → 直接改原本的訊息，順序不變
        try:
            for mid, e in zip(old, embeds):
                call("PATCH", f"{base}/messages/{mid}", {"embeds": [e]})
                new_ids.append(mid)
                time.sleep(0.4)
            print(f"已更新 {len(new_ids)} 則訊息")
        except urllib.error.HTTPError as e:
            print(f"改不到舊訊息（{e.code}），改成重新貼")
            new_ids = []
    if not new_ids:
        for mid in old:                                     # 則數變了 → 刪掉舊的、重新貼
            try:
                call("DELETE", f"{base}/messages/{mid}")
                time.sleep(0.4)
            except urllib.error.HTTPError:
                pass
        for e in embeds:
            r = call("POST", f"{base}?wait=true", {"username": "Video Helper 使用手冊", "embeds": [e]})
            new_ids.append(r["id"])
            time.sleep(0.6)
        print(f"已重新貼 {len(new_ids)} 則訊息")
    json.dump(new_ids, open(ids_path, "w", encoding="utf-8"))


if __name__ == "__main__":
    if sys.argv[1:2] == ["preview"]:
        for e in build():
            print("=" * 60, "\n#", e["title"], f"（{len(e['description'])} 字）\n", e["description"], sep="")
    elif sys.argv[1:2] == ["post"]:
        post(sys.argv[2])
    else:
        print(__doc__)

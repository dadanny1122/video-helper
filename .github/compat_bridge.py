"""S3 相容性：用「指定那份原始碼」的電腦版，只啟動連線服務（Bridge，不開視窗）。
用法：python -I .github/compat_bridge.py <原始碼資料夾> <紀錄檔>
印一行 {"port", "code", "version"}；收到的「加入下載」「查進度」寫進紀錄檔（一行一筆 JSON）。"""
import json
import os
import sys
import tempfile

src, log_path = os.path.abspath(sys.argv[1]), sys.argv[2]
os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, src)
os.chdir(src)
import video_helper_app as a  # noqa: E402

open(log_path, "w").close()


def rec(**d):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(d, ensure_ascii=False) + "\n")


def on_enqueue(ticket, url, height, media=None, *rest, **kw):
    rec(kind="enqueue", ticket=ticket, url=url, height=height, media=media)


def get_status(ticket):
    rec(kind="status", ticket=ticket)
    return {"status": "等待中", "progress": 0, "title": "S3 相容性"}


def on_classify(*args, **kw):
    rec(kind="classify")
    return {"results": []}


def on_node(*args, **kw):
    rec(kind="node")
    return {"task_id": "compat"}


def on_later(*args, **kw):
    rec(kind="later")
    return True


import inspect  # noqa: E402
extra = {k: f for k, f in (("on_classify", on_classify), ("on_node", on_node), ("on_later", on_later))
         if k in inspect.signature(a.Bridge.__init__).parameters}       # 舊版沒有的參數就不給
b = a.Bridge(a.Settings(), on_enqueue, get_status, **extra)
port = b.start()
print(json.dumps({"port": port, "code": b.new_pair_code(), "version": a.VERSION}), flush=True)
sys.stdin.read()

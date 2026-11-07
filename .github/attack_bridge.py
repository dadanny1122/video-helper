"""攻擊模擬用：只啟動電腦版的「連線服務」（真的程式碼，不開視窗），印出埠和配對碼。
收到的「加入下載」寫進 argv[1] 那個檔案（一行一筆），測試程式拿來對答案。"""
import json, os, sys, tempfile
os.environ["VIDEO_HELPER_HOME"] = tempfile.mkdtemp()
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import video_helper_app as a  # noqa: E402

log_path = sys.argv[1]
open(log_path, "w").close()


def on_enqueue(ticket, url, height, media=None):
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps({"ticket": ticket, "url": url}) + "\n")


b = a.Bridge(a.Settings(), on_enqueue, lambda t: {"status": "queued", "progress": 0, "title": ""})
port = b.start()
print(json.dumps({"port": port, "code": b.new_pair_code()}), flush=True)
sys.stdin.read()          # 等測試程式關掉 stdin 就結束

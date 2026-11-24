"""電腦版的 HLS 小工具測試：first_segment_url（測速用，找出第一個影片片段）
不開視窗：只從 video_helper_app.py 拿出這個函式，網路換成假的（.github/hls/cases.json）
用法：python .github/hls_test.py
"""
import ast
import io
import json
import os
import sys
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
src = open(os.path.join(ROOT, "video_helper_app.py"), encoding="utf-8").read()
tree = ast.parse(src)
want = {"first_segment_url", "HLS_MAX_BYTES"}
code = [n for n in tree.body if (isinstance(n, ast.FunctionDef) and n.name in want)
        or (isinstance(n, ast.Assign) and any(getattr(t, "id", "") in want for t in n.targets))]
ns = {"urllib": urllib}
exec(compile(ast.Module(body=code, type_ignores=[]), "video_helper_app.py", "exec"), ns)
first_segment_url = ns["first_segment_url"]

FILES = {}
READS = []


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    url = req.full_url if hasattr(req, "full_url") else req
    if url not in FILES:
        raise OSError("404 " + url)
    r = Resp(FILES[url].encode())
    orig = r.read
    r.read = lambda n=-1: (READS.append(n), orig(n))[1]
    return r


urllib.request.urlopen = fake_urlopen
cases = json.load(open(os.path.join(ROOT, ".github", "hls", "cases.json"), encoding="utf-8"))
bad = 0


def check(name, got, exp):
    global bad
    if got == exp:
        print("✔", name)
    else:
        bad += 1
        print(f"::error title=HLS 電腦版::{name}：拿到 {got!r}，應該是 {exp!r}")


SEG = "#EXTM3U\n#EXTINF:4,\nseg0.ts\n"
for c in cases:
    if "first" not in c:
        continue
    FILES.clear()
    FILES[c["base"]] = c["text"]
    if c["kind"] == "master":                    # 畫質總表 → 往下一層找片段
        FILES[c["first"]] = SEG
        exp = urllib.parse.urljoin(c["first"], "seg0.ts")
    else:
        exp = c["first"]
    check(c["name"], first_segment_url(c["base"], {}), exp)

# 總表一直指向自己：要停下來、回 None（不能把清單網址當成片段）
B = "https://cdn.example.com/m.m3u8"
FILES.clear()
FILES[B] = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nm.m3u8\n"
check("總表指向自己 → None", first_segment_url(B, {}), None)
# 總表第一項是只有聲音的 → 跳過，找有影像的
FILES.clear()
FILES[B] = ("#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=64000,CODECS=\"mp4a.40.2\"\nau.m3u8\n"
            "#EXT-X-STREAM-INF:BANDWIDTH=900000,RESOLUTION=640x360\nv.m3u8\n")
FILES["https://cdn.example.com/v.m3u8"] = SEG
FILES["https://cdn.example.com/au.m3u8"] = "#EXTM3U\n#EXTINF:4,\naudio0.aac\n"
check("第一項只有聲音 → 用有影像的", first_segment_url(B, {}), "https://cdn.example.com/seg0.ts")
# 清單超大：只讀上限那麼多，不整個吃進記憶體
FILES.clear()
FILES[B] = "#EXTM3U\n#EXTINF:4,\nseg0.ts\n" + "#EXTINF:4,\nx.ts\n" * 400000
READS.clear()
got = first_segment_url(B, {})
check("超大清單只讀上限", (got, all(0 < n <= ns.get("HLS_MAX_BYTES", 0) for n in READS) and bool(READS)),
      ("https://cdn.example.com/seg0.ts", True))
check("連不上 → None", first_segment_url("https://nowhere.example/x.m3u8", {}), None)

print(f"{bad} 個沒過" if bad else "全部都過")
sys.exit(1 if bad else 0)

"""yt-dlp 出新版時，準備好「一鍵合併」的更新（維護風險 #3）
網站改版 → yt-dlp 社群通常幾天內修好 → 這支每天檢查 PyPI，有新版就：
  1. requirements.txt 的 yt-dlp 換新版本＋新指紋（指紋從 PyPI 官方拿，並用 pip --require-hashes 實際下載核對）
  2. 電腦版 VERSION 加 0.0.1、CHANGELOG 寫一段
  3. 推到分支 bot/ytdlp-<版本>，開 PR（開不了就給「建立 PR」的連結）
你按「合併」→ build.yml 照常跑全部測試（含 S3 相容性），過了才發布新的 EXE。
機器人推不進 main（S4 規則），所以一定要你按合併。

用法：python3 -I .github/ytdlp_update.py check     → 印 current／latest，GITHUB_OUTPUT 寫 newer=1/0
      python3 -I .github/ytdlp_update.py apply      → 改檔案（不 commit）
      python3 -I .github/ytdlp_update.py selftest
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REQ = os.path.join(ROOT, "requirements.txt")
APP = os.path.join(ROOT, "video_helper_app.py")
LOG = os.path.join(ROOT, "CHANGELOG.md")
BLOCK = re.compile(r"^yt-dlp==(?P<v>\S+) \\\n(?:    --hash=sha256:[0-9a-f]{64}(?: \\)?\n)+", re.M)


def vt(s):
    return tuple(int(x) for x in re.findall(r"\d+", s))


def current(text=None):
    m = BLOCK.search(text if text is not None else open(REQ, encoding="utf-8").read())
    if not m:
        raise SystemExit("requirements.txt 找不到 yt-dlp 那段")
    return m.group("v")


def pypi(ver=None):
    url = f"https://pypi.org/pypi/yt-dlp/{ver}/json" if ver else "https://pypi.org/pypi/yt-dlp/json"
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-CI"}), timeout=30) as r:
        return json.load(r)


def new_block(ver, hashes):
    lines = [f"yt-dlp=={ver} \\"] + [f"    --hash=sha256:{h} \\" for h in sorted(hashes)]
    lines[-1] = lines[-1][:-2]
    return "\n".join(lines) + "\n"


def replace_block(text, ver, hashes):
    return BLOCK.sub(lambda m: new_block(ver, hashes), text, count=1)


def bump(version):
    a, b, c = (int(x) for x in version.split("."))
    return f"{a}.{b}.{c + 1}"


def apply(ver, hashes, old):
    req = open(REQ, encoding="utf-8").read()
    open(REQ, "w", encoding="utf-8").write(replace_block(req, ver, hashes))
    app = open(APP, encoding="utf-8").read()
    m = re.search(r'^VERSION = "([^"]+)"', app, re.M)
    nv = bump(m.group(1))
    app = app[:m.start(1)] + nv + app[m.end(1):]
    open(APP, "w", encoding="utf-8").write(app)
    log = open(LOG, encoding="utf-8").read()
    entry = (f"## v{nv} 下載核心 yt-dlp 更新到 {ver}\n"
             f"- yt-dlp {old} → {ver}：網站改版後 yt-dlp 社群修好的下載問題，這版都會跟著修好\n"
             f"- 這次只換 yt-dlp（自動準備、你確認後才發布），其他功能沒變\n\n")
    i = log.index("## ")
    open(LOG, "w", encoding="utf-8").write(log[:i] + entry + log[i:])
    return nv


def verify_hashes(ver, hashes):
    """真的用 pip --require-hashes 下載一次，指紋對不上就失敗"""
    with tempfile.TemporaryDirectory() as d:
        req = os.path.join(d, "r.txt")
        open(req, "w").write(new_block(ver, hashes))
        r = subprocess.run([sys.executable, "-m", "pip", "download", "-q", "--no-deps", "--require-hashes",
                            "-r", req, "-d", d], capture_output=True, text=True)
        if r.returncode:
            raise SystemExit("yt-dlp 指紋核對失敗：" + r.stderr[-800:])


def out(**kw):
    p = os.environ.get("GITHUB_OUTPUT")
    for k, v in kw.items():
        print(f"{k}={v}")
        if p:
            with open(p, "a") as f:
                f.write(f"{k}={v}\n")


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "check"
    if cmd == "selftest":
        return selftest()
    cur = current()
    info = pypi()
    latest = info["info"]["version"]
    if info["info"].get("yanked") or vt(latest) <= vt(cur):
        out(current=cur, latest=latest, newer=0)
        return
    files = pypi(latest)["urls"]
    hashes = [f["digests"]["sha256"] for f in files if not f.get("yanked")]
    if not hashes:
        raise SystemExit("PyPI 上沒有 yt-dlp " + latest + " 的檔案")
    if cmd == "check":
        out(current=cur, latest=latest, newer=1)
        return
    verify_hashes(latest, hashes)
    nv = apply(latest, hashes, cur)
    out(current=cur, latest=latest, newer=1, app_version=nv)


def selftest():
    sample = ("x==1 \\\n    --hash=sha256:" + "a" * 64 + "\n"
              "yt-dlp==2026.10.26 \\\n    --hash=sha256:" + "1" * 64 + " \\\n    --hash=sha256:" + "2" * 64 + "\n"
              "    # via -r requirements.in\n")
    assert current(sample) == "2026.10.26"
    t = replace_block(sample, "2026.11.23", ["f" * 64, "e" * 64])
    assert "yt-dlp==2026.11.23 \\\n    --hash=sha256:" + "e" * 64 + " \\\n    --hash=sha256:" + "f" * 64 + "\n    # via" in t, t
    assert "1" * 64 not in t and t.startswith("x==1")
    assert bump("1.46.0") == "1.46.1"
    assert vt("2026.11.23") > vt("2026.10.26") and vt("2026.1.5") < vt("2026.10.26")
    assert current() and vt(current())
    print("selftest OK")


if __name__ == "__main__":
    main()

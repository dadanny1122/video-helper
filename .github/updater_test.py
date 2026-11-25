"""一鍵更新的邏輯測試（vh_updater.py，v1.47.0；Linux／Windows 都能跑，不連網）
python3 -I .github/updater_test.py
"""
import base64
import hashlib
import io
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import vh_updater as U  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")     # python -I 不看 PYTHONIOENCODING（Windows 主控台是 cp1252）

REPO = "dadanny1122/video-helper"
fails = 0


def ok(c, m):
    global fails
    print(("✅ " if c else "❌ ") + m)
    if not c:
        fails += 1


def att(workflow=".github/workflows/build.yml", repo="https://github.com/" + REPO, typ="https://slsa.dev/provenance/v1"):
    st = {"predicateType": typ, "predicate": {"buildDefinition": {"externalParameters": {
        "workflow": {"path": workflow, "repository": repo}}}}}
    return {"bundle": {"dsseEnvelope": {"payload": base64.b64encode(json.dumps(st).encode()).decode()}}}


class Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class Opener:
    def __init__(self, data):
        self.data = data
        self.urls = []

    def open(self, req, timeout=None):
        self.urls.append(req.full_url)
        return Resp(self.data)


tmp = tempfile.mkdtemp()
exe_bytes = os.urandom(300_000)
exe_sha = hashlib.sha256(exe_bytes).hexdigest()
lock = json.dumps({"desktop": {"version": "1.48.0", "exe": {"file": "VideoHelper.exe", "size": len(exe_bytes), "sha256": exe_sha}}}).encode()
rel_api = {"tag_name": "v1.48.0", "html_url": "https://github.com/x", "body": "notes", "assets": [
    {"name": "VideoHelper.exe", "size": len(exe_bytes), "digest": "sha256:" + exe_sha,
     "browser_download_url": "https://github.com/dadanny1122/video-helper/releases/download/v1.48.0/VideoHelper.exe"},
    {"name": "components.lock.json", "size": len(lock), "digest": "sha256:" + hashlib.sha256(lock).hexdigest(),
     "browser_download_url": "https://github.com/dadanny1122/video-helper/releases/download/v1.48.0/components.lock.json"}]}


def gh_ok(atts):
    def gh(url):
        if url.endswith("/releases/latest"):
            return 200, rel_api
        if "/attestations/sha256:" in url:
            return (200, {"attestations": atts}) if atts is not None else (404, None)
        return 404, None
    return gh


# ---- 發布頁資料 ----
rel = U.latest_release(gh_ok([att()]), REPO)
ok(rel["tag"] == "1.48.0" and rel["exe"]["sha256"] == exe_sha and rel["lock"]["size"] == len(lock), "讀發布頁：版本、EXE、元件清單")
bad = dict(rel_api, assets=[a for a in rel_api["assets"] if a["name"] != "components.lock.json"])
try:
    U.latest_release(lambda u: (200, bad), REPO)
    ok(False, "沒有元件清單要停")
except U.UpdateError as e:
    ok("components.lock.json" in str(e), "沒有元件清單 → 停（等上傳完）")
nodig = dict(rel_api, assets=[dict(a, digest="") for a in rel_api["assets"]])
try:
    U.latest_release(lambda u: (200, nodig), REPO)
    ok(False, "沒有指紋要停")
except U.UpdateError:
    ok(True, "發布頁沒有指紋 → 停（不能核對就不更新）")
try:
    U.latest_release(lambda u: (403, None), REPO)
    ok(False, "限流要停")
except U.UpdateError as e:
    ok("限制" in str(e), "GitHub 限流 → 給白話原因")

# ---- 下載 ----
dest = os.path.join(tmp, "VideoHelper.exe.new")
seen = []
U.download(rel["exe"]["url"], dest, len(exe_bytes), progress=lambda g, t: seen.append(g), opener=Opener(exe_bytes))
ok(open(dest, "rb").read() == exe_bytes and seen[-1] == len(exe_bytes) and not os.path.exists(dest + ".part"), "下載完成、有進度、沒有半成品")
for name, args, want in [
    ("網址不是 GitHub", ("https://evil.example/x.exe", dest + "2", 10, None, None, Opener(b"x" * 10)), "不是 GitHub"),
    ("http 不是 https", ("http://github.com/x.exe", dest + "2", 10, None, None, Opener(b"x" * 10)), "不是 GitHub"),
    ("比發布頁大", (rel["exe"]["url"], dest + "2", 10, None, None, Opener(b"x" * 11)), "比發布頁寫的大"),
    ("比發布頁小", (rel["exe"]["url"], dest + "2", 10, None, None, Opener(b"x" * 9)), "不完整"),
    ("按了取消", (rel["exe"]["url"], dest + "2", 10, None, lambda: True, Opener(b"x" * 10)), "取消"),
]:
    try:
        U.download(*args)
        ok(False, name)
    except U.UpdateError as e:
        ok(want in str(e) and not os.path.exists(dest + "2") and not os.path.exists(dest + "2.part"), f"下載 {name} → 停、刪掉半成品")
ok(U.host_ok("https://objects.githubusercontent.com/x") and U.host_ok("https://release-assets.githubusercontent.com/x")
   and not U.host_ok("https://githubusercontent.com.evil.io/x") and not U.host_ok("https://evilgithub.com/x"), "只准 github.com／*.githubusercontent.com")
try:
    U._SafeRedirect().redirect_request(None, None, 302, "", {}, "https://evil.example/x")
    ok(False, "轉到別的網站要停")
except U.UpdateError:
    ok(True, "下載被轉到別的網站 → 停")

# ---- 三關核對 ----
ok(U.verify(dest, rel, gh_ok([att()]), REPO, lock) is None, "三關都過")
ok((U.verify(dest, rel, gh_ok([att(workflow=".github/workflows/evil.yml")]), REPO, lock) or "").startswith("②"), "② 不是 build.yml 打包的 → 不換")
ok((U.verify(dest, rel, gh_ok([att(repo="https://github.com/someone/fork")]), REPO, lock) or "").startswith("②"), "② 別人的 repo 打包的 → 不換")
ok((U.verify(dest, rel, gh_ok([]), REPO, lock) or "").startswith("②"), "② 沒有簽章紀錄 → 不換")
ok((U.verify(dest, rel, gh_ok(None), REPO, lock) or "").startswith("②"), "② 查不到（404）→ 不換")
ok((U.verify(dest, rel, gh_ok([att(typ="https://cyclonedx.org/bom")]), REPO, lock) or "").startswith("②"), "② 只有 SBOM 沒有打包紀錄 → 不換")
lock2 = lock.replace(exe_sha.encode(), b"0" * 64)
rel2 = json.loads(json.dumps(rel))
rel2["lock"]["sha256"] = hashlib.sha256(lock2).hexdigest()
ok((U.verify(dest, rel2, gh_ok([att()]), REPO, lock2) or "").startswith("③"), "③ 元件清單寫的 EXE 指紋不一樣 → 不換")
ok((U.verify(dest, rel, gh_ok([att()]), REPO, lock2) or "").startswith("③"), "③ 元件清單被換掉（指紋跟發布頁不一樣）→ 不換")
with open(dest, "r+b") as f:
    f.seek(100)
    f.write(b"\x00\x01\x02")
ok((U.verify(dest, rel, gh_ok([att()]), REPO, lock) or "").startswith("①"), "① 檔案被改過 → 不換")

# ---- 換檔、小幫手 ----
d = os.path.join(tmp, "app")
os.makedirs(d)
data = os.path.join(tmp, "data")
os.makedirs(data)
exe = os.path.join(d, "VideoHelper.exe")
P = U.paths(exe)


def fresh():
    for k in ("old", "failed", "new"):
        U._rm(P[k])
    open(exe, "w").write("OLD")
    open(P["new"], "w").write("NEW")


fresh()
old = U.swap(exe)
ok(open(exe).read() == "NEW" and open(old).read() == "OLD" and not os.path.exists(P["new"]), "換檔：新版就位、舊版改名 .old")


class FakeProc:
    def __init__(self, code=None):
        self.returncode = code
        self.pid = 999999

    def poll(self):
        return self.returncode

    def kill(self):
        self.returncode = -9

    def wait(self, t=None):
        return self.returncode


# 新版正常：啟動後寫「啟動成功」
launched = []


def launch_good(cmd):
    launched.append(cmd)
    U.mark_ok(data, "1.48.0")
    return FakeProc()


arg = json.dumps({"exe": exe, "old": old, "new_ver": "1.48.0", "old_ver": "1.47.0", "data": data})
r = U.watchdog(arg, timeout=5, launch=launch_good, sleep=lambda s: None)
ok(r == "ok" and launched == [[exe, "--after-update", "1.47.0"]] and open(exe).read() == "NEW", "小幫手：新版回報成功 → 保留新版")
ok(U.read_state(data).get("result") == "ok", "記下更新成功")
ok(U.cleanup(exe) and not os.path.exists(old), "新版清掉 .old")

# 新版閃退：自動換回
fresh()
old = U.swap(exe)
launched.clear()
U._rm(U.ok_marker(data))


def launch_crash(cmd):
    launched.append(cmd)
    return FakeProc(code=3) if "--after-update" in cmd else FakeProc()


r = U.watchdog(arg, timeout=5, launch=launch_crash, sleep=lambda s: None)
ok(r == "rolled_back" and open(exe).read() == "OLD" and open(P["failed"]).read() == "NEW", "小幫手：新版閃退 → 換回舊版")
ok(launched[-1] == [exe, "--update-failed", "1.48.0"], "換回後開舊版並說更新失敗")
st = U.read_state(data)
ok(st.get("result") == "rolled_back" and "代碼 3" in st.get("reason", ""), "記下失敗原因")

# 新版卡住、沒回報：時間到換回
fresh()
old = U.swap(exe)
U._rm(U.ok_marker(data))
t0 = time.time()
r = U.watchdog(arg, timeout=1, launch=lambda c: FakeProc(), sleep=lambda s: time.sleep(min(s, 0.2)))
ok(r == "rolled_back" and open(exe).read() == "OLD", "小幫手：時間到沒回報 → 換回舊版")
ok("秒內沒有正常打開" in U.read_state(data).get("reason", ""), "記下「沒有正常打開」")

# 回報的是別的版本 → 不算
fresh()
old = U.swap(exe)
U.mark_ok(data, "1.47.0")
r = U.watchdog(arg, timeout=1, launch=lambda c: FakeProc(), sleep=lambda s: time.sleep(min(s, 0.2)))
ok(r == "rolled_back", "回報的版本不對（舊的回報檔）→ 不算成功")

ok(U.can_write(tmp), "可寫入的資料夾")
ok(U.vtuple("v1.47.0") < U.vtuple("1.48.0") < U.vtuple("1.100.0"), "版本比大小（1.100 > 1.48）")

print("全部通過" if not fails else f"❌ {fails} 項沒過")
sys.exit(1 if fails else 0)

"""電腦版一鍵更新（v1.47.0，docs/infra/03-電腦版一鍵更新規格.md；D1～D4 照建議）

流程：latest_release() 拿發布頁資料 → download() 下載到 VideoHelper.exe.new →
verify() 三關（發布頁指紋、GitHub 打包簽章紀錄、components.lock.json）→ swap() 換檔 →
start_watchdog()：用舊的 EXE（已改名 .old）當小幫手，開新版、等它回報「啟動成功」（mark_ok），
WATCH_SECONDS 秒內沒回報或先結束 → 換回舊版、開舊版並告訴使用者。

這個檔只放邏輯（不碰視窗），方便測試；網路一律經呼叫端給的 gh(url)→(狀態碼, json) 和 opener。
"""
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.request

WATCH_SECONDS = 60                     # D3
ALLOWED_HOSTS = ("github.com", "githubusercontent.com")
EXE_NAME = "VideoHelper.exe"
LOCK_NAME = "components.lock.json"
CHUNK = 1 << 20


class UpdateError(Exception):
    """給使用者看的原因（中文）"""


def vtuple(v):
    return tuple(int(x) for x in str(v).lstrip("v").split("."))


def host_ok(url):
    from urllib.parse import urlsplit
    u = urlsplit(url)
    h = (u.hostname or "").lower()
    return u.scheme == "https" and any(h == d or h.endswith("." + d) for d in ALLOWED_HOSTS)


def latest_release(gh, repo):
    """發布頁最新版：{tag, url, notes, exe: {url, size, sha256}, lock: {url, size, sha256}}"""
    code, data = gh(f"https://api.github.com/repos/{repo}/releases/latest")
    if code in (403, 429):
        raise UpdateError("GitHub 暫時限制查詢次數，請過一陣子再試")
    if code != 200 or not isinstance(data, dict):
        raise UpdateError(f"連不到 GitHub（{code}）")
    assets = {a.get("name"): a for a in data.get("assets") or []}

    def asset(name):
        a = assets.get(name)
        if not a:
            raise UpdateError(f"這一版的發布頁還沒有 {name}（可能還在上傳），等幾分鐘再試")
        sha = (a.get("digest") or "").removeprefix("sha256:")
        if len(sha) != 64:
            raise UpdateError(f"發布頁沒有 {name} 的指紋，不能核對，請手動更新")
        return {"url": a.get("browser_download_url") or "", "size": int(a.get("size") or 0), "sha256": sha}
    return {"tag": (data.get("tag_name") or "").lstrip("v"), "url": data.get("html_url") or "",
            "notes": (data.get("body") or "").strip(), "exe": asset(EXE_NAME), "lock": asset(LOCK_NAME)}


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """只准轉到 github.com／*.githubusercontent.com（下載檔會從 github.com 轉到檔案伺服器）"""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not host_ok(newurl):
            raise UpdateError("下載被轉到不認識的網站，已停止")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def default_opener():
    return urllib.request.build_opener(_SafeRedirect())


def download(url, dest, size, progress=None, cancel=None, opener=None, ua="VideoHelper"):
    """下載到 dest（先寫 dest.part 再改名）；大小不對、被取消、網址不對都丟 UpdateError 並刪掉半成品"""
    if not host_ok(url):
        raise UpdateError("下載網址不是 GitHub，已停止")
    opener = opener or default_opener()
    part = dest + ".part"
    got = 0
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": ua}), timeout=30) as r, open(part, "wb") as f:
            while True:
                if cancel and cancel():
                    raise UpdateError("已取消更新")
                b = r.read(CHUNK)
                if not b:
                    break
                got += len(b)
                if size and got > size:
                    raise UpdateError("下載的檔案比發布頁寫的大，已停止")
                f.write(b)
                if progress:
                    progress(got, size)
        if size and got != size:
            raise UpdateError(f"下載不完整（{got}/{size}），請再試一次")
        os.replace(part, dest)
    except UpdateError:
        _rm(part)
        raise
    except Exception as e:
        _rm(part)
        raise UpdateError(f"下載失敗：{type(e).__name__}") from e


def fetch_json(url, opener=None, limit=1 << 20):
    if not host_ok(url):
        raise UpdateError("清單網址不是 GitHub")
    opener = opener or default_opener()
    with opener.open(urllib.request.Request(url, headers={"User-Agent": "VideoHelper"}), timeout=30) as r:
        data = r.read(limit + 1)
    if len(data) > limit:
        raise UpdateError("元件清單太大，不正常")
    return data


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(CHUNK), b""):
            h.update(b)
    return h.hexdigest()


def verify(path, rel, gh, repo, lock_bytes):
    """三關都過回 None；不過回原因（給使用者看）"""
    want = rel["exe"]
    if os.path.getsize(path) != want["size"]:
        return "① 檔案大小跟發布頁不一樣"
    sha = sha256_file(path)
    if sha != want["sha256"]:
        return "① 檔案指紋跟發布頁不一樣（可能下載壞了或被改過）"
    # ② GitHub 打包簽章紀錄：要有本 repo build.yml 對這個指紋的 provenance
    code, data = gh(f"https://api.github.com/repos/{repo}/attestations/sha256:{sha}")
    if code in (403, 429):
        return "② GitHub 暫時限制查詢次數，查不到打包紀錄，請過一陣子再試"
    if code != 200 or not isinstance(data, dict):
        return "② GitHub 查不到這個檔案的打包紀錄"
    ok = False
    for att in data.get("attestations") or []:
        info = attest_info(att)
        if ("provenance" in info.get("type", "") or "slsa" in info.get("type", "")) \
                and info.get("workflow", "").endswith(".github/workflows/build.yml") \
                and info.get("repo", "").rstrip("/").lower().endswith(repo.lower()):
            ok = True
            break
    if not ok:
        return "② 這個檔案不是本專案 build.yml 打包的（沒有對應的簽章紀錄）"
    # ③ 元件清單：自己的指紋也要對得上發布頁，裡面寫的 EXE 指紋要一樣
    if hashlib.sha256(lock_bytes).hexdigest() != rel["lock"]["sha256"]:
        return "③ 元件清單的指紋跟發布頁不一樣"
    try:
        lock = json.loads(lock_bytes)
        d = lock["desktop"]
        if str(d.get("version")) != rel["tag"] or d["exe"]["sha256"] != sha or int(d["exe"]["size"]) != want["size"]:
            return "③ 元件清單寫的版本或 EXE 指紋對不上"
    except (ValueError, KeyError, TypeError):
        return "③ 元件清單讀不懂"
    return None


def attest_info(att):
    try:
        import base64
        env = (att.get("bundle") or {}).get("dsseEnvelope") or {}
        st = json.loads(base64.b64decode(env.get("payload") or ""))
        pred = st.get("predicate") or {}
        wf = ((pred.get("buildDefinition") or {}).get("externalParameters") or {}).get("workflow") or {}
        return {"type": st.get("predicateType", ""), "workflow": wf.get("path", ""), "repo": wf.get("repository", "")}
    except Exception:
        return {}


def can_write(folder):
    t = os.path.join(folder, f".vh-write-test-{os.getpid()}")
    try:
        with open(t, "wb") as f:
            f.write(b"x")
        os.remove(t)
        return True
    except OSError:
        return False


def paths(exe):
    return {"new": exe + ".new", "old": exe + ".old", "failed": exe + ".failed"}


def swap(exe):
    """VideoHelper.exe → .old、.new → VideoHelper.exe（Windows 准許改名正在執行的檔）；中途失敗就復原"""
    p = paths(exe)
    _rm(p["old"])
    os.replace(exe, p["old"])
    try:
        os.replace(p["new"], exe)
    except OSError:
        os.replace(p["old"], exe)
        raise
    return p["old"]


def state_path(data_dir):
    return os.path.join(data_dir, "update_state.json")


def write_state(data_dir, **kw):
    try:
        with open(state_path(data_dir), "w", encoding="utf-8") as f:
            json.dump(dict(kw, time=time.time()), f, ensure_ascii=False)
    except OSError:
        pass


def read_state(data_dir):
    try:
        with open(state_path(data_dir), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def ok_marker(data_dir):
    return os.path.join(data_dir, "update_ok.json")


def mark_ok(data_dir, version):
    """新版呼叫：介面打開了 → 告訴小幫手「啟動成功」"""
    try:
        with open(ok_marker(data_dir), "w", encoding="utf-8") as f:
            json.dump({"version": version, "pid": os.getpid(), "time": time.time()}, f)
    except OSError:
        pass


def _flags():
    if sys.platform.startswith("win"):
        return {"creationflags": 0x00000008 | 0x00000200}      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    return {"start_new_session": True}


def start_watchdog(old_exe, exe, new_ver, old_ver, data_dir, extra=()):
    """由舊版呼叫（換檔之後）：用 .old 開小幫手，小幫手再開新版"""
    _rm(ok_marker(data_dir))
    args = json.dumps({"exe": exe, "old": old_exe, "new_ver": new_ver, "old_ver": old_ver, "data": data_dir,
                       "wait_pid": os.getpid()})
    return subprocess.Popen([old_exe, "--update-watchdog", args, *extra], close_fds=True, **_flags())


def wait_pid(pid, secs):
    """等某個程序結束（最多 secs 秒）。Windows 不能用 os.kill(pid, 0)：那會直接把它關掉"""
    end = time.time() + secs
    if sys.platform.startswith("win"):
        import ctypes
        from ctypes import wintypes
        k = ctypes.WinDLL("kernel32", use_last_error=True)
        k.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        k.OpenProcess.restype = wintypes.HANDLE
        k.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        k.WaitForSingleObject.restype = wintypes.DWORD
        k.CloseHandle.argtypes = (wintypes.HANDLE,)
        h = k.OpenProcess(0x00100000, False, pid)          # SYNCHRONIZE
        if not h:
            return True
        try:
            return k.WaitForSingleObject(h, int(secs * 1000)) == 0
        finally:
            k.CloseHandle(h)
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except OSError:
            return True
        time.sleep(0.2)
    return False


def _kill_tree(proc):
    try:
        if sys.platform.startswith("win"):
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True, timeout=20)
        else:
            proc.kill()
        proc.wait(10)
    except Exception:
        pass


def watchdog(arg, timeout=WATCH_SECONDS, launch=None, sleep=time.sleep):
    """小幫手（跑在舊版 .old 裡）：開新版 → 等「啟動成功」→ 沒等到就換回舊版。回 "ok" 或 "rolled_back" """
    a = json.loads(arg)
    exe, old, data = a["exe"], a["old"], a["data"]
    launch = launch or (lambda cmd: subprocess.Popen(cmd, close_fds=True, **_flags()))
    if a.get("wait_pid"):
        wait_pid(int(a["wait_pid"]), 30)        # 等舊版完全關掉（設定、紀錄都存好）才開新版
    proc = launch([exe, "--after-update", a["old_ver"]])
    end = time.time() + timeout
    marker = ok_marker(data)
    why = f"{timeout} 秒內沒有正常打開"
    while time.time() < end:
        try:
            with open(marker, encoding="utf-8") as f:
                if json.load(f).get("version") == a["new_ver"]:
                    write_state(data, result="ok", version=a["new_ver"], previous=a["old_ver"])
                    return "ok"
        except (OSError, ValueError):
            pass
        if proc.poll() is not None:
            why = f"打開後馬上結束（代碼 {proc.returncode}）"
            break
        sleep(0.5)
    _kill_tree(proc)
    sleep(1.5)
    p = paths(exe)
    for _ in range(10):                      # 新版剛被關掉，檔案可能還被鎖幾秒
        try:
            _rm(p["failed"])
            os.replace(exe, p["failed"])
            os.replace(old, exe)
            break
        except OSError:
            sleep(1)
    write_state(data, result="rolled_back", version=a["new_ver"], previous=a["old_ver"], reason=why)
    launch([exe, "--update-failed", a["new_ver"]])
    return "rolled_back"


def cleanup(exe, attempts=1, sleep=time.sleep):
    """新版正常啟動後清掉 .old／.failed／.new（小幫手可能還在跑，刪不掉就稍後再試）"""
    p = paths(exe)
    left = []
    for _ in range(max(1, attempts)):
        left = [k for k in ("old", "failed", "new") if os.path.exists(p[k]) and not _rm(p[k])]
        _rm(p["new"] + ".part")
        if not left:
            return True
        sleep(5)
    return not left


def _rm(path):
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False

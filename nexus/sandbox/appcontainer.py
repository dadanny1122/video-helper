"""
強隔離偵測（APEX Phase 1 PR2）：這台 Windows 能不能把沙箱子程序放進 AppContainer。
**只偵測，不改下載流程**（下載照舊用 jobs.Job，跟 v1.50.0 一樣）。

為什麼用另一個子程序偵測：這裡全是 ctypes 呼叫 Win32，萬一哪個結構寫錯，最多讓偵測子程序當掉，
節點本身不受影響（偵測結果就是 none＋原因）。

偵測內容（在 AppContainer 裡跑 Windows 內建的 cmd.exe／curl.exe，只給 internetClient 能力＝以後真的要用的設定）：
  run      ：程序開得起來、拿得到結束碼，而且放得進 Job（以後要兩個一起用）
  file     ：讀不到放在節點資料夾的測試檔（模擬私鑰的位置；**不碰真的私鑰**）
             對照組：同一個指令不在 AppContainer 裡要讀得到，才證明是 AppContainer 擋的
  loopback ：連不到本機 127.0.0.1 上剛開的測試埠（Master 常在同一台）
             對照組：不在 AppContainer 裡要連得到
全部符合預期才算 hardening="appcontainer"；任何一項不確定 → "none"＋原因（不猜）。

誠實說明：internetClient 還是能連外網；網域白名單照舊只在程式層（nexus/net/guard.py）。
區網其他電腦（192.168.x）這裡沒測（要另一台電腦才測得準）。
"""
import ctypes
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time

IS_WINDOWS = sys.platform == "win32"
PROFILE_NAME = "VideoHelper.Nexus.Probe"
INTERNET_CLIENT_SID = "S-1-15-3-1"
LEVELS = ("appcontainer", "none")

CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
EXTENDED_STARTUPINFO_PRESENT = 0x00080000
PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES = 0x00020009
SE_GROUP_ENABLED = 0x00000004
WAIT_OBJECT_0 = 0
HR_ALREADY_EXISTS = 0x800700B7          # HRESULT_FROM_WIN32(ERROR_ALREADY_EXISTS)
CURL_COULDNT_CONNECT = 7


class ProbeError(Exception):
    pass


if IS_WINDOWS:
    from ctypes import wintypes

    class SID_AND_ATTRIBUTES(ctypes.Structure):
        _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", wintypes.DWORD)]

    class SECURITY_CAPABILITIES(ctypes.Structure):
        _fields_ = [("AppContainerSid", ctypes.c_void_p), ("Capabilities", ctypes.POINTER(SID_AND_ATTRIBUTES)),
                    ("CapabilityCount", wintypes.DWORD), ("Reserved", wintypes.DWORD)]

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("lpReserved", wintypes.LPWSTR), ("lpDesktop", wintypes.LPWSTR),
                    ("lpTitle", wintypes.LPWSTR), ("dwX", wintypes.DWORD), ("dwY", wintypes.DWORD),
                    ("dwXSize", wintypes.DWORD), ("dwYSize", wintypes.DWORD), ("dwXCountChars", wintypes.DWORD),
                    ("dwYCountChars", wintypes.DWORD), ("dwFillAttribute", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("wShowWindow", wintypes.WORD), ("cbReserved2", wintypes.WORD), ("lpReserved2", ctypes.c_void_p),
                    ("hStdInput", wintypes.HANDLE), ("hStdOutput", wintypes.HANDLE), ("hStdError", wintypes.HANDLE)]

    class STARTUPINFOEXW(ctypes.Structure):
        _fields_ = [("StartupInfo", STARTUPINFOW), ("lpAttributeList", ctypes.c_void_p)]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [("hProcess", wintypes.HANDLE), ("hThread", wintypes.HANDLE),
                    ("dwProcessId", wintypes.DWORD), ("dwThreadId", wintypes.DWORD)]

    def _api():
        """載入 Win32 函式並設好 argtypes／restype（64 位元 handle 不設會被截斷）"""
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        adv = ctypes.WinDLL("advapi32", use_last_error=True)
        env = ctypes.WinDLL("userenv", use_last_error=True)
        PSID_P = ctypes.POINTER(ctypes.c_void_p)
        env.CreateAppContainerProfile.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.LPCWSTR,
                                                  ctypes.c_void_p, wintypes.DWORD, PSID_P]
        env.CreateAppContainerProfile.restype = ctypes.c_long
        env.DeriveAppContainerSidFromAppContainerName.argtypes = [wintypes.LPCWSTR, PSID_P]
        env.DeriveAppContainerSidFromAppContainerName.restype = ctypes.c_long
        env.DeleteAppContainerProfile.argtypes = [wintypes.LPCWSTR]
        env.DeleteAppContainerProfile.restype = ctypes.c_long
        adv.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, PSID_P]
        adv.ConvertStringSidToSidW.restype = wintypes.BOOL
        adv.FreeSid.argtypes = [ctypes.c_void_p]
        adv.FreeSid.restype = ctypes.c_void_p
        k32.LocalFree.argtypes = [ctypes.c_void_p]
        k32.LocalFree.restype = ctypes.c_void_p
        k32.InitializeProcThreadAttributeList.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD,
                                                          ctypes.POINTER(ctypes.c_size_t)]
        k32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        k32.UpdateProcThreadAttribute.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.c_size_t, ctypes.c_void_p,
                                                  ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p]
        k32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        k32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        k32.DeleteProcThreadAttributeList.restype = None
        k32.CreateProcessW.argtypes = [wintypes.LPCWSTR, wintypes.LPWSTR, ctypes.c_void_p, ctypes.c_void_p, wintypes.BOOL,
                                       wintypes.DWORD, ctypes.c_void_p, wintypes.LPCWSTR, ctypes.c_void_p,
                                       ctypes.POINTER(PROCESS_INFORMATION)]
        k32.CreateProcessW.restype = wintypes.BOOL
        k32.ResumeThread.argtypes = [wintypes.HANDLE]
        k32.ResumeThread.restype = wintypes.DWORD
        k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        k32.WaitForSingleObject.restype = wintypes.DWORD
        k32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        k32.GetExitCodeProcess.restype = wintypes.BOOL
        k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        k32.TerminateProcess.restype = wintypes.BOOL
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        k32.CloseHandle.restype = wintypes.BOOL
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.AssignProcessToJobObject.restype = wintypes.BOOL
        return k32, adv, env


def _hr(v: int) -> int:
    return v & 0xFFFFFFFF


class Container:
    """一個 AppContainer 設定檔＋只給 internetClient 的能力清單；用完 close() 會刪掉設定檔"""

    def __init__(self, name: str = PROFILE_NAME):
        self.k32, self.adv, self.env = _api()
        self.name = name
        self.sid = ctypes.c_void_p()
        hr = _hr(self.env.CreateAppContainerProfile(name, "Video Helper 節點沙箱偵測", "Video Helper Nexus probe",
                                                    None, 0, ctypes.byref(self.sid)))
        if hr == HR_ALREADY_EXISTS:              # 上次偵測沒清掉：沿用同一個名字
            hr = _hr(self.env.DeriveAppContainerSidFromAppContainerName(name, ctypes.byref(self.sid)))
        if hr != 0 or not self.sid:
            raise ProbeError(f"建立 AppContainer 失敗（HRESULT 0x{hr:08X}）")
        self.cap_sid = ctypes.c_void_p()
        if not self.adv.ConvertStringSidToSidW(INTERNET_CLIENT_SID, ctypes.byref(self.cap_sid)):
            self.close()
            raise ProbeError(f"internetClient 能力轉換失敗（Windows 錯誤 {ctypes.get_last_error()}）")
        self.caps = (SID_AND_ATTRIBUTES * 1)(SID_AND_ATTRIBUTES(self.cap_sid, SE_GROUP_ENABLED))

    def run(self, app: str, cmdline: str, cwd: str, timeout: float = 15.0, job=None) -> int:
        """在 AppContainer 裡跑一個程式，回傳結束碼；job（jobs.Job）有給就先放進 Job 再開始跑"""
        k32 = self.k32
        sc = SECURITY_CAPABILITIES(self.sid, ctypes.cast(self.caps, ctypes.POINTER(SID_AND_ATTRIBUTES)), 1, 0)
        size = ctypes.c_size_t(0)
        k32.InitializeProcThreadAttributeList(None, 1, 0, ctypes.byref(size))      # 第一次只問要多大（會回 False）
        buf = (ctypes.c_byte * size.value)()
        if not k32.InitializeProcThreadAttributeList(buf, 1, 0, ctypes.byref(size)):
            raise ProbeError(f"InitializeProcThreadAttributeList 失敗（{ctypes.get_last_error()}）")
        pi = PROCESS_INFORMATION()
        try:
            if not k32.UpdateProcThreadAttribute(buf, 0, PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES, ctypes.byref(sc),
                                                 ctypes.sizeof(sc), None, None):
                raise ProbeError(f"UpdateProcThreadAttribute 失敗（{ctypes.get_last_error()}）")
            si = STARTUPINFOEXW()
            si.StartupInfo.cb = ctypes.sizeof(STARTUPINFOEXW)
            si.lpAttributeList = ctypes.cast(buf, ctypes.c_void_p)
            line = ctypes.create_unicode_buffer(cmdline)
            flags = EXTENDED_STARTUPINFO_PRESENT | CREATE_SUSPENDED | CREATE_NO_WINDOW
            if not k32.CreateProcessW(app, line, None, None, False, flags, None, cwd, ctypes.byref(si), ctypes.byref(pi)):
                raise ProbeError(f"在 AppContainer 裡開程序失敗（Windows 錯誤 {ctypes.get_last_error()}）")
            if job is not None and not k32.AssignProcessToJobObject(job.handle, pi.hProcess):
                k32.TerminateProcess(pi.hProcess, 1)
                raise ProbeError(f"AppContainer 程序放不進 Job（Windows 錯誤 {ctypes.get_last_error()}）")
            if k32.ResumeThread(pi.hThread) == 0xFFFFFFFF:
                k32.TerminateProcess(pi.hProcess, 1)
                raise ProbeError("恢復程序執行失敗")
            if k32.WaitForSingleObject(pi.hProcess, int(timeout * 1000)) != WAIT_OBJECT_0:
                k32.TerminateProcess(pi.hProcess, 1)
                raise ProbeError(f"超過 {timeout:g} 秒沒結束")
            code = wintypes.DWORD()
            if not k32.GetExitCodeProcess(pi.hProcess, ctypes.byref(code)):
                raise ProbeError("拿不到結束碼")
            return int(code.value)
        finally:
            for h in (pi.hThread, pi.hProcess):
                if h:
                    k32.CloseHandle(h)
            k32.DeleteProcThreadAttributeList(buf)

    def close(self):
        if getattr(self, "cap_sid", None):
            self.k32.LocalFree(self.cap_sid)
            self.cap_sid = None
        if getattr(self, "sid", None):
            self.adv.FreeSid(self.sid)
            self.sid = None
        self.env.DeleteAppContainerProfile(self.name)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class _Listener:
    """127.0.0.1 測試埠：數有幾個連線進來（判斷「真的連上了」，不只看 curl 的結束碼）"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.sock.settimeout(0.2)
        self.port = self.sock.getsockname()[1]
        self.hits = 0
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True)
        self._t.start()

    def _loop(self):
        while not self._stop.is_set():
            try:
                c, _ = self.sock.accept()
                self.hits += 1
                c.close()
            except OSError:
                pass

    def close(self):
        self._stop.set()
        self._t.join(2)
        self.sock.close()


def _system32() -> str:
    return os.path.join(os.environ.get("SYSTEMROOT", r"C:\Windows"), "System32")


def run_checks(probe_dir: str) -> dict:
    """真正的偵測（在偵測子程序裡跑）。回傳 {"hardening","reasons","checks"}"""
    r = {"hardening": "none", "reasons": [], "checks": {}}
    if not IS_WINDOWS:
        r["reasons"].append("不是 Windows")
        return r
    from nexus.sandbox import jobs
    sys32 = _system32()
    cmd, curl = os.path.join(sys32, "cmd.exe"), os.path.join(sys32, "curl.exe")
    os.makedirs(probe_dir, exist_ok=True)
    secret = os.path.join(probe_dir, f"apex-probe-{secrets.token_hex(6)}.txt")
    lst = None
    try:
        with open(secret, "w", encoding="ascii") as f:
            f.write("probe")
        lst = _Listener()
        with Container() as ac, jobs.Job(jobs.JobLimits(memory_bytes=256 << 20, cpu_percent=50, max_processes=4)) as job:
            # run：開得起來＋結束碼正確＋放得進 Job
            rc = ac.run(cmd, f'"{cmd}" /d /c exit 7', sys32, job=job)
            r["checks"]["run"] = rc == 7
            if rc != 7:
                r["reasons"].append(f"AppContainer 程序結束碼不對（{rc}）")
            # file：對照組（一般權限）要讀得到；AppContainer 裡要讀不到
            plain = subprocess.run([cmd, "/d", "/c", "type", secret], capture_output=True, creationflags=CREATE_NO_WINDOW,
                                   timeout=15).returncode
            boxed = ac.run(cmd, f'"{cmd}" /d /c type "{secret}"', sys32, job=job)
            if plain != 0:
                r["checks"]["file"] = "unknown"
                r["reasons"].append("對照組讀不到測試檔，沒辦法判斷")
            else:
                r["checks"]["file"] = "blocked" if boxed != 0 else "readable"
                if boxed == 0:
                    r["reasons"].append("AppContainer 裡還讀得到節點資料夾")
            # loopback：對照組要連得到；AppContainer 裡要連不到
            if not os.path.isfile(curl):
                r["checks"]["loopback"] = "unknown"
                r["reasons"].append("這台沒有 curl.exe，沒辦法確認本機連線有沒有被擋")
            else:
                url = f"http://127.0.0.1:{lst.port}/"
                subprocess.run([curl, "-s", "-o", "NUL", "--max-time", "5", url], capture_output=True,
                               creationflags=CREATE_NO_WINDOW, timeout=15)
                time.sleep(0.3)
                plain_hits = lst.hits
                crc = ac.run(curl, f'"{curl}" -s -o NUL --max-time 5 {url}', sys32, job=job)
                time.sleep(0.3)
                boxed_hits = lst.hits - plain_hits
                if plain_hits == 0:
                    r["checks"]["loopback"] = "unknown"
                    r["reasons"].append("對照組連不到測試埠，沒辦法判斷")
                elif boxed_hits:
                    r["checks"]["loopback"] = "reachable"
                    r["reasons"].append("AppContainer 裡還連得到 127.0.0.1")
                else:
                    r["checks"]["loopback"] = "blocked"
                    r["checks"]["loopback_curl_exit"] = crc
            if not job.wait_empty(5):
                r["checks"]["run"] = False
                r["reasons"].append("偵測程序結束後還有殘留")
    except ProbeError as e:
        r["reasons"].append(str(e))
    except Exception as e:                           # 偵測不能讓節點出事：任何錯誤都只是「不能用＋原因」
        r["reasons"].append(f"偵測失敗：{type(e).__name__}: {str(e)[:120]}")
    finally:
        if lst:
            lst.close()
        try:
            os.remove(secret)
        except OSError:
            pass
    c = r["checks"]
    if c.get("run") is True and c.get("file") == "blocked" and c.get("loopback") == "blocked" and not r["reasons"]:
        r["hardening"] = "appcontainer"
    return r


# ---------- 給節點呼叫：開偵測子程序、讀結果 ----------
def probe_command(probe_dir: str) -> list[str]:
    if getattr(sys, "frozen", False):
        return [sys.executable, "--nexus-probe-strong", probe_dir]
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return [sys.executable, "-I", "-c",
            f"import sys;sys.path.insert(0,{root!r});from nexus.sandbox.appcontainer import entry;entry()",
            "--nexus-probe-strong", probe_dir]


def clean(raw) -> dict:
    """偵測子程序的輸出不可信任到哪裡去（它是我們自己的程式，但可能當掉印一半）：只留格式對的欄位"""
    if not isinstance(raw, dict):
        return {"hardening": "none", "reasons": ["偵測結果格式不對"], "checks": {}}
    level = raw.get("hardening") if raw.get("hardening") in LEVELS else "none"
    reasons = [str(x)[:200] for x in (raw.get("reasons") or []) if isinstance(x, str)][:5]
    checks = {}
    src = raw.get("checks") if isinstance(raw.get("checks"), dict) else {}
    for k, v in list(src.items())[:8]:
        if isinstance(v, (bool, int)) or (isinstance(v, str) and len(v) <= 16):
            checks[str(k)[:32]] = v
    if level == "none" and not reasons:
        reasons = ["原因不明"]
    return {"hardening": level, "reasons": reasons, "checks": checks}


def probe_strong(probe_dir: str, timeout: float = 60.0) -> dict:
    """節點啟動時在背景呼叫一次。永遠回傳結果，不丟例外"""
    if not IS_WINDOWS:
        return {"hardening": "none", "reasons": ["不是 Windows"], "checks": {}}
    try:
        p = subprocess.run(probe_command(probe_dir), capture_output=True, timeout=timeout, stdin=subprocess.DEVNULL,
                           creationflags=CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired:
        return {"hardening": "none", "reasons": [f"偵測超過 {timeout:g} 秒沒結束"], "checks": {}}
    except OSError as e:
        return {"hardening": "none", "reasons": [f"開不了偵測程式：{type(e).__name__}"], "checks": {}}
    for line in reversed(p.stdout.decode("utf-8", "replace").splitlines()):
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict) and "hardening" in raw:
            return clean(raw)
    return {"hardening": "none", "reasons": [f"偵測程式沒有回報就結束了（結束碼 {p.returncode}）"], "checks": {}}


def entry(argv: list[str] | None = None) -> int:
    """命令列入口：--nexus-probe-strong <節點資料夾>；結果印成一行 JSON"""
    argv = list(sys.argv[1:] if argv is None else argv)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass
    if len(argv) < 2 or argv[0] != "--nexus-probe-strong":
        sys.stderr.write("用法：--nexus-probe-strong <節點資料夾>\n")
        return 64
    try:
        r = run_checks(argv[1])
    except Exception as e:
        r = {"hardening": "none", "reasons": [f"偵測失敗：{type(e).__name__}"], "checks": {}}
    sys.stdout.write(json.dumps(r, ensure_ascii=True) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(entry())

"""
Windows Job Object（ctypes，不加依賴）。

用法：
    with Job(JobLimits(memory_bytes=2 << 30, cpu_percent=50, max_processes=8)) as job:
        p = job.spawn([exe, "--nexus-run", workdir], cwd=workdir, env=env)
        ...
        job.terminate()          # 取消／逾時：整組（含孫程序）一次結束
        job.wait_empty(5)        # 確認 0 個殘留
    # 離開 with → CloseHandle → KILL_ON_JOB_CLOSE：就算節點程式當掉，下載子程序也會跟著結束

為什麼先「暫停建立 → 放進 Job → 才恢復」：如果先跑再放進 Job，子程序可能在放進去之前就先生出孫程序，
孫程序不在 Job 裡、不受限制、取消時也砍不到。
"""
import ctypes
import os
import platform
import subprocess
import sys
import time
from dataclasses import dataclass

IS_WINDOWS = sys.platform == "win32"

# ---- Win32 常數 ----
CREATE_SUSPENDED = 0x00000004
CREATE_NO_WINDOW = 0x08000000
JobObjectBasicAccountingInformation = 1
JobObjectBasicProcessIdList = 3
JobObjectExtendedLimitInformation = 9
JobObjectCpuRateControlInformation = 15
JOB_OBJECT_LIMIT_ACTIVE_PROCESS = 0x00000008
JOB_OBJECT_LIMIT_JOB_MEMORY = 0x00000200
JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION = 0x00000400
JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
JOB_OBJECT_CPU_RATE_CONTROL_ENABLE = 0x1
JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP = 0x4


class SandboxError(Exception):
    """沙箱建不起來或操作失敗；message 給使用者看"""


@dataclass(frozen=True)
class JobLimits:
    memory_bytes: int = 2 << 30          # D4：記憶體 2 GB（整組程序加起來）
    cpu_percent: int = 50                # D4：CPU 50%（整台電腦的比例，硬上限）
    max_processes: int = 8               # D4：最多 8 個程序（PyInstaller 開機器＋執行器＋ffmpeg…）

    def __post_init__(self):
        if not (64 << 20 <= self.memory_bytes <= 64 << 30):
            raise ValueError("memory_bytes 要在 64MB～64GB")
        if not (1 <= self.cpu_percent <= 100):
            raise ValueError("cpu_percent 要在 1～100")
        if not (1 <= self.max_processes <= 64):
            raise ValueError("max_processes 要在 1～64")


if IS_WINDOWS:
    from ctypes import wintypes

    class IO_COUNTERS(ctypes.Structure):
        _fields_ = [(n, ctypes.c_ulonglong) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                                                      "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

    class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_longlong), ("PerJobUserTimeLimit", ctypes.c_longlong),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD), ("SchedulingClass", wintypes.DWORD)]

    class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
        _fields_ = [("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION), ("IoInfo", IO_COUNTERS),
                    ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                    ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

    class JOBOBJECT_CPU_RATE_CONTROL_INFORMATION(ctypes.Structure):
        _fields_ = [("ControlFlags", wintypes.DWORD), ("CpuRate", wintypes.DWORD)]

    class JOBOBJECT_BASIC_ACCOUNTING_INFORMATION(ctypes.Structure):
        _fields_ = [("TotalUserTime", ctypes.c_longlong), ("TotalKernelTime", ctypes.c_longlong),
                    ("ThisPeriodTotalUserTime", ctypes.c_longlong), ("ThisPeriodTotalKernelTime", ctypes.c_longlong),
                    ("TotalPageFaultCount", wintypes.DWORD), ("TotalProcesses", wintypes.DWORD),
                    ("ActiveProcesses", wintypes.DWORD), ("TotalTerminatedProcesses", wintypes.DWORD)]

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _ntdll = ctypes.WinDLL("ntdll")
    _k32.CreateJobObjectW.restype = wintypes.HANDLE
    _k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    _k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _k32.QueryInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                               ctypes.POINTER(wintypes.DWORD)]
    _k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    _k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    _k32.CloseHandle.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
    _ntdll.NtResumeProcess.restype = ctypes.c_long


def _err(what):
    code = ctypes.get_last_error() if IS_WINDOWS else 0
    return SandboxError(f"{what}失敗（Windows 錯誤 {code}）")


class Job:
    def __init__(self, limits: JobLimits | None = None):
        if not IS_WINDOWS:
            raise SandboxError("沙箱只支援 Windows")
        self.limits = limits or JobLimits()
        self.handle = _k32.CreateJobObjectW(None, None)
        if not self.handle:
            raise _err("建立 Job Object ")
        try:
            self._set_limits()
        except Exception:
            self.close()
            raise
        self.procs: list[subprocess.Popen] = []

    # ---- 設定 ----
    def _set_limits(self):
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        b = info.BasicLimitInformation
        b.LimitFlags = (JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | JOB_OBJECT_LIMIT_DIE_ON_UNHANDLED_EXCEPTION
                        | JOB_OBJECT_LIMIT_ACTIVE_PROCESS | JOB_OBJECT_LIMIT_JOB_MEMORY)
        b.ActiveProcessLimit = self.limits.max_processes
        info.JobMemoryLimit = self.limits.memory_bytes
        if not _k32.SetInformationJobObject(self.handle, JobObjectExtendedLimitInformation,
                                            ctypes.byref(info), ctypes.sizeof(info)):
            raise _err("設定記憶體／程序數上限")
        cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        cpu.ControlFlags = JOB_OBJECT_CPU_RATE_CONTROL_ENABLE | JOB_OBJECT_CPU_RATE_CONTROL_HARD_CAP
        cpu.CpuRate = self.limits.cpu_percent * 100           # 單位是 1/100 %
        if not _k32.SetInformationJobObject(self.handle, JobObjectCpuRateControlInformation,
                                            ctypes.byref(cpu), ctypes.sizeof(cpu)):
            raise _err("設定 CPU 上限")

    # ---- 開程序 ----
    def spawn(self, args: list[str], *, cwd: str | None = None, env: dict | None = None, **kw) -> subprocess.Popen:
        """暫停建立 → 放進 Job → 恢復。參數一律陣列（不用 shell）。放不進 Job 就砍掉、丟 SandboxError（不降級）。"""
        if kw.get("shell"):
            raise SandboxError("不允許 shell=True")
        flags = kw.pop("creationflags", 0) | CREATE_SUSPENDED | CREATE_NO_WINDOW
        p = subprocess.Popen(list(args), cwd=cwd, env=env, creationflags=flags, **kw)
        h = int(p._handle)
        if not _k32.AssignProcessToJobObject(self.handle, h):
            e = _err("把程序放進 Job ")
            p.kill()
            p.wait(5)
            raise e
        if _ntdll.NtResumeProcess(h) != 0:
            self.terminate()
            raise SandboxError("恢復程序執行失敗")
        self.procs.append(p)
        return p

    # ---- 查詢 ----
    def process_ids(self) -> list[int]:
        """Job 裡現在還活著的程序 id（含孫程序）"""
        n = 256

        class PIDS(ctypes.Structure):
            _fields_ = [("NumberOfAssignedProcesses", wintypes.DWORD), ("NumberOfProcessIdsInList", wintypes.DWORD),
                        ("ProcessIdList", ctypes.c_size_t * n)]
        info = PIDS()
        if not _k32.QueryInformationJobObject(self.handle, JobObjectBasicProcessIdList, ctypes.byref(info),
                                              ctypes.sizeof(info), None):
            raise _err("查詢 Job 裡的程序")
        return [int(info.ProcessIdList[i]) for i in range(info.NumberOfProcessIdsInList)]

    def accounting(self) -> dict:
        a = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION()
        if not _k32.QueryInformationJobObject(self.handle, JobObjectBasicAccountingInformation, ctypes.byref(a),
                                              ctypes.sizeof(a), None):
            raise _err("查詢 Job 統計")
        return {"active": a.ActiveProcesses, "total": a.TotalProcesses, "terminated": a.TotalTerminatedProcesses,
                "cpu_seconds": (a.TotalUserTime + a.TotalKernelTime) / 1e7}

    def limits_now(self) -> dict:
        """讀回 Windows 實際套用的上限與尖峰用量（測試與「為什麼失敗」用）"""
        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        if not _k32.QueryInformationJobObject(self.handle, JobObjectExtendedLimitInformation, ctypes.byref(info),
                                              ctypes.sizeof(info), None):
            raise _err("查詢 Job 上限")
        cpu = JOBOBJECT_CPU_RATE_CONTROL_INFORMATION()
        ok = _k32.QueryInformationJobObject(self.handle, JobObjectCpuRateControlInformation, ctypes.byref(cpu),
                                            ctypes.sizeof(cpu), None)
        return {"flags": info.BasicLimitInformation.LimitFlags, "max_processes": info.BasicLimitInformation.ActiveProcessLimit,
                "memory_bytes": info.JobMemoryLimit, "peak_memory": info.PeakJobMemoryUsed,
                "cpu_percent": cpu.CpuRate / 100 if ok else None}

    def memory_limit_hit(self) -> bool:
        """尖峰用量到上限的 95% 以上 → 視為「超過記憶體上限」（Windows 是讓配置失敗，不會直接砍）"""
        n = self.limits_now()
        return n["peak_memory"] >= n["memory_bytes"] * 0.95

    # ---- 結束 ----
    def terminate(self, exit_code: int = 1):
        """一次結束 Job 裡所有程序（yt-dlp、ffmpeg、孫程序全部）"""
        if self.handle and not _k32.TerminateJobObject(self.handle, exit_code):
            raise _err("結束整組程序")

    def wait_empty(self, timeout: float = 10.0) -> bool:
        """等到 Job 裡 0 個程序；時間到還有就回 False（呼叫端要回報「有殘留程序」，不能說成功）"""
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if not self.process_ids():
                for p in self.procs:
                    try:
                        p.wait(0.1)
                    except subprocess.TimeoutExpired:
                        pass
                return True
            time.sleep(0.05)
        return not self.process_ids()

    def close(self):
        """關掉 Job handle：KILL_ON_JOB_CLOSE 會結束還沒結束的程序"""
        if self.handle:
            _k32.CloseHandle(self.handle)
            self.handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def _is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def probe() -> dict:
    """
    沙箱能力偵測（D6、規格 §10.2）：真的建一個 Job、設上限、跑一個小程序放進去再收掉。
    任何一步失敗 → isolation="none"＋原因；節點就不接下載任務（不偷偷不用沙箱照跑）。
    """
    r = {"isolation": "none", "reasons": [], "windows": platform.version() if IS_WINDOWS else platform.system(),
         "admin": False}
    if not IS_WINDOWS:
        r["reasons"].append("不是 Windows")
        return r
    r["admin"] = _is_admin()
    try:
        with Job(JobLimits(memory_bytes=256 << 20, cpu_percent=50, max_processes=2)) as job:
            p = job.spawn([sys.executable, "-c", "pass"] if not getattr(sys, "frozen", False)
                          else [os.environ.get("COMSPEC", "cmd.exe"), "/c", "exit 0"])
            p.wait(15)
            lim = job.limits_now()
            if lim["memory_bytes"] != 256 << 20 or lim["max_processes"] != 2:
                r["reasons"].append("上限沒有套用成功")
            if not job.wait_empty(5):
                r["reasons"].append("測試程序結束後還有殘留")
    except Exception as e:
        r["reasons"].append(f"建立沙箱失敗：{e}")
    if not r["reasons"]:
        r["isolation"] = "basic"
    return r

"""
節點資源偵測（規格 §12、06-V2 §4.1）。只用標準庫＋ctypes，不加新套件。
  Windows：GetSystemTimes（兩次取樣算 CPU 負載）、GlobalMemoryStatusEx
  其他（開發／CI）：/proc/stat、/proc/meminfo
這些數字是 Worker 自己報的：Master 只拿來挑選節點，**不拿來授權**（規格 25.3）。
"""
import os
import platform
import shutil
import sys


class ResourceProbe:
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self._last = None                   # (idle, total) 上一次 CPU 取樣

    # ---- CPU ----
    def _cpu_times(self):
        if sys.platform.startswith("win"):
            import ctypes
            from ctypes import wintypes
            idle, kernel, user = wintypes.FILETIME(), wintypes.FILETIME(), wintypes.FILETIME()
            if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
                return None
            f = lambda ft: (ft.dwHighDateTime << 32) | ft.dwLowDateTime   # noqa: E731
            return f(idle), f(kernel) + f(user)          # kernel 時間已包含 idle
        try:
            with open("/proc/stat") as fh:
                parts = [int(x) for x in fh.readline().split()[1:]]
            return parts[3] + parts[4], sum(parts)
        except Exception:
            return None

    def cpu_load(self) -> float | None:
        """距離上一次呼叫的平均 CPU 使用率（0～1）；第一次呼叫回 None"""
        now = self._cpu_times()
        prev, self._last = self._last, now
        if not now or not prev:
            return None
        di, dt = now[0] - prev[0], now[1] - prev[1]
        if dt <= 0:
            return None
        return round(min(1.0, max(0.0, 1 - di / dt)), 3)

    # ---- RAM ----
    @staticmethod
    def memory_mb() -> tuple[int | None, int | None]:
        if sys.platform.startswith("win"):
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong), ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong), ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong), ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong), ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(st)
            if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st)):
                return None, None
            return st.ullTotalPhys // 1048576, st.ullAvailPhys // 1048576
        try:
            info = {}
            with open("/proc/meminfo") as fh:
                for line in fh:
                    k, v = line.split(":", 1)
                    info[k] = int(v.split()[0])
            return info["MemTotal"] // 1024, info.get("MemAvailable", info["MemFree"]) // 1024
        except Exception:
            return None, None

    def snapshot(self) -> dict:
        total, avail = self.memory_mb()
        try:
            disk = shutil.disk_usage(self.data_dir).free // 1048576
        except OSError:
            disk = None
        out = {"cpu_count": os.cpu_count() or 1, "cpu_load": self.cpu_load(), "memory_total_mb": total,
               "memory_available_mb": avail, "disk_free_mb": disk,
               "platform": "windows" if sys.platform.startswith("win") else sys.platform[:16],
               "arch": (platform.machine() or "unknown")[:16].lower()}
        return {k: v for k, v in out.items() if v is not None}

"""
V3 沙箱第二層：檔案（ADR-6、差距分析 C2／C6／C8、AT-006、AT-024）。

  ・每個任務一個隨機、猜不到的工作資料夾：<root>/work/<32 位隨機>/{input,output,tmp}
  ・子程序只拿到白名單環境變數；使用者資料夾（APPDATA、USERPROFILE…）都指到工作資料夾，
    所以 yt-dlp 讀不到使用者自己的設定和 Cookie；token、AI 金鑰、下載器設定一律不傳
  ・成功後只把 output 裡「真的在 output 裡、不是連結、副檔名允許」的檔案搬到下載資料夾，
    檔名重新清理、真實路徑一定要在下載資料夾內、不覆蓋舊檔
  ・結束一定清理；刪不掉就回報，不假裝完成

誠實說明：資料夾權限用 Windows 預設（繼承），**不宣稱**能擋同一個帳號下的其他程式。
"""
import hashlib
import os
import secrets
import shutil
import stat
import sys
import time
import unicodedata

ALLOWED_EXT = (".mp4", ".mkv", ".webm", ".m4a")
MAX_NAME_CHARS = 150
_RESERVED = {"CON", "PRN", "AUX", "NUL", "CONIN$", "CONOUT$",
             *(f"COM{i}" for i in range(10)), *(f"LPT{i}" for i in range(10)),
             "COM¹", "COM²", "COM³", "LPT¹", "LPT²", "LPT³"}
_BAD_CHARS = set('<>:"/\\|?*')
# 子程序可以繼承的系統變數（其他全部不給）
_ENV_KEEP = ("SYSTEMROOT", "WINDIR", "PATHEXT", "COMSPEC", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS")


class WorkspaceError(Exception):
    """路徑／檔案檢查不通過；message 給使用者看"""

    def __init__(self, reason: str, code: str = "PATH_BLOCKED"):
        super().__init__(reason)
        self.reason, self.code = reason, code


def _real(p: str) -> str:
    return os.path.normcase(os.path.realpath(p))


def is_inside(child: str, parent: str) -> bool:
    """child 的真實路徑（解開 .. 和連結）是不是在 parent 裡面（不含 parent 本身）"""
    c, p = _real(child), _real(parent)
    try:
        return c != p and os.path.commonpath([c, p]) == p
    except ValueError:                              # 不同磁碟機
        return False


def _is_link(p: str) -> bool:
    return os.path.islink(p) or (hasattr(os.path, "isjunction") and os.path.isjunction(p))


def sanitize_filename(name: str, fallback: str = "video") -> str:
    """把網站給的標題變成安全的 Windows 檔名（只是檔名，不能含路徑）"""
    name = unicodedata.normalize("NFC", str(name or ""))
    name = " ".join(name.split())                   # 換行、Tab、連續空白 → 一個空白（先做，免得變成底線）
    name = "".join("_" if (ch in _BAD_CHARS or ord(ch) < 32 or ch == "\x7f") else ch for ch in name)
    name = name.strip(" .")                         # Windows 不准結尾是空白或點
    stem, ext = os.path.splitext(name)
    if len(stem) > MAX_NAME_CHARS:
        stem = stem[:MAX_NAME_CHARS].rstrip(" .")
    name = stem + ext
    if not stem or stem.split(".")[0].upper() in _RESERVED:
        name = f"{fallback}_{stem}{ext}" if stem else fallback + ext
    if name in ("", ".", ".."):
        name = fallback
    return name


def safe_env(ws: "Workspace", extra_path: list[str] | None = None) -> dict:
    """子程序的環境變數：白名單＋全部使用者資料夾指到工作資料夾"""
    env = {k: os.environ[k] for k in _ENV_KEEP if k in os.environ}
    sysroot = os.environ.get("SYSTEMROOT", r"C:\Windows") if sys.platform == "win32" else "/usr/bin"
    paths = [*(extra_path or []), os.path.join(sysroot, "System32") if sys.platform == "win32" else sysroot]
    if sys.platform != "win32":
        paths.append("/bin")
    env["PATH"] = os.pathsep.join(paths)
    for k in ("TEMP", "TMP", "TMPDIR"):
        env[k] = ws.tmp
    for k in ("APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME"):
        env[k] = ws.tmp
    env.update({"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONNOUSERSITE": "1", "VH_SANDBOX": "1"})
    return env


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class Workspace:
    def __init__(self, root: str):
        os.makedirs(root, exist_ok=True)
        self.root = os.path.abspath(root)
        base = os.path.join(self.root, "work")
        os.makedirs(base, exist_ok=True)
        for _ in range(5):
            path = os.path.join(base, secrets.token_hex(16))
            try:
                os.mkdir(path)                       # 已存在就丟錯（不重用別人的資料夾）
                break
            except FileExistsError:
                continue
        else:
            raise WorkspaceError("建不出工作資料夾", "WORKDIR")
        if not is_inside(path, base) or _is_link(path):
            raise WorkspaceError("工作資料夾位置不對", "WORKDIR")
        self.path = path
        self.input, self.output, self.tmp = (os.path.join(path, n) for n in ("input", "output", "tmp"))
        for d in (self.input, self.output, self.tmp):
            os.mkdir(d)

    # ---- 收成果 ----
    def outputs(self) -> tuple[list[str], list[str]]:
        """output 裡可以收的檔案、被拒絕的原因（連結、資料夾外、副檔名不允許）"""
        ok, rejected = [], []
        for dirpath, dirnames, filenames in os.walk(self.output, followlinks=False):
            for d in list(dirnames):
                if _is_link(os.path.join(dirpath, d)):
                    rejected.append(f"{d}：是連結，不收")
                    dirnames.remove(d)
            for fn in filenames:
                p = os.path.join(dirpath, fn)
                if _is_link(p):
                    rejected.append(f"{fn}：是連結，不收")
                elif not is_inside(p, self.output):
                    rejected.append(f"{fn}：不在工作資料夾裡")
                elif os.path.splitext(fn)[1].lower() not in ALLOWED_EXT:
                    rejected.append(f"{fn}：副檔名不允許")
                else:
                    ok.append(p)
        return sorted(ok), rejected

    def deliver(self, dest_dir: str, max_bytes: int | None = None) -> list[dict]:
        """把成果搬到下載資料夾；任何一個檔案不合規就整批不搬、丟 WorkspaceError"""
        dest_dir = os.path.abspath(dest_dir)
        if _is_link(dest_dir):
            raise WorkspaceError("下載資料夾不能是連結", "DEST")
        os.makedirs(dest_dir, exist_ok=True)
        files, rejected = self.outputs()
        if rejected:
            raise WorkspaceError("有不合規的檔案：" + "；".join(rejected[:3]), "OUTPUT_REJECTED")
        if not files:
            raise WorkspaceError("沒有下載到任何檔案", "NO_OUTPUT")
        total = sum(os.path.getsize(f) for f in files)
        if max_bytes is not None and total > max_bytes:
            raise WorkspaceError(f"檔案太大（{total} > 上限 {max_bytes} bytes）", "TOO_BIG")
        out = []
        for f in files:
            target = self.unique_target(dest_dir, sanitize_filename(os.path.basename(f)))
            digest = sha256_file(f)
            size = os.path.getsize(f)
            shutil.move(f, target)
            out.append({"name": os.path.basename(target), "size": size, "sha256": digest, "path": target})
        return out

    @staticmethod
    def unique_target(dest_dir: str, name: str) -> str:
        """下載資料夾裡的安全目標路徑；同名就加 (2)、(3)…，不覆蓋"""
        stem, ext = os.path.splitext(name)
        for i in range(1, 1000):
            cand = os.path.join(dest_dir, name if i == 1 else f"{stem} ({i}){ext}")
            if not is_inside(cand, dest_dir):
                raise WorkspaceError("檔名會跑到下載資料夾外面", "PATH_ESCAPE")
            if not os.path.lexists(cand):
                return cand
        raise WorkspaceError("同名檔案太多", "DEST")

    # ---- 清理 ----
    def cleanup(self, tries: int = 5) -> tuple[bool, str]:
        """刪掉整個工作資料夾（Windows 檔案剛關閉可能還鎖著，會重試）；回傳 (成功?, 錯誤)"""
        def _onexc(func, p, exc):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
            except OSError:
                pass
        err = ""
        for i in range(tries):
            try:
                if sys.version_info >= (3, 12):
                    shutil.rmtree(self.path, onexc=_onexc)
                else:
                    shutil.rmtree(self.path, onerror=_onexc)
            except FileNotFoundError:
                pass
            except OSError as e:
                err = f"{type(e).__name__}: {e}"
            if not os.path.lexists(self.path):
                return True, ""
            time.sleep(0.2 * (i + 1))
        return False, err or "資料夾還在"

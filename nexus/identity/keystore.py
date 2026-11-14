"""
私鑰保護：PEM 用隨機密碼加密存檔，密碼用 Windows DPAPI 保護（只有同一個 Windows 使用者解得開）。
非 Windows（CI 的 Linux 單元測試）：DPAPI 不可用 → 密碼檔用 0600 權限存，並在日誌警告保護等級較低。
私鑰永遠不以明文落地。
"""
import logging
import os
import secrets
import sys

log = logging.getLogger("nexus.keystore")


def _dpapi_protect(data: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    inp = BLOB(len(data), ctypes.cast(ctypes.create_string_buffer(data, len(data)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not crypt32.CryptProtectData(ctypes.byref(inp), "VideoHelper Nexus key", None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def _dpapi_unprotect(blob: bytes) -> bytes:
    import ctypes
    import ctypes.wintypes as wt

    class BLOB(ctypes.Structure):
        _fields_ = [("cbData", wt.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]

    crypt32, kernel32 = ctypes.windll.crypt32, ctypes.windll.kernel32
    inp = BLOB(len(blob), ctypes.cast(ctypes.create_string_buffer(blob, len(blob)), ctypes.POINTER(ctypes.c_char)))
    out = BLOB()
    if not crypt32.CryptUnprotectData(ctypes.byref(inp), None, None, None, None, 0, ctypes.byref(out)):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel32.LocalFree(out.pbData)


def protection_level() -> str:
    return "dpapi" if sys.platform == "win32" else "file-permissions"


class KeyStore:
    """管一個目錄下的 <name>.key（加密 PEM）＋ <name>.pw（DPAPI blob 或 0600 檔）"""

    def __init__(self, directory: str):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        if protection_level() != "dpapi":
            log.warning("這個平台沒有 DPAPI：私鑰密碼只靠檔案權限保護（Windows 正式版會用 DPAPI）")

    def _paths(self, name):
        return os.path.join(self.dir, f"{name}.key"), os.path.join(self.dir, f"{name}.pw")

    def exists(self, name: str) -> bool:
        k, p = self._paths(name)
        return os.path.isfile(k) and os.path.isfile(p)

    def save_private_key(self, name: str, key) -> None:
        from cryptography.hazmat.primitives import serialization
        password = secrets.token_bytes(32)
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.BestAvailableEncryption(password))
        k, p = self._paths(name)
        self._write(k, pem)
        self._write(p, _dpapi_protect(password) if protection_level() == "dpapi" else password)

    def load_private_key(self, name: str):
        from cryptography.hazmat.primitives import serialization
        k, p = self._paths(name)
        pw = open(p, "rb").read()
        password = _dpapi_unprotect(pw) if protection_level() == "dpapi" else pw
        return serialization.load_pem_private_key(open(k, "rb").read(), password=password)

    def key_pem_path_and_password(self, name: str):
        """給 ssl.SSLContext.load_cert_chain(keyfile=, password=) 用：檔案本身是加密的 PEM"""
        k, p = self._paths(name)
        pw = open(p, "rb").read()
        return k, (_dpapi_unprotect(pw) if protection_level() == "dpapi" else pw)

    def delete(self, name: str):
        for path in self._paths(name):
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    @staticmethod
    def _write(path: str, data: bytes):
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass

"""
兩種模式各自的設定檔（決策 3：獨立設定／日誌／生命週期）
  Master：%APPDATA%\\VideoHelper\\nexus\\master\\config.json
  Worker：%APPDATA%\\VideoHelper\\nexus\\worker\\config.json
有 schema_version；未知欄位保留並警告（不丟、不當命令）。寫檔用暫存檔＋取代，避免寫一半。
"""
import json
import logging
import os
import sys
import tempfile

log = logging.getLogger("nexus.config")
SCHEMA_VERSION = 1

MASTER_DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "enabled": False,                    # 預設關；使用者在 GUI 打開
    "bind_host": "127.0.0.1",            # 只能選本機網卡位址；0.0.0.0 要 bind_all_confirmed
    "port": 47331,
    "bind_all_confirmed": False,
    "hostname": "",                      # 伺服器憑證 SAN 用；空＝只放 IP
    "max_connections": 64,
    "max_payload_bytes": 65536,
    "device_cert_days": 90,
    "renew_before_days": 14,
    "supersede_overlap_hours": 24,
    "nonce_ttl_seconds": 600,
    "clock_skew_seconds": 300,
}
WORKER_DEFAULTS = {
    "schema_version": SCHEMA_VERSION,
    "master_url": "",                    # https://主機:埠
    "device_name": "",
    "heartbeat_seconds": 10,
    "reconnect_max_seconds": 300,
}


def nexus_root() -> str:
    if sys.platform.startswith("win") and os.environ.get("APPDATA"):
        base = os.path.join(os.environ["APPDATA"], "VideoHelper")
    else:
        base = os.path.join(os.path.expanduser("~"), ".video_helper")
    return os.environ.get("VH_NEXUS_ROOT") or os.path.join(base, "nexus")


def mode_dir(mode: str) -> str:
    assert mode in ("master", "worker")
    d = os.path.join(nexus_root(), mode)
    os.makedirs(d, exist_ok=True)
    return d


class Config:
    def __init__(self, mode: str, path: str | None = None):
        self.mode = mode
        self.defaults = MASTER_DEFAULTS if mode == "master" else WORKER_DEFAULTS
        self.path = path or os.path.join(mode_dir(mode), "config.json")
        self.data = dict(self.defaults)
        self.unknown = {}
        self.load()

    def load(self):
        if not os.path.isfile(self.path):
            return
        try:
            raw = json.load(open(self.path, encoding="utf-8"))
        except Exception as e:
            log.warning("設定檔讀不懂，改用預設值：%s", e)
            return
        if not isinstance(raw, dict):
            return
        ver = int(raw.get("schema_version") or 0)
        if ver > SCHEMA_VERSION:
            log.warning("設定檔 schema_version=%s 比程式新（%s），未知欄位會保留但不使用", ver, SCHEMA_VERSION)
        raw = self._migrate(raw, ver)
        for k, v in raw.items():
            if k in self.defaults:
                self.data[k] = v
            else:
                self.unknown[k] = v
        if self.unknown:
            log.warning("設定檔有不認識的欄位（保留、不使用）：%s", sorted(self.unknown))

    def _migrate(self, raw: dict, ver: int) -> dict:
        # 目前只有 v1；未來每個版本一段，升級前先備份
        if ver < 1:
            raw["schema_version"] = 1
        return raw

    def save(self):
        out = {**self.unknown, **self.data, "schema_version": SCHEMA_VERSION}
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path), prefix=".config-", suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def __getitem__(self, k):
        return self.data[k]

    def __setitem__(self, k, v):
        if k not in self.defaults:
            raise KeyError(f"不是已知的設定：{k}")
        self.data[k] = v

    def get(self, k, default=None):
        return self.data.get(k, default)

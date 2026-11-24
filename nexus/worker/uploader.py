"""
V4 節點把檔案傳回 Master（10-V4 §3）：
  ・先問 Master「這個檔案你收到哪幾段了」，只傳缺的（斷線、節點重開、Master 重開都接著傳）
  ・一段 8 MB、每段附 SHA-256；這段被說不對就重傳這段
  ・Master 空間不夠（D6）→ 等它說的秒數再試，檔案留著
  ・網路斷了就一直退避重試（最多等 60 秒一次）；要不要放棄由 Master 判斷（D7：24 小時）
  ・只傳「這台下載資料夾裡、大小對得上」的檔案：就算 Master 要求別的路徑也不會傳（防止被拿來偷檔案）
"""
import logging
import os
import time

from nexus.core.errors import NexusError
from nexus.sandbox.workspace import is_inside

log = logging.getLogger("nexus.worker.upload")
FATAL = ("UPLOAD_EXPIRED", "UPLOAD_NOT_ALLOWED", "CREDENTIAL_REVOKED", "CREDENTIAL_EXPIRED", "CREDENTIAL_MISMATCH",
         "DEVICE_NOT_ACTIVE", "TASK_NOT_FOUND", "TASK_NOT_ASSIGNED", "PROTOCOL_INCOMPATIBLE", "OUTPUT_REJECTED")
MAX_CHUNK_RETRIES = 3


class UploadAbort(Exception):
    """不用再傳了：code＝TRANSFER_FAILED 要回報失敗；其他（Master 已經放棄、被撤銷）安靜停下"""
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def check_files(files: list[dict], download_dir: str) -> str:
    """回傳問題（空字串＝可以傳）"""
    for f in files:
        p = f.get("path") or ""
        if not p or not os.path.isfile(p) or os.path.islink(p):
            return f"節點上找不到檔案：{f.get('name')}"
        if not is_inside(p, download_dir):
            return "檔案不在這台的下載資料夾裡，不傳"
        if os.path.getsize(p) != f.get("size"):
            return f"節點上的檔案大小變了：{f.get('name')}"
    return ""


class Uploader:
    def __init__(self, new_client, ctx, on_status=None):
        """new_client：() → NexusClient（每個傳回自己一條連線）；ctx：有 check()/sleep()/progress() 的任務環境"""
        self.new_client, self.ctx = new_client, ctx
        self.on_status = on_status or (lambda s: None)
        self.client = None
        self.sent = 0
        self.total = 0
        self.bps = 0.0
        self._t0, self._b0 = time.monotonic(), 0

    def _c(self):
        if self.client is None:
            self.client = self.new_client()
        return self.client

    def close(self):
        if self.client:
            self.client.close()
            self.client = None

    def _call(self, fn, passthrough=()):
        backoff = 1.0
        while True:
            self.ctx.check()
            try:
                return fn(self._c())
            except NexusError as e:
                if e.code in passthrough:
                    raise
                if e.code in FATAL:
                    raise UploadAbort(e.code, str(e)) from e
                if e.code == "MASTER_DISK_FULL":
                    self.on_status("disk_full")
                    log.info("Master 空間不夠，%s 秒後再試", e.retry_after or 300)
                    self.ctx.sleep(e.retry_after or 300)
                    continue
                wait = e.retry_after or backoff
            except Exception as e:                  # 斷線、逾時 → 換一條連線再試
                log.info("傳回連線問題：%s", type(e).__name__)
                self.close()
                wait = backoff
            self.on_status("retrying")
            self.ctx.sleep(min(60.0, wait))
            backoff = min(60.0, backoff * 2)

    def _pace(self, n: int):
        """D5：照 Master 的「下載限速」當上傳上限（0＝不限）"""
        if not self.bps:
            return
        self._b0 += n
        ahead = self._b0 / self.bps - (time.monotonic() - self._t0)
        if ahead > 0:
            self.ctx.sleep(ahead)

    def _report(self):
        if self.total:
            self.ctx.progress(round(min(1.0, self.sent / self.total), 3))

    def run(self, task_id: str, attempt_id: str, files: list[dict]):
        self.total = sum(f["size"] for f in files)
        try:
            for i, f in enumerate(files):
                self._one(task_id, attempt_id, i, f)
        finally:
            self.close()

    def _one(self, task_id, attempt_id, index, f):
        hash_retry = 0
        base = self.sent                                     # 前面檔案已經算進去的量
        while True:
            self.sent = base
            info = self._call(lambda c: c.upload_open(task_id, attempt_id, index))
            self.on_status("uploading")
            self.bps = float(info.get("rate_limit_mbps") or 0) * 1e6 / 8
            uid, cs, n_total = info["upload_id"], int(info["chunk_size"]), int(info["chunks_total"])
            if info.get("done"):
                self.sent += f["size"]
                self._report()
                return
            have = set(info.get("have") or [])
            self.sent += sum(min(cs, f["size"] - i * cs) for i in have)
            self._report()
            with open(f["path"], "rb") as fh:
                for n in range(n_total):
                    if n in have:
                        continue
                    self.ctx.check()
                    fh.seek(n * cs)
                    data = fh.read(cs)
                    for k in range(MAX_CHUNK_RETRIES + 1):
                        try:
                            self._call(lambda c, n=n, data=data, uid=uid: c.upload_chunk(uid, n, data),
                                       passthrough=("CHUNK_MISMATCH", "SIZE_MISMATCH"))
                            break
                        except NexusError as e:
                            if k == MAX_CHUNK_RETRIES:
                                raise UploadAbort("TRANSFER_FAILED", f"第 {n} 段一直傳不對（{e.code}）") from e
                    self.sent += len(data)
                    self._pace(len(data))
                    self._report()
            try:
                r = self._call(lambda c, uid=uid: c.upload_complete(uid),
                               passthrough=("HASH_MISMATCH", "UPLOAD_INCOMPLETE", "SIZE_MISMATCH"))
                log.info("檔案 %d 傳回完成", index)
                return r
            except NexusError as e:
                if e.code == "HASH_MISMATCH" and ((e.details or {}).get("final") or hash_retry >= 1):
                    raise UploadAbort("TRANSFER_FAILED", "整個檔案的 SHA-256 連續不對（節點上的檔案可能壞了）") from e
                if e.code == "HASH_MISMATCH":
                    hash_retry += 1
                continue                                     # Master 把沒收對的段清掉了 → 重新問一次、只傳缺的

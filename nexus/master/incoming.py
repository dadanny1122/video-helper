"""
V4 收檔（10-V4 §3、§5）：節點把下載好的影片一段一段傳回 Master。
收檔這一端**一律不相信節點**：
  ・檔名、大小、SHA-256 只認「節點回報、Master 驗過格式、存進資料庫」的那份；之後的請求不能改
  ・每段長度一定要等於「依宣告大小算出來」的長度；每段自己的 SHA-256 不對就不算收到
  ・半成品放 nexus\\master\\incoming\\（不在下載資料夾）；整檔 SHA-256 對了才搬進下載資料夾
  ・搬進去時檔名再清理一次、副檔名白名單、真實路徑一定在下載資料夾內、不覆蓋
  ・空間不夠就先不收（MASTER_DISK_FULL＋Retry-After），不會把節點那份弄丟
誠實標示：SHA-256 只保證「收到的跟節點送的一樣」，不保證影片本身安全。
"""
import hashlib
import logging
import os
import shutil
import threading
import uuid

from nexus.core.errors import NexusError
from nexus.core.models import utcnow
from nexus.sandbox.workspace import ALLOWED_EXT, Workspace, WorkspaceError, is_inside, sanitize_filename

log = logging.getLogger("nexus.incoming")
CHUNK_SIZE = 8 << 20                  # D5：一段 8 MB
SPACE_MARGIN = 1 << 30                # 空間要多留 1 GB
DISK_WAIT_SECONDS = 300               # D6：空間不夠 → 5 分鐘後再試
MAX_HASH_FAILURES = 2                 # 整檔雜湊不對：全部重傳一次，再錯就失敗
_READ = 1 << 20


def _bits(n: int) -> bytearray:
    return bytearray((n + 7) // 8)


def has(bitmap: bytes, i: int) -> bool:
    return bool(bitmap[i // 8] & (1 << (i % 8)))


def have_list(row: dict) -> list[int]:
    return [i for i in range(row["chunks_total"]) if has(row["have"], i)]


def received_bytes(row: dict) -> int:
    n, cs, size = row["chunks_total"], row["chunk_size"], row["size"]
    total = 0
    for i in range(n):
        if has(row["have"], i):
            total += min(cs, size - i * cs)
    return total


def chunk_length(row: dict, n: int) -> int:
    return min(row["chunk_size"], row["size"] - n * row["chunk_size"])


class Incoming:
    def __init__(self, db, root: str, dest_dir=None, free_bytes=None):
        """dest_dir：callable → 下載資料夾（電腦版會換成 ① 下載影片 那個資料夾）；free_bytes：測試用"""
        self.db = db
        self.root = root
        self.dest_dir = dest_dir or (lambda: os.path.join(os.path.dirname(root), "received"))
        self.free_bytes = free_bytes or (lambda p: shutil.disk_usage(p).free)
        self._locks = {}
        self._glock = threading.Lock()

    # ---------- 資料 ----------
    def _lock(self, uid: str) -> threading.Lock:
        with self._glock:
            return self._locks.setdefault(uid, threading.Lock())

    @staticmethod
    def _row(r):
        if not r:
            return None
        d = dict(r)
        d["have"] = bytes(d["have"])
        return d

    def get(self, upload_id: str, c=None):
        return self._row((c or self.db.conn()).execute("SELECT * FROM uploads WHERE upload_id=?", (upload_id,)).fetchone())

    def of_task(self, task_id: str, c=None) -> list[dict]:
        rows = (c or self.db.conn()).execute("SELECT * FROM uploads WHERE task_id=? ORDER BY file_index", (task_id,)).fetchall()
        return [self._row(r) for r in rows]

    def _update(self, c, upload_id: str, **f):
        f["updated_at"] = utcnow()
        c.execute(f"UPDATE uploads SET {', '.join(k + '=?' for k in f)} WHERE upload_id=?", (*f.values(), upload_id))

    def part_path(self, upload_id: str) -> str:
        return os.path.join(self.root, upload_id + ".part")

    # ---------- 任務進到 UPLOADING 時（派送層的交易裡）建好每個檔案一筆 ----------
    def prepare(self, c, task_id: str, device_id: str, files: list[dict]):
        for i, f in enumerate(files):
            ex = c.execute("SELECT upload_id FROM uploads WHERE task_id=? AND file_index=?", (task_id, i)).fetchone()
            if ex:
                continue
            n = max(1, (f["size"] + CHUNK_SIZE - 1) // CHUNK_SIZE)
            now = utcnow()
            c.execute("INSERT INTO uploads(upload_id, task_id, file_index, device_id, name, size, sha256, node_path, chunk_size,"
                      " chunks_total, have, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (str(uuid.uuid4()), task_id, i, device_id, f["name"], f["size"], f["sha256"], str(f.get("path") or "")[:1024],
                       CHUNK_SIZE, n, bytes(_bits(n)), "open", now, now))

    def reopen(self, c, task_id: str):
        """「重新傳回」：沒收完的重新開放（已收的段保留，接著傳）"""
        for r in self.of_task(task_id, c):
            if r["status"] != "done":
                self._update(c, r["upload_id"], status="open", hash_failures=0, note="")

    # ---------- 節點：開始（或接著）傳 ----------
    def check_space(self, row: dict):
        need = row["size"] - received_bytes(row) + SPACE_MARGIN
        os.makedirs(self.root, exist_ok=True)
        dest = self.dest_dir()
        os.makedirs(dest, exist_ok=True)
        if self.free_bytes(self.root) < need or self.free_bytes(dest) < row["size"] + SPACE_MARGIN:
            with self.db.tx() as c:
                self._update(c, row["upload_id"], note="disk_full")
            raise NexusError("MASTER_DISK_FULL", retry_after=DISK_WAIT_SECONDS,
                             details={"need_bytes": need, "wait_seconds": DISK_WAIT_SECONDS})
        if row["note"] == "disk_full":
            with self.db.tx() as c:
                self._update(c, row["upload_id"], note="")

    def open_for(self, row: dict) -> dict:
        if row["status"] == "failed":
            raise NexusError("UPLOAD_EXPIRED")
        if row["status"] != "done":
            self.check_space(row)
        return {"upload_id": row["upload_id"], "chunk_size": row["chunk_size"], "chunks_total": row["chunks_total"],
                "size": row["size"], "have": have_list(row), "done": row["status"] == "done"}

    # ---------- 節點：一段 ----------
    def write_chunk(self, row: dict, n: int, length: int, sha: str, stream) -> dict:
        """stream：請求的 body（還沒讀）。長度先檢查才讀；寫進 .part 的對應位置；這段雜湊對了才記成已收"""
        uid = row["upload_id"]
        if row["status"] != "open":
            raise NexusError("UPLOAD_NOT_ALLOWED", message="這個檔案現在不收（已收完或已放棄）")
        if not (isinstance(n, int) and 0 <= n < row["chunks_total"]):
            raise NexusError("CHUNK_MISMATCH", message="段號不對")
        want = chunk_length(row, n)
        if length != want:
            raise NexusError("SIZE_MISMATCH", details={"expected": want})
        if not (isinstance(sha, str) and len(sha) == 64):
            raise NexusError("CHUNK_MISMATCH", message="缺少這段的 SHA-256")
        os.makedirs(self.root, exist_ok=True)
        p = self.part_path(uid)
        with self._lock(uid):
            h = hashlib.sha256()
            mode = "r+b" if os.path.exists(p) else "w+b"
            got = 0
            with open(p, mode) as f:
                f.seek(n * row["chunk_size"])
                while got < length:
                    buf = stream.read(min(_READ, length - got))
                    if not buf:
                        break
                    h.update(buf)
                    f.write(buf)
                    got += len(buf)
            if got != length:
                raise NexusError("SIZE_MISMATCH", message="這段沒有收完整")
            if h.hexdigest() != sha.lower():
                raise NexusError("CHUNK_MISMATCH", message="這段的 SHA-256 不對，請重傳這段")
            with self.db.tx() as c:
                cur = self.get(uid, c)
                bm = bytearray(cur["have"])
                bm[n // 8] |= 1 << (n % 8)
                self._update(c, uid, have=bytes(bm))
        return {"received": n}

    # ---------- 節點：收完了 → 整檔驗證、搬進下載資料夾 ----------
    def complete(self, row: dict) -> dict:
        uid = row["upload_id"]
        if row["status"] == "done":
            return {"ok": True, "master_path": row["master_path"]}           # 重送的 complete（冪等）
        if row["status"] != "open":
            raise NexusError("UPLOAD_NOT_ALLOWED")
        missing = [i for i in range(row["chunks_total"]) if not has(row["have"], i)]
        if missing:
            raise NexusError("UPLOAD_INCOMPLETE", details={"missing": missing[:50], "missing_count": len(missing)})
        p = self.part_path(uid)
        with self._lock(uid):
            if not os.path.isfile(p) or os.path.getsize(p) != row["size"]:
                self._reset(uid, "半成品大小不對")
                raise NexusError("SIZE_MISMATCH", message="收到的大小跟宣告的不一樣，請重傳")
            h = hashlib.sha256()
            with open(p, "rb") as f:
                for buf in iter(lambda: f.read(_READ), b""):
                    h.update(buf)
            if h.hexdigest() != row["sha256"]:
                fails = row["hash_failures"] + 1
                if fails >= MAX_HASH_FAILURES:
                    self._discard_file(uid)
                    with self.db.tx() as c:
                        self._update(c, uid, status="failed", hash_failures=fails, have=bytes(_bits(row["chunks_total"])),
                                     note="hash")
                    raise NexusError("HASH_MISMATCH", message="整個檔案的 SHA-256 連續不對，放棄傳回", details={"final": True})
                self._reset(uid, "hash", hash_failures=fails)
                raise NexusError("HASH_MISMATCH", message="整個檔案的 SHA-256 不對，請全部重傳一次", details={"final": False})
            target = self._deliver(p, row["name"])
            with self.db.tx() as c:
                self._update(c, uid, status="done", master_path=target, note="")
        log.info("收到檔案 %s（%d bytes）", os.path.basename(target), row["size"])
        return {"ok": True, "master_path": target}

    def _deliver(self, part: str, name: str) -> str:
        dest = os.path.realpath(self.dest_dir())
        os.makedirs(dest, exist_ok=True)
        clean = sanitize_filename(name)
        if os.path.splitext(clean)[1].lower() not in ALLOWED_EXT:
            raise NexusError("OUTPUT_REJECTED", message="副檔名不允許")
        try:
            target = Workspace.unique_target(dest, clean)
        except WorkspaceError as e:
            raise NexusError("OUTPUT_REJECTED", message=str(e)) from e
        if not is_inside(target, dest):
            raise NexusError("OUTPUT_REJECTED", message="檔名會跳出下載資料夾")
        shutil.move(part, target)                    # 不同磁碟也能搬（會變成複製）
        return target

    def _reset(self, uid: str, note: str, **extra):
        self._discard_file(uid)
        with self.db.tx() as c:
            cur = self.get(uid, c)
            self._update(c, uid, have=bytes(_bits(cur["chunks_total"])), note=note, **extra)

    def _discard_file(self, uid: str) -> bool:
        p = self.part_path(uid)
        try:
            if os.path.exists(p):
                os.remove(p)
            return True
        except OSError as e:
            log.warning("刪不掉半成品 %s：%s", uid[:8], e)
            return False

    # ---------- 清理（取消／失敗／過期） ----------
    def discard_task(self, task_id: str, c=None, status: str = "failed") -> bool:
        ok = True
        for r in self.of_task(task_id, c):
            if r["status"] == "done":
                continue
            ok = self._discard_file(r["upload_id"]) and ok
            if c is not None:
                self._update(c, r["upload_id"], status=status, have=bytes(_bits(r["chunks_total"])))
            else:
                with self.db.tx() as cc:
                    self._update(cc, r["upload_id"], status=status, have=bytes(_bits(r["chunks_total"])))
        return ok

    def progress(self, task_id: str) -> dict | None:
        rows = self.of_task(task_id)
        if not rows:
            return None
        size = sum(r["size"] for r in rows)
        got = sum(r["size"] if r["status"] == "done" else received_bytes(r) for r in rows)
        return {"size": size, "received": got, "ratio": (got / size) if size else 1.0,
                "disk_full": any(r["note"] == "disk_full" for r in rows),
                "done": all(r["status"] == "done" for r in rows), "files": rows,
                "updated_at": max(r["updated_at"] for r in rows)}

"""
Adapter 允許清單（ADR-10、規格 §10.1／§13.3）。Master 與 Worker 共用同一份定義：
  ・Master 用它驗證參數、決定能不能重試
  ・Worker 用它決定接不接（版本主號要一樣）、怎麼執行
V2 只有三個測試 Adapter：不開子程序、不碰檔案、不連網。**永遠不會有 shell／python／powershell 這類 Adapter。**
"""
import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    type: type
    required: bool = True
    max_len: int | None = None
    min: int | None = None
    max: int | None = None
    enum: tuple | None = None
    check: object = None                 # 額外檢查：fn(value) → 問題字串或 None


@dataclass(frozen=True)
class RetryPolicy:
    """
    重試要等多久（APEX 借 Temporal 的設計：重試政策是資料，每個 Adapter 自己宣告）。
    第 n 次重試等 initial×backoff^(n-1) 秒，最多 max_seconds，再加減 jitter 比例的隨機量（避免一堆任務同時重來）。
    min_by_error：某些錯誤至少要等多久（例如被網站限流）。
    """
    initial_seconds: float = 10
    backoff: float = 2.0
    max_seconds: float = 600
    jitter: float = 0.2
    min_by_error: tuple = ()             # ((錯誤碼, 秒), ...)；frozen dataclass 不能放 dict

    def delay(self, retry_no: int, error_code: str = "", rng=random) -> float:
        """retry_no：第幾次重試（1 開始）。回傳要等的秒數"""
        if self.initial_seconds <= 0:
            base = 0.0
        else:
            base = min(self.max_seconds, self.initial_seconds * self.backoff ** max(0, retry_no - 1))
            base *= 1 + rng.uniform(-self.jitter, self.jitter)
        floor = dict(self.min_by_error).get(error_code, 0)
        return round(max(base, floor), 1)


NO_WAIT = RetryPolicy(initial_seconds=0)          # 測試任務：馬上重試（測試才不會變慢）


@dataclass(frozen=True)
class AdapterSpec:
    name: str
    version: str
    params: dict = field(default_factory=dict)
    retry_safe: bool = True
    idempotent: bool = True
    checkpoint_supported: bool = False
    side_effects: str = "none"
    default_runtime_seconds: int = 60
    max_runtime_seconds: int = 900
    description: str = ""
    validate: object = None              # 跨欄位檢查：fn(params) → 問題清單
    retry: RetryPolicy = field(default_factory=RetryPolicy)

    @property
    def major(self) -> str:
        return self.version.split(".")[0]


def _video_url_problem(v: str):
    from nexus.net.guard import NetBlocked, static_check
    try:
        static_check(v, kind="page")
        return None
    except NetBlocked as e:
        return f"url 不行：{e.reason}"


def _media_url_problem(v: str):
    from nexus.net.guard import NetBlocked, static_check
    try:
        static_check(v, kind="any")
        return None
    except NetBlocked as e:
        return f"media_url 不行：{e.reason}"


def _video_cross_check(params: dict) -> list[str]:
    """V3.1：瀏覽器交來的影片位址一定要跟網頁是同一個網站（Pornhub 網頁只能配 phncdn.com…）"""
    media = params.get("media_url")
    if not isinstance(media, str) or not isinstance(params.get("url"), str):
        return []
    import urllib.parse

    from nexus.net import sites
    a = sites.site_name(urllib.parse.urlsplit(params["url"]).hostname or "")
    b = sites.site_name(urllib.parse.urlsplit(media).hostname or "")
    return [] if a and a == b else ["影片位址和網頁不是同一個網站"]


VIDEO_QUALITIES = ("best", "1080", "720", "480")

ADAPTERS: dict[str, AdapterSpec] = {
    "test.echo": AdapterSpec(
        "test.echo", "1.0", {"text": Param(str, max_len=1000)},
        default_runtime_seconds=30, retry=NO_WAIT, description="回傳原文＋SHA-256（測完整流程）"),
    "test.sleep": AdapterSpec(
        "test.sleep", "1.0", {"seconds": Param(int, min=1, max=600)},
        default_runtime_seconds=660, retry=NO_WAIT, description="等 N 秒、每秒回報進度，可取消（測取消、逾時）"),
    "test.fail": AdapterSpec(
        "test.fail", "1.0", {"code": Param(str, enum=("ADAPTER_ERROR", "RESOURCE_LIMIT", "INVALID_INPUT"))},
        default_runtime_seconds=30, retry=NO_WAIT, description="固定失敗（測重試分類）"),
    # V3：節點在沙箱裡下載影片，檔案留在節點（D1）；只准 sites.json 的網站（D2）；D4 單部最長 2 小時
    "video.download": AdapterSpec(
        "video.download", "1.0",
        {"url": Param(str, max_len=2048, check=_video_url_problem),
         "quality": Param(str, required=False, enum=VIDEO_QUALITIES),
         # V3.1：瀏覽器已經拿到的影片真正位址（網站擋程式開網頁時用，例如 Pornhub 410）
         "media_url": Param(str, required=False, max_len=4096, check=_media_url_problem),
         "title": Param(str, required=False, max_len=200),
         # V4（協定 1.3）：下載完把檔案傳回 Master（D2；舊節點不認得這個參數 → 排程不會派給它）
         "return_to_master": Param(bool, required=False)},
        validate=_video_cross_check,
        retry_safe=True, idempotent=False, side_effects="network,files",
        default_runtime_seconds=7200, max_runtime_seconds=7200,
        # 網路問題：30 秒、1 分、2 分…最多 10 分鐘；被網站限流至少等 2 分鐘（馬上重來只會再被擋）
        retry=RetryPolicy(initial_seconds=30, backoff=2, max_seconds=600, min_by_error=(("RATE_LIMITED", 120),)),
        description="在節點的沙箱裡下載影片，存在節點那台電腦（可勾選傳回這台）"),
    # V4：測「傳回」用。節點在自己的下載資料夾寫一個固定內容的假影片檔（不連網、不開子程序），可以傳回 Master
    "test.file": AdapterSpec(
        "test.file", "1.0",
        {"size_mb": Param(int, min=1, max=2048), "return_to_master": Param(bool, required=False)},
        retry_safe=True, idempotent=False, side_effects="test-file", retry=NO_WAIT,
        default_runtime_seconds=300, max_runtime_seconds=3600,
        description="在節點寫一個 N MB 的測試檔（測傳回、續傳；不連網）"),
}
# 這些副作用不需要沙箱（只在節點自己的下載資料夾寫固定內容的檔案）
NO_SANDBOX_EFFECTS = ("none", "test-file")


def needs_sandbox(name: str) -> bool:
    """有副作用（連網、寫檔）的任務一定要在沙箱裡跑（V3 D6）"""
    spec = ADAPTERS.get(name)
    return bool(spec and spec.side_effects not in NO_SANDBOX_EFFECTS)


def get(name: str) -> AdapterSpec | None:
    return ADAPTERS.get(name)


def compatible(spec: AdapterSpec, version: str) -> bool:
    """同一個主版本才相容（AT-018）"""
    return isinstance(version, str) and version.split(".")[0] == spec.major


def check_params(spec: AdapterSpec, params) -> list[str]:
    """回傳問題清單（空＝OK）。不認得的欄位一律算錯（AT-005）"""
    if not isinstance(params, dict):
        return ["params 必須是物件"]
    problems = [f"不認得的參數：{k}" for k in sorted(set(params) - set(spec.params))]
    for k, p in spec.params.items():
        if k not in params:
            if p.required:
                problems.append(f"缺少參數：{k}")
            continue
        v = params[k]
        if (p.type is int and isinstance(v, bool)) or not isinstance(v, p.type):
            problems.append(f"{k} 型別不對")
            continue
        if p.max_len is not None and len(v) > p.max_len:
            problems.append(f"{k} 太長（最多 {p.max_len}）")
        if p.min is not None and v < p.min:
            problems.append(f"{k} 太小（最少 {p.min}）")
        if p.max is not None and v > p.max:
            problems.append(f"{k} 太大（最多 {p.max}）")
        if p.enum is not None and v not in p.enum:
            problems.append(f"{k} 只能是 {'/'.join(p.enum)}")
        if p.check is not None and (msg := p.check(v)):
            problems.append(msg)
    if not problems and spec.validate is not None:
        problems += spec.validate(params)
    return problems

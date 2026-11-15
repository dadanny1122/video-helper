"""
Adapter 允許清單（ADR-10、規格 §10.1／§13.3）。Master 與 Worker 共用同一份定義：
  ・Master 用它驗證參數、決定能不能重試
  ・Worker 用它決定接不接（版本主號要一樣）、怎麼執行
V2 只有三個測試 Adapter：不開子程序、不碰檔案、不連網。**永遠不會有 shell／python／powershell 這類 Adapter。**
"""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Param:
    type: type
    required: bool = True
    max_len: int | None = None
    min: int | None = None
    max: int | None = None
    enum: tuple | None = None


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

    @property
    def major(self) -> str:
        return self.version.split(".")[0]


ADAPTERS: dict[str, AdapterSpec] = {
    "test.echo": AdapterSpec(
        "test.echo", "1.0", {"text": Param(str, max_len=1000)},
        default_runtime_seconds=30, description="回傳原文＋SHA-256（測完整流程）"),
    "test.sleep": AdapterSpec(
        "test.sleep", "1.0", {"seconds": Param(int, min=1, max=600)},
        default_runtime_seconds=660, description="等 N 秒、每秒回報進度，可取消（測取消、逾時）"),
    "test.fail": AdapterSpec(
        "test.fail", "1.0", {"code": Param(str, enum=("ADAPTER_ERROR", "RESOURCE_LIMIT", "INVALID_INPUT"))},
        default_runtime_seconds=30, description="固定失敗（測重試分類）"),
}


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
    return problems

"""把日誌、稽核、錯誤訊息裡的秘密遮掉（AT-021）。寧可多遮，不可漏。"""
import re

_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "[PRIVATE KEY]"),
    (re.compile(r"(?i)(authorization\s*[:=]\s*)(bearer\s+)?[A-Za-z0-9._~+/=-]{8,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)\b(token|secret|password|passwd|api[_-]?key|registration_token|pair_code)\b(\s*[:=]\s*|\"\s*:\s*\")[^\s,;\"']{4,}"),
     r"\1\2[REDACTED]"),
    (re.compile(r"(?i)(cookie\s*[:=]\s*)[^\r\n]{4,}"), r"\1[REDACTED]"),
    (re.compile(r"(?i)(x-vh-[a-z-]+\s*[:=]\s*)[^\s,;]{4,}"), r"\1[REDACTED]"),
]


def mask(text) -> str:
    s = str(text)
    for pat, rep in _PATTERNS:
        s = pat.sub(rep, s)
    return s


def mask_dict(d: dict) -> dict:
    """遞迴遮罩 dict（key 命中就整個值蓋掉）"""
    out = {}
    for k, v in d.items():
        if re.search(r"(?i)token|secret|password|passwd|private|cookie|authorization|pair_code|csr", str(k)):
            out[k] = "[REDACTED]"
        elif isinstance(v, dict):
            out[k] = mask_dict(v)
        elif isinstance(v, (list, tuple)):
            out[k] = [mask_dict(x) if isinstance(x, dict) else mask(x) for x in v]
        else:
            out[k] = mask(v) if isinstance(v, str) else v
    return out

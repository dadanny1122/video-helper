"""英文版（v1.44.0，規格 docs/i18n/00-英文版規格.md）：跟 webui/i18n.js 同一份字典（webui/en.json）、同一套規則。

電腦版自己顯示的字（系統匣、Windows 通知、對話框、小視窗）用 tr()；送到網頁的字網頁那邊會自己翻。
"""
import json
import os
import re
import sys
import unicodedata

CJK = re.compile(r"[㐀-鿿豈-﫿]")
PUNCT = {"：": ": ", "（": " (", "）": ")", "，": ", ", "。": ". ", "、": ", ", "「": '"', "」": '"', "『": '"', "』": '"',
         "！": "!", "？": "?", "；": "; ", "｜": " | ", "・": " · ", "～": "–", "　": " ", "【": "[", "】": "]", "《": '"', "》": '"'}
SEPS = re.compile(r"(｜|・|；|。)")
LANGS = ("auto", "zh", "en")
_D = {"exact": {}, "patterns": []}
_PATS = []
LANG = "zh"


def _dict_path():
    return os.path.join(getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "webui", "en.json")


def load(path=None):
    global _D, _PATS
    with open(path or _dict_path(), encoding="utf-8") as f:
        _D = json.load(f)
    _split_lines(_D["exact"])
    pats = []
    for zh, en in _D.get("patterns", []):
        names = []
        body = ""
        for p in re.split(r"(\{\w+\})", zh):
            if re.fullmatch(r"\{\w+\}", p):
                names.append(p[1:-1])
                body += r"([\s\S]+?)" if "\n" in zh else r"([^\n]+?)"     # 樣板沒有換行就不跨行
            else:
                body += re.escape(p)
        lit = re.sub(r"\{\w+\}", "", zh)
        pats.append((len(lit), re.compile("^" + body + "$"), names, en, bool(CJK.search(lit))))
    pats.sort(key=lambda x: -x[0])         # 跟 JS 一樣：穩定排序、字多的先比
    _PATS = pats
    return _D


def _split_lines(ex):
    """多行的句子也拆成一行一行（中英行數一樣才拆）：別的訊息把它接在後面時也翻得到"""
    for zh, en in list(ex.items()):
        a, b = zh.split("\n"), en.split("\n")
        if len(a) > 1 and len(a) == len(b):
            for x, y in zip(a, b):
                if x.strip() and CJK.search(x) and x not in ex:
                    ex[x] = y


def system_lang():
    """Windows 顯示語言是中文 → zh，其他 → en"""
    try:
        if sys.platform == "win32":
            import ctypes
            return "zh" if (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x04 else "en"
        import locale
        loc = (locale.getlocale()[0] or os.environ.get("LANG", "")).lower()
        return "zh" if loc.startswith("zh") or loc.startswith("chinese") else "en"
    except Exception:
        return "zh"


def resolve(choice):
    return choice if choice in ("zh", "en") else system_lang()


def set_lang(choice):
    global LANG
    LANG = resolve(choice)
    if LANG == "en" and not _D["exact"]:
        try:
            load()
        except Exception:
            pass
    return LANG


def _fix(s):
    s = "".join(PUNCT.get(c, c) for c in s)
    return re.sub(r" {2,}", " ", s).replace("( ", "(")


def _edge(c):
    return c.isspace() or unicodedata.category(c)[0] in "PS"


def _pattern(s, glue=False):
    """glue＝只有標點的樣板（例如「{a}：{b}」），最後才試"""
    for _, rx, names, en, has_cjk in _PATS:
        if has_cjk == glue:
            continue
        m = rx.match(s)
        if m:
            v = {n: tr(m.group(i + 1)) for i, n in enumerate(names)}
            return re.sub(r"\{(\w+)\}", lambda x: v.get(x.group(1), ""), en)
    return None


def _core(s):
    ex = _D["exact"]
    return ex[s] if s in ex else _pattern(s)


def tr(s):
    """一整段字 → 英文（翻不出來的部分保留中文）。不管目前語言"""
    s = "" if s is None else str(s)
    if not CJK.search(s):
        return s
    r = _core(s)
    if r is not None:
        return r
    a, b = 0, len(s)
    while a < b and _edge(s[a]):
        a += 1
    while b > a and _edge(s[b - 1]):
        b -= 1
    if (a > 0 or b < len(s)) and a < b:
        for x, y in ((a, len(s)), (0, b), (a, b)):         # 前後的標點拿掉（只拿前面／只拿後面：括號要成對）
            r = _core(s[x:y]) if (x, y) != (0, len(s)) else None
            if r is not None:
                return _fix(s[:x]) + r + _fix(s[y:]).rstrip()
    if "\n" in s:                                             # 先一行一行翻
        return "\n".join(tr(x) for x in s.split("\n"))
    if SEPS.search(s):
        parts = [p for p in SEPS.split(s) if p != ""]
        if len(parts) > 1:
            return "".join(_fix(p) if p in ("｜", "・", "；", "。") else tr(p) for p in parts).strip()
    r = _pattern(s, glue=True)
    if r is None and (a > 0 or b < len(s)) and a < b:
        r = _pattern(s[a:b], glue=True)
        if r is not None:
            r = _fix(s[:a]) + r + _fix(s[b:]).rstrip()
    return s if r is None else r


def t(s, **kw):
    """電腦版自己顯示的字：英文模式才翻；kw 填進 {名字}（先翻再填，名字本身不翻）"""
    out = tr(s) if LANG == "en" else s
    return out.format(**kw) if kw else out

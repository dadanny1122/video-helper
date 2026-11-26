"""英文版字典檢查（v1.44.0，docs/i18n/00-英文版規格.md）：
找出新介面、電腦版送到網頁／系統匣／對話框的中文字，用 webui/i18n.py 的 tr() 翻一次，翻完還有中文的就列出來。

用法：python3 -I .github/i18n_check.py            → 有漏翻就失敗（CI 用）
      python3 -I .github/i18n_check.py --dump x.json → 把漏翻的寫成 JSON（加字典用）
f-string 和「字串 + 變數」會變成 {a}{b} 樣板；檢查時把空格填成 7。
"""
import ast
import json
import os
import re
import sys
from html.parser import HTMLParser

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from webui import i18n  # noqa: E402

CJK = re.compile(r"[㐀-鿿豈-﫿]")

# 電腦版：會出現在新介面、系統匣、Windows 通知、配對／AI 字幕小視窗、對話框的函式（原本的舊視窗 build_* 不翻：規格 L3）
APP_FUNCS = re.compile(r"^(_?web_|on_web|_web|notify$|start_tray|update_tray|hide_to_tray|on_tray|tray_|quit_warning|verify_self|_attest|_gh_json|"
                       r"component_report|smart_report|pair_browser|try_pair|unpair_browsers|subtitle_dialog|_subtitle_worker|on_sub_|"
                       r"make_subtitles|ensure_model|install_whisper|run_whisper|extract_audio|ai_translate|refresh_(bridge|ai|bl)_label|"
                       r"classify_error|finish_task|on_download_error|on_progress|node_enqueue|on_node_returned|add_notice|retry_failed|"
                       r"toggle_pause|on_update|on_close|import_settings|export_settings|import_backup|fmt_|quality_label|preview_filename|"
                       r"check_update|enqueue|start_batch|on_batch_done|open_batch|on_analyzed|on_remote_analyzed|work$|download$|"
                       r"parallel_download|run_download|play_file|open_path|on_quit|quit_app|ask_close|show_window|_install_setup|setup_step|"
                       r"clean_old_logs|clear_logs|reset_stats|unpair|stop_bridge|update_blocklist|test_ai|clear_ai_cache|reset_speed|"
                       r"smart_learn|due_retries|pump$|cancel$|has_subtitles|ai_classify|classify_domains|retry_wait|human_|describe_|"
                       r"site_of|bridge_status|_bridge|do_POST|do_GET|_reply|status_text|state_text|error_text|friendly|reason_text|"
                       r"_later_start|on_later_added|on_notify_failed|_sched_hint|_classic_lock_prompt)")
APP_SKIP_CONST = {"TIPS", "HOME_CARDS", "NAV_ITEMS",             # 只有舊視窗用
                  "HALLU_PHRASES", "HALLU_EXACT"}                # AI 字幕要刪掉的雜訊字，不是畫面上的字
NEXUS_ALL = ["nexus/tasks/scheduler.py", "nexus/tasks/states.py", "nexus/core/errors.py", "nexus/tasks/adapters.py",
             "nexus/worker/tray.py", "nexus/worker/agent.py",
             "nexus/display.py",            # S5：節點畫面的名稱、時間、大小（新介面和舊視窗共用）
             "vh_updater.py",               # v1.47.0 一鍵更新的錯誤原因（會顯示在首頁更新卡）
             "vh_extras.py"]                # v1.48.0 密碼鎖、定時下載、整理、通知手機的選項和訊息
NEXUS_GUI_EXTRA = {"nexus/worker/executor.py": r"^(sandbox_status)$", "nexus/sandbox/jobs.py": r"^(probe)$"}   # 沙箱不能用的原因          # 節點小視窗（--worker）也翻
NEXUS_GUI = {"nexus/gui/devices_tab.py": re.compile(r"^(_ago|_local|_isolation)$"),
             "nexus/gui/tasks_tab.py": re.compile(r"^(summary_of|fmt_size|local|_who|_download_result|_result_text|_why)$")}


def template(node):
    """字串運算式 → 樣板（變數變成 {a}{b}…）；不是字串就回 None"""
    names = iter("abcdefghijklmnop")
    out = []

    def add(n):
        if isinstance(n, ast.Constant) and isinstance(n.value, str):
            out.append(n.value)
        elif isinstance(n, ast.JoinedStr):
            for v in n.values:
                if isinstance(v, ast.Constant):
                    out.append(v.value)
                else:
                    out.append("{" + next(names) + "}")
        elif isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add):
            add(n.left)
            add(n.right)
        else:
            out.append("{" + next(names) + "}")
    add(node)
    return "".join(out)


def is_str(n):
    if isinstance(n, ast.Constant):
        return isinstance(n.value, str)
    if isinstance(n, ast.JoinedStr):
        return True
    return isinstance(n, ast.BinOp) and isinstance(n.op, ast.Add) and (is_str(n.left) or is_str(n.right))


class Collect(ast.NodeVisitor):
    def __init__(self, want, skip_const=()):
        self.want, self.skip_const, self.stack, self.found = want, set(skip_const), [], []

    def visit_FunctionDef(self, n):
        self.stack.append(n.name)
        self.generic_visit(n)
        self.stack.pop()
    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, n):
        self.stack.append("class:" + n.name)
        self.generic_visit(n)
        self.stack.pop()

    def visit_Assign(self, n):
        if not self.stack and any(isinstance(t, ast.Name) and t.id in self.skip_const for t in n.targets):
            return
        self.generic_visit(n)

    def active(self):
        fn = [s for s in self.stack if not s.startswith("class:")]
        return self.want(fn[-1] if fn else None)

    def visit_Expr(self, n):
        if isinstance(n.value, ast.Constant):        # 說明文字（docstring）
            return
        self.generic_visit(n)

    def visit_Call(self, n):
        f = n.func
        if isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id in ("log", "logging", "_log", "logger"):
            return
        if isinstance(f, ast.Attribute) and f.attr in ("info", "warning", "exception", "debug", "error") and \
                isinstance(f.value, ast.Name) and f.value.id.endswith("log"):
            return
        self.generic_visit(n)

    def visit_Raise(self, n):          # 例外訊息大多是給日誌看；會顯示的會另外被包在別的地方
        self.generic_visit(n)

    def generic_visit(self, n):
        for c in ast.iter_child_nodes(n):
            if is_str(c) and not isinstance(n, ast.BinOp):
                if self.active():
                    t = template(c)
                    if CJK.search(t):
                        self.found.append(t)
                if isinstance(c, (ast.Constant, ast.JoinedStr)):
                    continue
            self.visit(c)


def py_strings():
    out = []
    path = os.path.join(ROOT, "video_helper_app.py")
    c = Collect(lambda fn: fn is None or bool(APP_FUNCS.match(fn)), APP_SKIP_CONST)
    c.visit(ast.parse(open(path, encoding="utf-8").read()))
    out += [("video_helper_app.py", s) for s in c.found]
    for f in NEXUS_ALL:
        c = Collect(lambda fn: True)
        c.visit(ast.parse(open(os.path.join(ROOT, f), encoding="utf-8").read()))
        out += [(f, s) for s in c.found]
    for f, rx in {**NEXUS_GUI, **{k: re.compile(v) for k, v in NEXUS_GUI_EXTRA.items()}}.items():
        c = Collect(lambda fn, rx=rx: fn is None or bool(rx.match(fn)))
        c.visit(ast.parse(open(os.path.join(ROOT, f), encoding="utf-8").read()))
        out += [(f, s) for s in c.found]
    return out


class H(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found, self.skip = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        for k, v in attrs:
            if k in ("placeholder", "title", "aria-label") and v and CJK.search(v):
                self.found.append(v)

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.skip -= 1

    def handle_data(self, d):
        if not self.skip and CJK.search(d) and d.strip():
            self.found.append(d.strip())

    def handle_comment(self, d):
        pass


JS_STR = re.compile(r"'((?:[^'\\\n]|\\.)*)'|\"((?:[^\"\\\n]|\\.)*)\"|`((?:[^`\\]|\\.)*)`", re.S)


def js_strings(text):
    """app.js 的字串（`…${x}…` 變成 {a}；`${` 裡面的字串另外再找）"""
    text = re.sub(r"(?m)^\s*//.*$|\s//\s.*$", "", text)
    out = []

    def tmpl(s):
        names = iter("abcdefghijklmnop")
        res, i, parts = "", 0, []
        while i < len(s):
            if s.startswith("${", i):
                depth, j = 1, i + 2
                while j < len(s) and depth:
                    depth += {"{": 1, "}": -1}.get(s[j], 0)
                    j += 1
                parts.append(s[i + 2:j - 1])
                res += "{" + next(names) + "}"
                i = j
            else:
                res += s[i]
                i += 1
        return res, parts

    def scan(t):
        for m in JS_STR.finditer(t):
            s = next(g for g in m.groups() if g is not None)
            if m.group(3) is not None:
                s, inner = tmpl(s.replace("\\n", "\n"))
                for x in inner:
                    scan(x)
                # 樣板裡的 HTML 標籤拿掉，只留看得到的字
                for piece in re.split(r"<[^>]*>", s):
                    piece = piece.strip()
                    if CJK.search(piece):
                        out.append(piece)
                for a in re.findall(r'(?:title|placeholder)="([^"]*)"', s):
                    if CJK.search(a):
                        out.append(a)
            elif CJK.search(s):
                s = s.replace("\\n", "\n").replace("\\'", "'")
                for piece in re.split(r"<[^>]*>", s) if "<" in s else [s]:
                    if CJK.search(piece):
                        out.append(piece.strip() if "<" in s else piece)
    scan(text)
    return out


def web_strings():
    d = os.path.join(ROOT, "webui")
    h = H()
    h.feed(open(os.path.join(d, "index.html"), encoding="utf-8").read())
    out = [("index.html", s) for s in h.found]
    out += [("app.js", s) for s in js_strings(open(os.path.join(d, "app.js"), encoding="utf-8").read())]
    return out


ALLOW = ("繁體中文", ".雙語.srt", "語言", "🌐 中文")        # 英文版也照樣顯示的中文（語言選單、真的檔名）


def left_cjk(s):
    for w in ALLOW:
        s = s.replace(w, "")
    return CJK.search(s)


def sample(s):
    return re.sub(r"\{\w*\}", "7", s)


def main():
    d = i18n.load()
    ignore = set(d.get("ignore", []))
    seen, missing, allstr = set(), [], web_strings() + py_strings()
    for src, s in allstr:
        if s in seen or s in ignore or "|" in s or s.startswith("把下面的影片字幕翻成"):    # 正規表示式、送給 AI 的指令不翻
            continue
        seen.add(s)
        if left_cjk(i18n.tr(sample(s))):
            missing.append((src, s))
    # 「字串 + 變數」拆出來的片段：整句翻得出來就不用單獨翻
    whole = [s for s in seen if "{" in s and not left_cjk(i18n.tr(sample(s)))]
    missing = [(src, s) for src, s in missing if "{" in s or not any(s in w for w in whole)]
    print(f"檢查 {len(seen)} 句，漏翻 {len(missing)} 句")
    if "--dump" in sys.argv:
        json.dump([s for _, s in missing], open(sys.argv[sys.argv.index("--dump") + 1], "w", encoding="utf-8"), ensure_ascii=False, indent=0)
    for src, s in missing[:2000]:
        print(f"  [{src}] {s!r}")
    # 字典本身：樣板的 {名字} 中英要一樣
    bad = [(zh, en) for zh, en in i18n._D["patterns"] if sorted(re.findall(r"\{\w+\}", zh)) != sorted(re.findall(r"\{\w+\}", en))]
    bad += [(zh, en) for zh, en in i18n._D["exact"].items() if left_cjk(en)]
    for zh, en in bad:
        print(f"  ❌ 字典有問題：{zh!r} → {en!r}")
    # JS（webui/i18n.js）和 Python 翻出來要一模一樣
    diff = js_parity([sample(x) for x in sorted(seen)] + EXTRA)
    for zh, py, js in diff[:30]:
        print(f"  ❌ JS 和 Python 不一樣：{zh!r}\n      py={py!r}\n      js={js!r}")
    print(f"JS／Python 對照 {len(seen) + len(EXTRA)} 句，不一樣 {len(diff)} 句")
    bad_script = check_script()
    sys.exit(1 if missing or bad or diff or bad_script else 0)


SCRIPT = os.path.join(ROOT, "video-helper.user.js")
DICT_RE = re.compile(r"/\*I18N_EN\*/(.*?)/\*END_I18N_EN\*/", re.S)


def check_script():
    """瀏覽器腳本（規格 E2）：字典寫在腳本裡（單一檔案）。抓腳本裡的中文 → 用腳本的字典翻 → 不能剩中文；JS 和 Python 結果要一樣"""
    import tempfile
    src = open(SCRIPT, encoding="utf-8").read()
    d = json.loads(DICT_RE.search(src).group(1))
    tmp = os.path.join(tempfile.mkdtemp(), "script-en.json")
    json.dump(d, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
    i18n.load(tmp)
    body = DICT_RE.sub("", src[src.index("==/UserScript=="):])
    body = re.sub(r"console\.(log|warn|error|info)\([^\n]*", "", body)
    ignore = set(d.get("ignore", []))
    seen, missing = [], []
    for s in js_strings(body):
        if s in seen or s in ignore:
            continue
        seen.append(s)
        if left_cjk(i18n.tr(sample(s))):
            missing.append(s)
    whole = [s for s in seen if "{" in s and not left_cjk(i18n.tr(sample(s)))]
    missing = [s for s in missing if "{" in s or not any(s in w for w in whole)]
    print(f"腳本：檢查 {len(seen)} 句，漏翻 {len(missing)} 句")
    for s in missing:
        print(f"  [腳本] {s!r}")
    # 腳本裡的翻譯引擎（I18N-ENGINE-START～END）和 Python 一樣嗎
    eng = src[src.index("// I18N-ENGINE-START"):src.index("// I18N-ENGINE-END")]
    items = [sample(x) for x in seen] + EXTRA_SCRIPT
    diff = js_parity(items, eng + "\nconst I18N = { tr: vhTr };\n")
    for zh, py, js in diff[:20]:
        print(f"  ❌ 腳本 JS 和 Python 不一樣：{zh!r}\n      py={py!r}\n      js={js!r}")
    print(f"腳本 JS／Python 對照 {len(items)} 句，不一樣 {len(diff)} 句")
    i18n.load()
    return missing or diff


EXTRA_SCRIPT = ["🛡 已攔截 12 次｜省下 30 個廣告請求", "2 分 5 秒", "❌ 載入失敗：網路錯誤", "開始播放：約 3 秒（依你過去 4 次校正）", "網速：12.3 Mbps（實測）"]


EXTRA = ["⏸ 已暫停（3 部等待中）", "下載中 45%（還有 2 部）", "下載中 1・排隊 2・暫停 1・速度上限 7 Mbps・讀取網址中…", "心跳 3 秒前",
         "書房電腦\n指紋 0011\n來源 1.2.3.4\n\n核准後這台節點會拿到憑證、可以跟 Master 心跳。\n請先核對節點畫面上的 Master 指紋前 8 碼。",
         "下載完成：某某：影片", "✅ 執行中（127.0.0.1:47321）｜已配對 1 個瀏覽器", "完全沒翻譯的句子", "  前後有空白的完成  ", "🧹 清掉已結束"]


def js_parity(items, engine=None):
    import shutil
    import subprocess
    node = shutil.which("node")
    if not node:
        print("（沒有 node，跳過 JS 對照）")
        return []
    from webui import en_json
    js = engine or open(os.path.join(ROOT, "webui", "i18n.js"), encoding="utf-8").read().replace("/*EN*/{ exact: {}, patterns: [] }", en_json(), 1)
    js += "\nconst items = JSON.parse(require('fs').readFileSync(0, 'utf8'));\nprocess.stdout.write(JSON.stringify(items.map((s) => I18N.tr(s))));"
    r = subprocess.run([node, "-e", js], input=json.dumps(items), capture_output=True, text=True, encoding="utf-8")
    if r.returncode:
        print(r.stderr[-2000:])
        return [("(node 出錯)", "", r.stderr[-200:])]
    out = json.loads(r.stdout)
    return [(zh, i18n.tr(zh), j) for zh, j in zip(items, out) if i18n.tr(zh) != j]


if __name__ == "__main__":
    main()

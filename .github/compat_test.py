"""S3 相容性矩陣（docs/infra/01-S3相容性矩陣規格.md）
一邊更新、另一邊還沒更新時，會不會壞？發布前自動試：
  C1 新電腦版 × 上一版腳本        C2 上一版電腦版 × 新腳本
  C3 新 Master × 上一版 Worker    C4 腳本裡「舊電腦版功能對照表」（含跟歷史版本對答案）
「上一版」＝比現在版本小的最近一個 tag（腳本 userscript-v*、電腦版 v*）。
用法：python3 -I .github/compat_test.py [--desktop v1.40.0] [--script userscript-v3.20.0] [--only c1,c3]
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PY = os.environ.get("PYTHON", sys.executable)


def git(*a):
    return subprocess.run(["git", "-C", ROOT, *a], capture_output=True, text=True, encoding="utf-8").stdout


def vtuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", s)[:3])


def notice(t, m):
    print(f"::notice title={t}::{m}", flush=True)


def error(t, m):
    print(f"::error title={t}::{m}", flush=True)


def prev_tag(prefix, current):
    tags = [t for t in git("tag", "-l", prefix + "*").split() if re.fullmatch(re.escape(prefix) + r"\d+\.\d+\.\d+", t)]
    older = sorted((t for t in tags if vtuple(t[len(prefix):]) < vtuple(current)), key=lambda t: vtuple(t[len(prefix):]))
    return older[-1] if older else None


def run(cmd, title, timeout=300):
    print(f"::group::{title}", flush=True)
    try:
        p = subprocess.run(cmd, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=dict(os.environ, PYTHON=PY))
        out = (p.stdout or "") + (p.stderr or "")
        code = p.returncode
    except subprocess.TimeoutExpired as e:
        out, code = str(e.stdout or "") + "\n等太久", 124
    print(out[-6000:], flush=True)
    print("::endgroup::", flush=True)
    return code == 0, out


def legacy_history(script_src):
    """表上每一格的版本，要等於歷史上第一個真的有這個功能／網站的電腦版 tag"""
    marks = {"media": "bridge.on_enqueue(ticket, url, h, media", "classify": '"/classify"'}
    feats = dict(re.findall(r"(\w+): '(\d+\.\d+\.\d+)'", re.search(r"LEGACY_FEATURES = \{([^}]*)\}", script_src).group(1)))
    sites = dict(re.findall(r"'([\w.-]+)': '(\d+\.\d+\.\d+)'", re.search(r"LEGACY_SITES = \{([^}]*)\}", script_src).group(1)))
    tags = sorted((t for t in git("tag", "-l", "v*").split() if re.fullmatch(r"v\d+\.\d+\.\d+", t)), key=vtuple)
    src = {}

    def first(marker):
        for i, t in enumerate(tags):
            if t not in src:
                src[t] = git("show", f"{t}:video_helper_app.py")
            if any(m in src[t] for m in marker):
                return (tags[i - 1] if i else None), t
        return None, None

    bad, ok = [], []
    for name, ver in [*feats.items(), *sites.items()]:
        marker = [marks[name]] if name in marks else ([f"'{name}'", f'"{name}"'] if "." in name else None)
        if not marker:                     # enqueue／status＝一開始就有
            continue
        before, t = first(marker)
        good = t and vtuple(ver) <= vtuple(t) and (before is None or vtuple(ver) > vtuple(before))
        (ok if good else bad).append(f"{name} 表上 {ver}、歷史上第一次出現在 {t or '找不到'}")
    return ok, bad


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--desktop", help="舊電腦版的 tag（預設：上一版）")
    ap.add_argument("--script", help="舊腳本的 tag（預設：上一版）")
    ap.add_argument("--only", default="c1,c2,c3,c4")
    a = ap.parse_args()
    only = set(a.only.lower().split(","))

    app_ver = re.search(r'^VERSION = "([^"]+)"', open(os.path.join(ROOT, "video_helper_app.py"), encoding="utf-8").read(), re.M).group(1)
    new_script = open(os.path.join(ROOT, "video-helper.user.js"), encoding="utf-8").read()
    us_ver = re.search(r"@version\s+(\S+)", new_script).group(1)
    old_desktop = a.desktop or prev_tag("v", app_ver)
    old_script = a.script or prev_tag("userscript-v", us_ver)
    notice("S3 相容性", f"新：電腦版 v{app_ver}、腳本 v{us_ver}｜舊：電腦版 {old_desktop or '—'}、腳本 {old_script or '—'}")

    tmp = tempfile.mkdtemp(prefix="vh-s3-")
    results = []                                                  # (代號, ok, 說明)
    tree = None
    if old_desktop and only & {"c2", "c3"}:
        tree = os.path.join(tmp, "old-desktop")
        subprocess.run(["git", "-C", ROOT, "worktree", "add", "-q", "--detach", tree, old_desktop], check=True)
    try:
        if "c1" in only:
            if not old_script:
                results.append(("C1", None, "沒有上一版腳本的 tag，跳過"))
            else:
                f = os.path.join(tmp, "old.user.js")
                with open(f, "w", encoding="utf-8") as fh:
                    fh.write(git("show", f"{old_script}:video-helper.user.js"))
                ok, out = run(["node", os.path.join(HERE, "compat_test.js"), "browser", f, ROOT, "C1 新電腦版×舊腳本"], "C1")
                results.append(("C1", ok, f"電腦版 v{app_ver} × {old_script}"))
        if "c2" in only:
            if not tree:
                results.append(("C2", None, "沒有上一版電腦版的 tag，跳過"))
            else:
                ok, out = run(["node", os.path.join(HERE, "compat_test.js"), "browser", os.path.join(ROOT, "video-helper.user.js"),
                               tree, "C2 舊電腦版×新腳本"], "C2")
                results.append(("C2", ok, f"{old_desktop} × 腳本 v{us_ver}"))
        if "c3" in only:
            if not tree:
                results.append(("C3", None, "沒有上一版電腦版的 tag，跳過"))
            elif not os.path.isdir(os.path.join(tree, "nexus")):
                results.append(("C3", None, f"{old_desktop} 還沒有 Nexus，跳過"))
            else:
                ok, out = run([PY, "-I", os.path.join(HERE, "compat_nexus.py"), "master", tree], "C3", timeout=400)
                last = next((json.loads(x) for x in reversed(out.splitlines()) if x.startswith("{")), {})
                results.append(("C3", ok, f"Master nexus {last.get('master')}（協定 {last.get('master_protocol')}）× "
                                          f"{old_desktop} 的 Worker nexus {last.get('worker')}（協定 {last.get('worker_protocol')}）"
                                          + ("：配對→核准→心跳→回聲任務→撤銷 都 OK" if ok else f"：{last.get('error', '失敗')}")))
        if "c4" in only:
            ok, out = run(["node", os.path.join(HERE, "compat_test.js"), "legacy", os.path.join(ROOT, "video-helper.user.js"), ROOT, "C4 功能對照表"], "C4")
            good, bad = legacy_history(new_script)
            for b in bad:
                error("C4 跟歷史版本對答案", "❌ " + b)
            results.append(("C4", ok and not bad, f"情境測試{'通過' if ok else '失敗'}；歷史對答案 {len(good)} 項對、{len(bad)} 項錯"))
    finally:
        if tree:
            subprocess.run(["git", "-C", ROOT, "worktree", "remove", "--force", tree], capture_output=True)
        shutil.rmtree(tmp, ignore_errors=True)

    for code, ok, msg in results:
        (error if ok is False else notice)(f"S3 {code}", ("✅ " if ok else "⏭ " if ok is None else "❌ ") + msg)
    sys.exit(1 if any(ok is False for _, ok, _ in results) else 0)


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()

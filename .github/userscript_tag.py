"""瀏覽器腳本版本 tag（架構稽核 S2，docs/infra/00-架構稽核與改善計畫.md）

每個 @version 一個附註 tag：userscript-v3.23.1 → 指向含這版腳本的 commit；tag 說明寫檔名、大小、SHA-256、commit。
舊版可以從 https://raw.githubusercontent.com/<repo>/userscript-v3.23.1/video-helper.user.js 下載，再用 tag 說明裡的指紋驗證。
安裝來源、Greasy Fork、電腦版 URL_SCRIPT、自動更新都不變（還是 raw main）。

  python -I .github/userscript_tag.py plan <commit>        → 印 action=create|skip，版本衝突就失敗（exit 1）
  python -I .github/userscript_tag.py create <commit>      → 建附註 tag 並推上去（已存在就失敗，絕不覆蓋）
  python -I .github/userscript_tag.py verify <tag>         → 重新驗證：tag 名＝@version、指紋＝tag 裡的檔、說明的 commit＝tag 指的 commit
  python -I .github/userscript_tag.py verify-all [--download]  → 每個 userscript-v* 都驗；--download 再從 raw 網址下載核對
  python -I .github/userscript_tag.py backfill [--push]   → 幫歷史上每個版本補 tag（每版第一個出現的 commit）
      ⚠ 要在自己電腦上用自己的 git 權限跑：GitHub 不准 workflow 的權限把 tag 放到 workflow 檔跟現在不一樣的舊 commit
  python -I .github/userscript_tag.py selftest             → 在暫存 git repo 走一次所有規則
"""
import hashlib
import os
import re
import subprocess
import sys
import tempfile
import urllib.request

FILE = "video-helper.user.js"
PREFIX = "userscript-v"
REPO = os.environ.get("GITHUB_REPOSITORY", "dadanny1122/video-helper")
GIT_DIR = [os.path.dirname(os.path.dirname(os.path.abspath(__file__)))]


def git(*args, check=True, binary=False):
    r = subprocess.run(["git", "-C", GIT_DIR[0], *args], capture_output=True, check=False)
    if check and r.returncode:
        raise SystemExit(f"git {' '.join(args)} 失敗：{r.stderr.decode('utf-8', 'replace').strip()}")
    return r.stdout if binary else r.stdout.decode("utf-8", "replace").strip()


def blob(rev):
    """git 裡存的內容（不受 Windows 換行影響；就是使用者從 raw 網址下載到的）"""
    r = subprocess.run(["git", "-C", GIT_DIR[0], "show", f"{rev}:{FILE}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def version_of(data):
    m = re.search(rb"^// @version\s+(\S+)", data or b"", re.M)
    return m.group(1).decode() if m else None


def vkey(v):
    return tuple(int(x) if x.isdigit() else 0 for x in re.split(r"[.\-]", v))


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def tags():
    out = git("tag", "-l", PREFIX + "*")
    return [t for t in out.splitlines() if t]


def tag_commit(tag):
    return git("rev-list", "-n1", tag)


def tag_message(tag):
    return git("tag", "-l", "--format=%(contents)", tag)


def fields(msg):
    return dict(re.findall(r"^([a-z0-9_]+):\s*(\S+)\s*$", msg, re.M))


def message(ver, data, commit):
    bridge = re.search(rb"const BRIDGE_API = (\d+)", data)
    return (f"Video Helper 瀏覽器腳本 v{ver}\n\n"
            f"file: {FILE}\nversion: {ver}\nsize: {len(data)}\nsha256: {sha256(data)}\ncommit: {commit}\n"
            f"bridge_api: {bridge.group(1).decode() if bridge else 'none'}\n"
            f"raw: https://raw.githubusercontent.com/{REPO}/{PREFIX}{ver}/{FILE}\n")


def plan(commit):
    """回傳 ("create"|"skip", 版本, 說明)；版本衝突時 SystemExit"""
    commit = git("rev-parse", commit)
    data = blob(commit)
    if data is None:
        raise SystemExit(f"{commit[:7]} 沒有 {FILE}")
    ver = version_of(data)
    if not ver:
        raise SystemExit(f"{commit[:7]} 的 {FILE} 找不到 @version")
    tag = PREFIX + ver
    have = tags()
    if tag in have:
        old = blob(tag)
        if old == data:
            return "skip", ver, f"{tag} 已經存在、內容一樣（{tag_commit(tag)[:7]}），不用再建"
        raise SystemExit(f"版本衝突：{tag} 已經存在（{tag_commit(tag)[:7]}），但 {commit[:7]} 的腳本內容不一樣 → 改了腳本要把 @version +1")
    newer = [t for t in have if vkey(t[len(PREFIX):]) >= vkey(ver)]
    if newer:
        raise SystemExit(f"版本衝突：{ver} 不比已發布的 {max(newer, key=lambda t: vkey(t[len(PREFIX):]))} 新（版本號只能往上）")
    return "create", ver, f"要建 {tag} → {introduced(commit, data)[:7]}（這份腳本第一次出現的 commit），sha256 {sha256(data)}"


def introduced(commit, data):
    """commit 的歷史裡，腳本第一次變成這個內容的 commit"""
    for c in git("log", "--reverse", "--format=%H", commit, "--", FILE).splitlines():
        if blob(c) == data:
            return c
    return commit


def create(commit, push=True):
    commit = git("rev-parse", commit)
    action, ver, why = plan(commit)
    print(why)
    if action == "skip":
        return ver
    tag = PREFIX + ver
    data = blob(commit)
    first = introduced(commit, data)           # 先試「這份腳本第一次出現」的 commit（機器人 commit 再多也一樣）

    def make(target, extra=""):
        msgfile = os.path.join(tempfile.mkdtemp(), "msg.txt")
        with open(msgfile, "w", encoding="utf-8") as f:
            f.write(message(ver, data, target) + extra)
        git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "tag", "-a", tag, target, "-F", msgfile)
    make(first)
    if push:
        try:
            push_tag(tag)
        except WorkflowRefused:
            # GitHub 不准 workflow 的權限把 tag 放在「workflow 檔跟現在不一樣」的舊 commit → 改放這次 push 的 commit
            # （腳本內容完全一樣，說明另外記第一次出現的 commit）
            git("tag", "-d", tag)
            make(commit, f"introduced: {first}\n")
            push_tag(tag)
            print(f"（{first[:7]} 被 GitHub 擋，tag 改放在 {commit[:7]}；腳本內容一樣）")
    if not verify(tag):
        raise SystemExit(f"{tag} 建好之後驗證失敗")
    return ver


class WorkflowRefused(Exception):
    pass


def push_tag(tag):
    """推 tag，絕不覆蓋。先用 git push（沒有 --force）；GitHub 不准 workflow 的權限把 tag 推到 workflow 檔跟現在不一樣的
    舊 commit 上（refusing … without `workflows` permission）→ 改用 API 建同一個附註 tag（refs 已存在 API 會回 422，一樣不覆蓋）"""
    r = subprocess.run(["git", "-C", GIT_DIR[0], "push", "origin", f"refs/tags/{tag}"], capture_output=True, text=True)
    if r.returncode == 0:
        return
    if "without `workflows` permission" in (r.stderr or ""):
        raise WorkflowRefused(r.stderr.strip())
    raise SystemExit(f"推 {tag} 失敗（已經有同名 tag 的話不會覆蓋）：{r.stderr.strip()}")


def verify(tag, download=False):
    bad = []
    f = fields(tag_message(tag))
    data = blob(tag)
    commit = tag_commit(tag)
    ver = tag[len(PREFIX):]
    if data is None:
        bad.append("tag 裡沒有腳本")
    else:
        if version_of(data) != ver:
            bad.append(f"tag 名是 {ver}，腳本的 @version 是 {version_of(data)}")
        if f.get("sha256") != sha256(data) or f.get("size") != str(len(data)):
            bad.append("tag 說明的指紋／大小跟 tag 裡的腳本不一樣")
    if f.get("version") != ver:
        bad.append("tag 說明的版本不對")
    if f.get("commit") != commit:
        bad.append(f"tag 說明的 commit（{f.get('commit', '?')[:7]}）不是 tag 指的 commit（{commit[:7]}）")
    if git("cat-file", "-t", tag) != "tag":
        bad.append("不是附註 tag")
    if download and data is not None and not bad:
        url = f"https://raw.githubusercontent.com/{REPO}/{tag}/{FILE}"
        try:
            with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-CI"}), timeout=30) as r:
                got = r.read()
            if sha256(got) != f.get("sha256"):
                bad.append(f"從 {url} 下載的指紋不一樣")
        except Exception as e:
            bad.append(f"下載不到 {url}（{e}）")
    for b in bad:
        print(f"::error title={tag}::{b}")
    if not bad:
        print(f"✅ {tag} → {commit[:7]}  sha256 {f.get('sha256')}" + ("（下載核對過）" if download else ""))
    return not bad


def verify_all(download=False):
    ts = sorted(tags(), key=lambda t: vkey(t[len(PREFIX):]))
    ok = all([verify(t, download) for t in ts])
    print(f"{'::notice' if ok else '::error'} title=腳本版本 tag::驗證 {len(ts)} 個 tag，{'全部通過' if ok else '有問題'}")
    return ok


def history():
    """每個 @version 第一次出現的 commit（從舊到新）"""
    seen, out = set(), []
    for c in git("log", "--reverse", "--format=%H", "--", FILE).splitlines():
        v = version_of(blob(c))
        if v and v not in seen:
            seen.add(v)
            out.append((v, c))
    return out


def backfill(push=False):
    have = set(tags())
    made = 0
    for ver, commit in history():
        tag = PREFIX + ver
        if tag in have:
            continue
        data = blob(commit)
        msgfile = os.path.join(tempfile.mkdtemp(), "msg.txt")
        with open(msgfile, "w", encoding="utf-8") as f:
            f.write(message(ver, data, commit) + "backfill: true\n")
        git("-c", "user.name=github-actions[bot]", "-c", "user.email=41898282+github-actions[bot]@users.noreply.github.com",
            "tag", "-a", tag, commit, "-F", msgfile)
        made += 1
    if push:                                     # 上次推失敗、tag 只在本機的，這次也要推
        remote = set(git("ls-remote", "--tags", "origin", PREFIX + "*").split())
        for ver, _ in history():
            tag = PREFIX + ver
            if tag not in have and f"refs/tags/{tag}" not in remote:
                push_tag(tag)                            # 一個一個推，不用 --force
    print(f"::notice title=補 tag::新建 {made} 個（{'已推上去' if push else '只在本機'}）")
    return made


def selftest():
    """在暫存 git repo 走一次：新建、重跑不重建、沒升版改內容→失敗、版本往下→失敗、說明被改→驗證失敗、補歷史"""
    d = tempfile.mkdtemp()
    remote = tempfile.mkdtemp()
    old = GIT_DIR[0]
    GIT_DIR[0] = d
    try:
        subprocess.run(["git", "init", "-q", "--bare", remote], check=True)
        git("init", "-q")
        git("config", "user.name", "selftest")             # CI 的機器沒設 git 身分
        git("config", "user.email", "selftest@example.invalid")
        git("remote", "add", "origin", remote)

        def commit_script(ver, extra=""):
            with open(os.path.join(d, FILE), "w", encoding="utf-8", newline="\n") as f:
                f.write(f"// ==UserScript==\n// @version      {ver}\n// ==/UserScript==\nconst BRIDGE_API = 1;\n{extra}\n")
            git("add", FILE)
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", ver)
            return git("rev-parse", "HEAD")

        def other_commit():
            with open(os.path.join(d, "state.json"), "a", encoding="utf-8") as f:
                f.write("x")
            git("add", "state.json")
            git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "bot")
            return git("rev-parse", "HEAD")

        def fails(fn, *a):
            try:
                fn(*a)
            except SystemExit:
                return True
            return False
        c1 = commit_script("1.0.0")
        c1b = commit_script("1.0.1")
        git("push", "-q", "origin", "HEAD:main")
        assert backfill(push=True) == 2 and verify_all(), "補歷史版本"
        assert tag_commit(PREFIX + "1.0.0") == c1 and "backfill: true" in tag_message(PREFIX + "1.0.0")
        c2 = commit_script("1.1.0")
        assert plan(c2)[0] == "create" and create(c2) == "1.1.0", "新版本要建 tag"
        assert tag_commit(PREFIX + "1.1.0") == c2, "tag 要指向那個 commit"
        bot = other_commit()
        assert plan(bot)[0] == "skip", "機器人 commit（腳本沒變）不建新 tag"
        c2b = commit_script("1.1.1")
        bot2 = other_commit()
        assert create(bot2) == "1.1.1" and tag_commit(PREFIX + "1.1.1") == c2b, "從後面的 commit 建，tag 還是指向腳本第一次出現的 commit"
        assert create(bot) == "1.1.0" and tag_commit(PREFIX + "1.1.0") == c2, "重跑不會移動 tag"
        c3 = commit_script("1.1.0", "// 改了內容但沒升版")
        assert fails(plan, c3), "沒升版改內容要停"
        c4 = commit_script("1.0.5")
        assert fails(plan, c4), "版本往下要停"
        git("tag", "-a", PREFIX + "1.0.5", c4, "-m", "file: x\nversion: 1.0.5\nsha256: " + "0" * 64 + f"\ncommit: {c4}\nsize: 1\n")
        assert not verify(PREFIX + "1.0.5"), "說明裡的指紋被改要抓得到"
        git("tag", "-d", PREFIX + "1.0.5")
        c5 = commit_script("1.2.0")
        git("-c", "user.name=t", "-c", "user.email=t@t", "tag", "-a", PREFIX + "1.2.0", c5, "-m", "x")
        git("push", "-q", "origin", f"refs/tags/{PREFIX}1.2.0")
        git("tag", "-d", PREFIX + "1.2.0")
        assert fails(create, c5), "遠端已經有同名 tag 就失敗（不覆蓋）"
        # GitHub 擋「舊 commit 上的 tag」→ tag 改放這次 push 的 commit，說明記第一次出現的 commit
        c6 = commit_script("1.3.0")
        hook = os.path.join(remote, "hooks", "pre-receive")
        with open(hook, "w", encoding="utf-8", newline="\n") as f:
            f.write(f"#!/bin/sh\nwhile read old new ref; do\n"
                    f"  if [ \"$(git rev-parse \"$new^{{commit}}\" 2>/dev/null)\" = \"{c6}\" ]; then\n"
                    f"    echo 'refusing to allow a GitHub App to create or update workflow without `workflows` permission' >&2; exit 1; fi\n"
                    f"done\n")
        os.chmod(hook, 0o755)
        bot3 = other_commit()
        assert create(bot3) == "1.3.0", "被擋要改放這次的 commit"
        assert tag_commit(PREFIX + "1.3.0") == bot3 and fields(tag_message(PREFIX + "1.3.0")).get("introduced") == c6
        assert c1b
        print("selftest OK：新建、重跑不動、機器人 commit 不建、沒升版／版本往下會停、指紋被改抓得到、不覆蓋遠端 tag、補歷史")
        return True
    finally:
        GIT_DIR[0] = old


def main():
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8")
        except Exception:
            pass
    a = sys.argv[1:]
    cmd = a[0] if a else ""
    if cmd == "plan":
        action, ver, why = plan(a[1])
        print(why)
        out = os.environ.get("GITHUB_OUTPUT")
        if out:
            with open(out, "a", encoding="utf-8") as f:
                f.write(f"action={action}\nversion={ver}\ntag={PREFIX}{ver}\n")
    elif cmd == "create":
        create(a[1])
    elif cmd == "verify":
        sys.exit(0 if verify(a[1], "--download" in a) else 1)
    elif cmd == "verify-all":
        sys.exit(0 if verify_all("--download" in a) else 1)
    elif cmd == "backfill":
        backfill("--push" in a)
    elif cmd == "selftest":
        selftest()
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()

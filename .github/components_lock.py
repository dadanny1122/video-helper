"""components.lock.json（架構稽核 S1，docs/infra/00-架構稽核與改善計畫.md）：
這個版本的每個元件「是哪一版、指紋多少」記在一個檔，附在 Release、跟 EXE 一起簽章。

用法（build.yml，打包完、簽章前）：
  python -I .github/components_lock.py make   --exe dist/VideoHelper.exe --ffmpeg-dir ffmpeg_bin --sbom sbom.cdx.json -o components.lock.json
  python -I .github/components_lock.py verify components.lock.json --exe dist/VideoHelper.exe --ffmpeg-dir ffmpeg_bin
  python -I .github/components_lock.py selftest      （CI 先在 Linux 跑：用假的 EXE 走一次 make＋verify）

規則：
- 版本號都從原始碼讀（AST／正規表示式），不 import 主程式（不會開視窗、不連網）
- 只放版本、檔名、大小、SHA-256、公開網址；**絕不放金鑰、憑證、使用者資料**（make 完會自己掃一遍）
- make 也會把 ffmpeg、mux.js、whisper.cpp、AI 模型、瀏覽器腳本補進 SBOM（原本只有 Python 套件）
"""
import argparse
import ast
import hashlib
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FORMAT = 1


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fileinfo(path, name=None):
    """build 產生的檔（EXE、ffmpeg、SBOM）：直接算"""
    return {"file": name or os.path.relpath(path, ROOT).replace(os.sep, "/"), "size": os.path.getsize(path), "sha256": sha256(path)}


def repoinfo(rel):
    """repo 裡的檔：算 git 裡存的內容（Windows 的 checkout 會把換行改成 CRLF，跟使用者從 GitHub 下載的不一樣）"""
    import subprocess
    try:
        data = subprocess.run(["git", "-C", ROOT, "cat-file", "blob", "HEAD:" + rel], capture_output=True, check=True).stdout
    except Exception:
        with open(os.path.join(ROOT, rel), "rb") as f:
            data = f.read()
    return {"file": rel, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def read(rel):
    with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
        return f.read()


def py_consts(rel, names):
    """從原始碼讀頂層常數（只接受字面值：數字、字串、dict、tuple）"""
    out = {}
    for node in ast.parse(read(rel)).body:
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name) and t.id in names:
                    out[t.id] = ast.literal_eval(node.value)
    missing = set(names) - set(out)
    if missing:
        raise SystemExit(f"{rel} 找不到常數：{sorted(missing)}")
    return out


def userscript():
    src = read("video-helper.user.js")
    ver = re.search(r"^// @version\s+(\S+)", src, re.M).group(1)
    reqs = []
    for url in re.findall(r"^// @require\s+(\S+)", src, re.M):
        m = re.search(r"/npm/([^@/]+)@([^/]+)/.*#sha256=([0-9a-f]{64})", url)
        if m:
            reqs.append({"name": m.group(1), "version": m.group(2), "url": url.split("#")[0], "sha256": m.group(3)})
        else:
            reqs.append({"url": url})
    api = int(re.search(r"const BRIDGE_API = (\d+)", src).group(1)) if re.search(r"const BRIDGE_API = (\d+)", src) else None
    return ver, reqs, api


def make(args):
    app = py_consts("video_helper_app.py", ["VERSION", "REPO", "BRIDGE_API", "WHISPER_PIN", "MODEL_SHA256", "MODEL_SOURCES", "MODEL_URLS"])
    nx = py_consts("nexus/__init__.py", ["NEXUS_VERSION", "PROTOCOL_VERSION"])
    migs = py_consts("nexus/storage/migrations.py", ["MIGRATIONS"])
    us_ver, us_reqs, us_api = userscript()
    commit = os.environ.get("GITHUB_SHA") or args.commit or ""
    lock = {
        "format": FORMAT,
        "release": f"v{app['VERSION']}",
        "repository": app["REPO"],
        "commit": commit,
        "note": "版本和指紋清單；不含任何金鑰、憑證或使用者資料。/ Versions and SHA-256 only — no keys, certificates or user data.",
        "desktop": {"version": app["VERSION"], **({"exe": fileinfo(args.exe, os.path.basename(args.exe))} if args.exe else {}),
                    "bridge_api": app["BRIDGE_API"]},
        "browser": {"version": us_ver, **repoinfo("video-helper.user.js"), "bridge_api": us_api,
                    "install_url": f"https://raw.githubusercontent.com/{app['REPO']}/{commit or 'main'}/video-helper.user.js",
                    "requires": us_reqs},
        "nexus": {"version": nx["NEXUS_VERSION"], "protocol": nx["PROTOCOL_VERSION"], "db_schema": max(migs["MIGRATIONS"])},
        "data": {"sites_json": repoinfo("nexus/net/sites.json"),
                 "en_json": repoinfo("webui/en.json"),
                 "webui": sorted((repoinfo("webui/" + f) for f in os.listdir(os.path.join(ROOT, "webui"))
                                  if f.endswith((".html", ".css", ".js", ".json", ".py"))), key=lambda x: x["file"])},
        "bundled": {},
        "runtime_download": {
            "whisper.cpp": {"version": app["WHISPER_PIN"]["tag"], "url": app["WHISPER_PIN"]["url"],
                            "sha256": app["WHISPER_PIN"]["sha256"], "size": app["WHISPER_PIN"]["size"]},
            "models": {n: {"sha256": s, "size": z, "url": (app["MODEL_SOURCES"][n][1][0] if n in app["MODEL_SOURCES"]
                                                          else app["MODEL_URLS"][0].format(n))}
                       for n, (s, z) in sorted(app["MODEL_SHA256"].items())},
        },
        "python": {"requirements_txt": repoinfo("requirements.txt"),
                   "packages": sorted(re.findall(r"^([A-Za-z0-9_.\-]+)==([^\s\\]+)", read("requirements.txt"), re.M))},
    }
    if args.ffmpeg_dir:
        for exe in ("ffmpeg.exe", "ffprobe.exe"):
            p = os.path.join(args.ffmpeg_dir, exe)
            if os.path.isfile(p):
                lock["bundled"][exe] = {"version": os.environ.get("FFMPEG_TAG", ""), **fileinfo(p, exe)}
    if args.sbom:
        lock["python"]["sbom"] = fileinfo(args.sbom, os.path.basename(args.sbom))
        add_to_sbom(args.sbom, lock)
        lock["python"]["sbom"] = fileinfo(args.sbom, os.path.basename(args.sbom))     # 補完之後的指紋
    no_secrets(lock)
    with open(args.output, "w", encoding="utf-8", newline="\n") as f:
        json.dump(lock, f, ensure_ascii=False, indent=1)
        f.write("\n")
    print(f"::notice title=components.lock.json::Desktop {lock['desktop']['version']}｜Browser {us_ver}｜"
          f"Nexus {nx['NEXUS_VERSION']}（協定 {nx['PROTOCOL_VERSION']}、資料庫 {lock['nexus']['db_schema']}）｜commit {commit[:7]}")
    return lock


SECRET = re.compile(r"-----BEGIN|PRIVATE KEY|sk-ant-|ghp_|github_pat_|xox[bp]-|discord(app)?\.com/api/webhooks|AppData|\\Users\\|/home/", re.I)


def no_secrets(lock):
    text = json.dumps(lock, ensure_ascii=False)
    m = SECRET.search(text)
    if m:
        raise SystemExit(f"components.lock.json 裡出現不該有的東西（{m.group(0)}），不產生")


def add_to_sbom(path, lock):
    """CycloneDX：補上非 Python 的元件（打包在 EXE 裡的 ffmpeg、腳本用的 mux.js、執行時才下載的 whisper.cpp 和模型、瀏覽器腳本本身）"""
    with open(path, encoding="utf-8") as f:
        sbom = json.load(f)
    comps = sbom.setdefault("components", [])
    have = {c.get("bom-ref") for c in comps}

    def add(ref, name, version, sha, typ="application", url=None, desc=None):
        if ref in have:
            return
        c = {"bom-ref": ref, "type": typ, "name": name, "version": version, "hashes": [{"alg": "SHA-256", "content": sha}]}
        if url:
            c["externalReferences"] = [{"type": "distribution", "url": url}]
        if desc:
            c["description"] = desc
        comps.append(c)
    for exe, info in lock["bundled"].items():
        add(f"vh:bundled/{exe}", exe.replace(".exe", ""), info["version"], info["sha256"], url="https://github.com/FFmpeg/FFmpeg",
            desc="打包在 EXE 裡的迷你 ffmpeg（LGPL，.github/ffmpeg/build.sh）")
    w = lock["runtime_download"]["whisper.cpp"]
    add("vh:runtime/whisper.cpp", "whisper.cpp", w["version"], w["sha256"], url=w["url"], desc="AI 字幕第一次使用時下載（指紋寫死）")
    for n, m in lock["runtime_download"]["models"].items():
        add(f"vh:runtime/model-{n}", f"whisper-model-{n}", "pinned", m["sha256"], typ="data", url=m["url"])
    b = lock["browser"]
    add("vh:browser/video-helper.user.js", "video-helper.user.js", b["version"], b["sha256"], url=b["install_url"], desc="瀏覽器腳本（另外安裝，不在 EXE 裡）")
    for r in b["requires"]:
        if "sha256" in r:
            add(f"vh:browser/{r['name']}", r["name"], r["version"], r["sha256"], typ="library", url=r["url"], desc="瀏覽器腳本 @require")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(sbom, f, ensure_ascii=False, indent=1)


def verify(args):
    """CI 重算：清單裡每個檔的指紋都要跟手上的檔一樣（腳本、資料、ffmpeg、EXE），版本號也要跟原始碼一樣"""
    with open(args.lock, encoding="utf-8") as f:
        lock = json.load(f)
    bad = []

    def chk(info, path):
        if not os.path.isfile(path):
            bad.append(f"少了 {path}")
        elif sha256(path) != info["sha256"] or os.path.getsize(path) != info["size"]:
            bad.append(f"{info['file']} 指紋不一樣")

    def chk_repo(info):
        now = repoinfo(info["file"])
        if (now["sha256"], now["size"]) != (info["sha256"], info["size"]):
            bad.append(f"{info['file']} 指紋不一樣（跟這個 commit 裡的不同）")
    for info in [lock["browser"], lock["data"]["sites_json"], lock["data"]["en_json"], *lock["data"]["webui"],
                 lock["python"]["requirements_txt"]]:
        chk_repo(info)
    if args.exe:
        chk(lock["desktop"]["exe"], args.exe)
    if args.ffmpeg_dir:
        for exe, info in lock["bundled"].items():
            chk(info, os.path.join(args.ffmpeg_dir, exe))
    if args.sbom and "sbom" in lock["python"]:
        chk(lock["python"]["sbom"], args.sbom)
    app = py_consts("video_helper_app.py", ["VERSION", "BRIDGE_API"])
    nx = py_consts("nexus/__init__.py", ["NEXUS_VERSION", "PROTOCOL_VERSION"])
    if lock["desktop"]["version"] != app["VERSION"] or lock["nexus"]["version"] != nx["NEXUS_VERSION"] \
            or lock["nexus"]["protocol"] != nx["PROTOCOL_VERSION"] or lock["browser"]["version"] != userscript()[0]:
        bad.append("版本號跟原始碼不一樣")
    if lock["desktop"]["bridge_api"] != app["BRIDGE_API"] or (lock["browser"]["bridge_api"] not in (None, app["BRIDGE_API"])):
        bad.append(f"Bridge API 不一致（電腦版 {lock['desktop']['bridge_api']}、腳本 {lock['browser']['bridge_api']}）")
    if os.environ.get("GITHUB_SHA") and lock["commit"] != os.environ["GITHUB_SHA"]:
        bad.append("commit 跟這次打包的不一樣")
    no_secrets(lock)
    for b in bad:
        print(f"::error title=components.lock.json::{b}")
    if not bad:
        print("::notice title=components.lock.json::重算一致（腳本、資料、Python 清單、ffmpeg、EXE）")
    return not bad


def selftest():
    d = tempfile.mkdtemp()
    exe = os.path.join(d, "VideoHelper.exe")
    ff = os.path.join(d, "ff")
    os.makedirs(ff)
    for p, data in ((exe, b"fake exe"), (os.path.join(ff, "ffmpeg.exe"), b"ff"), (os.path.join(ff, "ffprobe.exe"), b"fp")):
        with open(p, "wb") as f:
            f.write(data)
    sbom = os.path.join(d, "sbom.cdx.json")
    with open(sbom, "w", encoding="utf-8") as f:
        json.dump({"bomFormat": "CycloneDX", "components": [{"bom-ref": "pkg:pypi/x", "name": "x"}]}, f)
    out = os.path.join(d, "components.lock.json")
    ns = argparse.Namespace(exe=exe, ffmpeg_dir=ff, sbom=sbom, output=out, commit="0" * 40)
    lock = make(ns)
    lock_text = open(out, encoding="utf-8").read()
    assert lock["browser"]["sha256"] == hashlib.sha256(read("video-helper.user.js").encode("utf-8")).hexdigest() or "\r" in read("video-helper.user.js"), \
        "腳本指紋要等於 git 裡的內容"
    assert "sk-ant" not in lock_text and "BEGIN" not in lock_text
    names = {c["name"] for c in json.load(open(sbom, encoding="utf-8"))["components"]}
    assert {"ffmpeg", "whisper.cpp", "video-helper.user.js", "mux.js"} <= names, names
    assert verify(argparse.Namespace(lock=out, exe=exe, ffmpeg_dir=ff, sbom=sbom)), "剛產生的清單重算要一致"
    with open(exe, "ab") as f:
        f.write(b"!")
    assert not verify(argparse.Namespace(lock=out, exe=exe, ffmpeg_dir=ff, sbom=sbom)), "EXE 被改過要抓得到"
    try:
        no_secrets({"x": "-----BEGIN PRIVATE KEY-----"})
        raise AssertionError("私鑰要擋下來")
    except SystemExit:
        pass
    assert lock["browser"]["bridge_api"] == lock["desktop"]["bridge_api"], "腳本和電腦版的 Bridge API 要一樣"
    print("selftest OK：", json.dumps({k: lock[k] for k in ("release", "nexus")}, ensure_ascii=False))
    return True


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("make")
    m.add_argument("--exe")
    m.add_argument("--ffmpeg-dir")
    m.add_argument("--sbom")
    m.add_argument("--commit")
    m.add_argument("-o", "--output", default="components.lock.json")
    v = sub.add_parser("verify")
    v.add_argument("lock")
    v.add_argument("--exe")
    v.add_argument("--ffmpeg-dir")
    v.add_argument("--sbom")
    sub.add_parser("selftest")
    a = ap.parse_args()
    if a.cmd == "make":
        make(a)
    elif a.cmd == "verify":
        sys.exit(0 if verify(a) else 1)
    else:
        selftest()


if __name__ == "__main__":
    main()

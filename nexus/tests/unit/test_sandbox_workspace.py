"""V3 PR3：工作資料夾、環境變數白名單、檔名／路徑檢查、清理（AT-006、AT-024、C2／C6／C8）"""
import os
import sys

import pytest

from nexus.sandbox import workspace as wsmod
from nexus.sandbox.workspace import Workspace, WorkspaceError, is_inside, safe_env, sanitize_filename


def can_symlink(tmp_path):
    try:
        os.symlink(tmp_path, tmp_path / "_probe")
        os.remove(tmp_path / "_probe")
        return True
    except (OSError, NotImplementedError):
        return False


def test_workspace_is_random_and_has_three_dirs(tmp_path):
    a, b = Workspace(str(tmp_path)), Workspace(str(tmp_path))
    assert a.path != b.path and len(os.path.basename(a.path)) == 32
    for d in (a.input, a.output, a.tmp):
        assert os.path.isdir(d) and is_inside(d, a.path)


def test_env_whitelist_hides_secrets_and_user_folders(tmp_path, monkeypatch):
    monkeypatch.setenv("VH_BRIDGE_TOKEN", "secret-token")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-xxx")
    monkeypatch.setenv("PYTHONPATH", "/evil")
    ws = Workspace(str(tmp_path))
    env = safe_env(ws, extra_path=[str(tmp_path / "ffmpeg")])
    joined = " ".join(f"{k}={v}" for k, v in env.items())
    assert "secret-token" not in joined and "sk-xxx" not in joined and "PYTHONPATH" not in env
    for k in ("TEMP", "TMP", "APPDATA", "LOCALAPPDATA", "USERPROFILE", "HOME"):
        assert env[k] == ws.tmp
    assert env["PATH"].split(os.pathsep)[0] == str(tmp_path / "ffmpeg")
    assert env["VH_SANDBOX"] == "1"


@pytest.mark.parametrize("raw,expect", [
    ("Hello World.mp4", "Hello World.mp4"),
    ("a/b\\c:d*e?f\"g<h>i|j.mp4", "a_b_c_d_e_f_g_h_i_j.mp4"),
    ("..\\..\\Windows\\evil.mp4", None),          # 只要不含路徑分隔符號就好
    ("CON.mp4", "video_CON.mp4"),
    ("lpt1", "video_lpt1"),
    ("  trailing dots... ", "trailing dots"),
    ("line\nbreak\ttab.mp4", "line break tab.mp4"),
    ("", "video"),
    ("..", "video"),
])
def test_sanitize_filename(raw, expect):
    out = sanitize_filename(raw)
    assert "/" not in out and "\\" not in out and ":" not in out
    if expect is not None:
        assert out == expect


def test_sanitize_long_name_keeps_extension():
    out = sanitize_filename("字" * 400 + ".mp4")
    assert out.endswith(".mp4") and len(out) <= wsmod.MAX_NAME_CHARS + 4


def test_deliver_moves_allowed_files_with_hash(tmp_path):
    ws = Workspace(str(tmp_path / "root"))
    # Windows 檔名不能有冒號（NTFS 會當成「替代資料流」），所以只在 Linux 測冒號被換掉
    raw, want = ("My Video.mp4", "My Video.mp4") if sys.platform == "win32" else ("My: Video.mp4", "My_ Video.mp4")
    with open(os.path.join(ws.output, raw), "wb") as f:
        f.write(b"x" * 1000)
    dest = tmp_path / "dl"
    out = ws.deliver(str(dest))
    assert len(out) == 1 and out[0]["name"] == want and out[0]["size"] == 1000
    assert len(out[0]["sha256"]) == 64 and os.path.isfile(out[0]["path"]) and is_inside(out[0]["path"], str(dest))


def test_deliver_never_overwrites(tmp_path):
    dest = tmp_path / "dl"
    dest.mkdir()
    (dest / "v.mp4").write_bytes(b"old")
    ws = Workspace(str(tmp_path / "root"))
    open(os.path.join(ws.output, "v.mp4"), "wb").write(b"new")
    out = ws.deliver(str(dest))
    assert out[0]["name"] == "v (2).mp4" and (dest / "v.mp4").read_bytes() == b"old"


def test_ntfs_alternate_data_stream_name_rejected(tmp_path):
    """Windows：「v.mp4:hidden」會寫進 v.mp4 的隱藏資料流；收成果時不能被它騙過"""
    if sys.platform != "win32":
        pytest.skip("只有 NTFS 有替代資料流")
    ws = Workspace(str(tmp_path / "root"))
    open(os.path.join(ws.output, "x:hidden.mp4"), "wb").write(b"evil")
    with pytest.raises(WorkspaceError):
        ws.deliver(str(tmp_path / "dl"))


def test_deliver_rejects_bad_extension_whole_batch(tmp_path):
    ws = Workspace(str(tmp_path / "root"))
    open(os.path.join(ws.output, "v.mp4"), "wb").write(b"ok")
    open(os.path.join(ws.output, "run.bat"), "wb").write(b"evil")
    with pytest.raises(WorkspaceError) as e:
        ws.deliver(str(tmp_path / "dl"))
    assert e.value.code == "OUTPUT_REJECTED"
    assert not (tmp_path / "dl" / "v.mp4").exists()              # 整批不搬


def test_deliver_empty_and_too_big(tmp_path):
    ws = Workspace(str(tmp_path / "root"))
    with pytest.raises(WorkspaceError) as e:
        ws.deliver(str(tmp_path / "dl"))
    assert e.value.code == "NO_OUTPUT"
    open(os.path.join(ws.output, "v.mp4"), "wb").write(b"x" * 100)
    with pytest.raises(WorkspaceError) as e:
        ws.deliver(str(tmp_path / "dl"), max_bytes=10)
    assert e.value.code == "TOO_BIG"


def test_symlink_in_output_is_rejected(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("這台不能建符號連結")
    secret = tmp_path / "secret.mp4"
    secret.write_bytes(b"user file")
    ws = Workspace(str(tmp_path / "root"))
    os.symlink(secret, os.path.join(ws.output, "steal.mp4"))
    with pytest.raises(WorkspaceError):
        ws.deliver(str(tmp_path / "dl"))
    assert secret.read_bytes() == b"user file"


def test_symlinked_dest_dir_rejected(tmp_path):
    if not can_symlink(tmp_path):
        pytest.skip("這台不能建符號連結")
    real = tmp_path / "elsewhere"
    real.mkdir()
    os.symlink(real, tmp_path / "dl")
    ws = Workspace(str(tmp_path / "root"))
    open(os.path.join(ws.output, "v.mp4"), "wb").write(b"x")
    with pytest.raises(WorkspaceError) as e:
        ws.deliver(str(tmp_path / "dl"))
    assert e.value.code == "DEST"


def test_is_inside_blocks_traversal(tmp_path):
    base = str(tmp_path / "a")
    os.makedirs(base)
    assert not is_inside(os.path.join(base, "..", "b"), base)
    assert not is_inside(base, base)
    assert is_inside(os.path.join(base, "x", "y"), base)


def test_cleanup_removes_everything_even_readonly(tmp_path):
    ws = Workspace(str(tmp_path))
    p = os.path.join(ws.tmp, "ro.part")
    open(p, "wb").write(b"x")
    os.chmod(p, 0o444)
    ok, err = ws.cleanup()
    assert ok and not err and not os.path.exists(ws.path)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows 才會因為檔案被開著而刪不掉")
def test_cleanup_reports_failure_when_file_locked(tmp_path):
    ws = Workspace(str(tmp_path))
    f = open(os.path.join(ws.tmp, "locked.part"), "wb")
    try:
        ok, err = ws.cleanup(tries=2)
        assert not ok and err                     # 刪不掉要說，不能假裝成功（AT-024）
    finally:
        f.close()
    assert ws.cleanup()[0]

"""APEX Phase 1 PR2：強隔離（AppContainer）偵測——不需要 Windows 的部分（格式、相容性、節點什麼時候送）"""
import json
import subprocess
import sys

import pytest

import nexus
from nexus.core.errors import NexusError
from nexus.sandbox import appcontainer
from nexus.tasks import capabilities


def _caps(**res):
    return {"resources": {"cpu_count": 4, "isolation": "basic", **res}, "adapters": {"test.echo": "1.0.0"}}


# ---------- Master 驗證能力 ----------
def test_master_accepts_hardening_fields():
    out = capabilities.normalize(_caps(hardening="appcontainer", hardening_reason=""))
    assert out["resources"]["hardening"] == "appcontainer"
    out = capabilities.normalize(_caps(hardening="none", hardening_reason="不是 Windows"))
    assert out["resources"]["hardening_reason"] == "不是 Windows"


def test_master_still_accepts_old_worker_without_hardening():
    assert "hardening" not in capabilities.normalize(_caps())["resources"]


@pytest.mark.parametrize("bad", ["strong", "", "x" * 17, 1, True])
def test_master_rejects_bad_hardening(bad):
    with pytest.raises(NexusError):
        capabilities.normalize(_caps(hardening=bad))


def test_protocol_bumped_for_new_field():
    assert nexus.version_tuple(nexus.PROTOCOL_VERSION) >= nexus.version_tuple(nexus.HARDENING_MIN_PROTOCOL) == (1, 4)


# ---------- 節點：只送給看得懂的 Master ----------
class _FakeClient:
    def __init__(self, peer):
        self.peer_protocol = peer


def _fields(monkeypatch, peer, hs):
    from nexus.worker import agent as agent_mod
    from nexus.worker import executor
    monkeypatch.setattr(executor, "hardening_status", lambda *a, **k: hs)
    a = agent_mod.WorkerAgent.__new__(agent_mod.WorkerAgent)      # 只測這個方法，不開連線
    a._client = _FakeClient(peer) if peer is not None else None
    return a._hardening_fields()


DONE = {"hardening": "none", "reasons": ["這台沒有 curl.exe"], "checks": {}}


def test_old_master_never_gets_new_field(monkeypatch):
    """舊 Master（1.3）看到不認得的欄位會把整份能力丟掉 → 這台就接不到任務。一定不能送"""
    assert _fields(monkeypatch, "1.3", DONE) == {}
    assert _fields(monkeypatch, None, DONE) == {}                  # 還沒連過 Master：不知道版本 → 不送
    assert _fields(monkeypatch, "壞掉", DONE) == {}


def test_new_master_gets_result_when_ready(monkeypatch):
    assert _fields(monkeypatch, "1.4", None) == {}                 # 還在偵測
    assert _fields(monkeypatch, "1.4", DONE) == {"hardening": "none", "hardening_reason": "這台沒有 curl.exe"}
    ok = {"hardening": "appcontainer", "reasons": [], "checks": {}}
    assert _fields(monkeypatch, "1.5", ok) == {"hardening": "appcontainer", "hardening_reason": ""}


# ---------- 偵測子程序的輸出 ----------
def test_clean_keeps_only_valid_fields():
    r = appcontainer.clean({"hardening": "appcontainer", "reasons": [], "checks": {"run": True, "file": "blocked"}})
    assert r == {"hardening": "appcontainer", "reasons": [], "checks": {"run": True, "file": "blocked"}}
    r = appcontainer.clean({"hardening": "super", "reasons": [1, "x" * 500], "checks": {"a": {"nested": 1}, "b": "y" * 99}})
    assert r["hardening"] == "none" and r["reasons"] == ["x" * 200] and r["checks"] == {}
    assert appcontainer.clean("garbage")["hardening"] == "none"
    assert appcontainer.clean({"hardening": "none"})["reasons"] == ["原因不明"]        # 不能用一定要有原因


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上會真的偵測（見 integration）")
def test_not_windows_reports_none_with_reason(tmp_path):
    assert appcontainer.probe_strong(str(tmp_path)) == {"hardening": "none", "reasons": ["不是 Windows"], "checks": {}}
    assert appcontainer.run_checks(str(tmp_path))["reasons"] == ["不是 Windows"]


def test_entry_prints_one_json_line(tmp_path):
    """跟節點真正開的指令一樣（probe_command）：一定印出一行看得懂的 JSON、結束碼 0"""
    p = subprocess.run(appcontainer.probe_command(str(tmp_path)), capture_output=True, text=True, timeout=60)
    assert p.returncode == 0
    r = json.loads(p.stdout.strip().splitlines()[-1])
    assert r["hardening"] in appcontainer.LEVELS
    assert r["hardening"] == "appcontainer" or r["reasons"]
    assert list(tmp_path.iterdir()) == []                          # 測試檔有刪掉


def test_entry_usage_error():
    assert appcontainer.entry(["--nope"]) == 64

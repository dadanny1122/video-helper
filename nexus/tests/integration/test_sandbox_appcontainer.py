"""
APEX Phase 1 PR2：在真的 Windows 上跑強隔離偵測（nexus.yml 的 windows job）。
GitHub 的 Windows 機器能不能用 AppContainer 不一定，所以這裡**不要求一定是 appcontainer**，
只要求：偵測跑得完、結果前後一致、測試檔刪掉、不留程序。真正的答案看使用者自己電腦上的結果。
"""
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "win32", reason="AppContainer 只有 Windows")

if sys.platform == "win32":
    from nexus.sandbox import appcontainer


def test_probe_completes_and_is_consistent(tmp_path):
    r = appcontainer.run_checks(str(tmp_path))
    print("強隔離偵測：", r)                                     # CI log 看得到實際結果
    assert r["hardening"] in appcontainer.LEVELS
    c = r["checks"]
    if r["hardening"] == "appcontainer":
        assert c["run"] is True and c["file"] == "blocked" and c["loopback"] == "blocked" and not r["reasons"]
    else:
        assert r["reasons"]
    assert list(tmp_path.iterdir()) == []


def test_probe_via_subprocess_matches(tmp_path):
    r = appcontainer.probe_strong(str(tmp_path), timeout=90)
    assert r["hardening"] in appcontainer.LEVELS
    assert "偵測程式沒有回報" not in "".join(r["reasons"])


def test_container_profile_cleanup_is_repeatable():
    """上次沒清掉也能再用（ALREADY_EXISTS → 沿用），而且每次都能刪掉"""
    for _ in range(2):
        try:
            with appcontainer.Container():
                pass
        except appcontainer.ProbeError as e:
            pytest.skip(f"這台不能建 AppContainer：{e}")

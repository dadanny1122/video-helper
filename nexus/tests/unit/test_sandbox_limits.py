"""V3 PR2：沙箱設定驗證（任何平台都跑）；真的 Job Object 測試在 integration/test_sandbox_job.py（只在 Windows）"""
import sys

import pytest

from nexus.sandbox import jobs
from nexus.sandbox.jobs import JobLimits


def test_default_limits_match_spec_d4():
    d = JobLimits()
    assert d.memory_bytes == 2 << 30 and d.cpu_percent == 50 and d.max_processes == 8


@pytest.mark.parametrize("kw", [{"memory_bytes": 1 << 20}, {"memory_bytes": 128 << 30}, {"cpu_percent": 0},
                                {"cpu_percent": 101}, {"max_processes": 0}, {"max_processes": 100}])
def test_bad_limits_rejected(kw):
    with pytest.raises(ValueError):
        JobLimits(**kw)


@pytest.mark.skipif(sys.platform == "win32", reason="非 Windows 才測")
def test_non_windows_refuses_not_degrades():
    r = jobs.probe()
    assert r["isolation"] == "none" and "不是 Windows" in r["reasons"]
    with pytest.raises(jobs.SandboxError):
        jobs.Job()

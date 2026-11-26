"""APEX sim.pvwatts：氣象轉換、參數檢查、沙箱裡真的跑 PySAM（有裝才跑）、結果檢查、Master 驗證"""
import datetime as dt
import io
import json
import math
import sys

import pytest

from nexus import display as nd
from nexus.sim import power, pvwatts
from nexus.tasks import adapters, manifest

try:
    import PySAM.Pvwattsv8  # noqa: F401
    HAVE_PYSAM = True
except Exception:
    HAVE_PYSAM = False


def fake_power(year=2023, lat=25.04, lon=121.56, missing=0, leap=False):
    """假的 NASA POWER 回應：晴天正弦日照"""
    par = {k: {} for k in power.PARAMS}
    t = dt.datetime(year, 1, 1)
    n = 8784 if leap else 8760
    for i in range(n):
        d = t + dt.timedelta(hours=i)
        k = d.strftime("%Y%m%d%H")
        s = max(0.0, math.sin(math.pi * (d.hour + 0.5 - 6) / 12)) if 6 <= d.hour < 18 else 0.0
        gh = -999 if i < missing else round(800 * s, 2)
        par["ALLSKY_SFC_SW_DWN"][k] = gh
        par["ALLSKY_SFC_SW_DNI"][k] = round(600 * s, 2)
        par["ALLSKY_SFC_SW_DIFF"][k] = round(200 * s, 2)
        par["T2M"][k] = 25.0
        par["WS10M"][k] = 2.0
    return {"geometry": {"coordinates": [lon, lat, 12.0]}, "header": {"fill_value": -999.0},
            "properties": {"parameter": par}}


# ---------- 氣象 ----------
def test_url_is_fixed_host_and_params():
    u = power.url_for(25.04, 121.56, 2023)
    assert u.startswith("https://power.larc.nasa.gov/api/temporal/hourly/point?")
    assert "start=20230101" in u and "end=20231231" in u and "time-standard=LST" in u


def test_to_resource_basic():
    r = power.to_resource(fake_power(), 25.04, 121.56, 2023)
    assert len(r["gh"]) == 8760 and r["tz"] == 8 and r["elev"] == 12.0 and r["missing_hours"] == 0
    assert 780 < max(r["gh"]) <= 800 and min(r["gh"]) == 0


def test_leap_year_drops_feb29():
    r = power.to_resource(fake_power(year=2024, leap=True), 25.0, 121.5, 2024)
    assert len(r["gh"]) == 8760 and not any(m == 2 and d == 29 for m, d in zip(r["month"], r["day"], strict=True))


def test_missing_values():
    r = power.to_resource(fake_power(missing=100), 25.0, 121.5, 2023)
    assert r["missing_hours"] == 100 and r["gh"][0] == 0
    with pytest.raises(power.WeatherError) as e:
        power.to_resource(fake_power(missing=500), 25.0, 121.5, 2023)       # 超過 5%
    assert e.value.code == "INVALID_INPUT"


def test_load_uses_cache(tmp_path):
    calls = []

    class Opener:
        def open(self, req, timeout=None):
            calls.append(req.full_url)
            return io.BytesIO(json.dumps(fake_power()).encode())
    r1, cached1 = power.load(25.04, 121.56, 2023, str(tmp_path), opener=Opener())
    r2, cached2 = power.load(25.04, 121.56, 2023, str(tmp_path), opener=Opener())
    assert (cached1, cached2) == (False, True) and len(calls) == 1 and r1["gh"] == r2["gh"]


# ---------- 參數 ----------
SPEC = adapters.get("sim.pvwatts")


def test_params():
    ok = {"lat": 25.04, "lon": 121.56, "year": 2023, "capacity_kw": 1000}
    assert adapters.check_params(SPEC, ok) == []
    assert adapters.check_params(SPEC, {**ok, "lat": 91}) and adapters.check_params(SPEC, {**ok, "lat": True})
    assert adapters.check_params(SPEC, {**ok, "year": dt.date.today().year})               # 今年還沒過完
    assert adapters.check_params(SPEC, {**ok, "code": "import os"})                         # 不認得的參數一律拒絕
    m = manifest.build("sim.pvwatts", ok)
    assert manifest.validate(m) == [] and m["limits"]["max_runtime_seconds"] == 600
    assert adapters.needs_sandbox("sim.pvwatts")


# ---------- 結果檢查 ----------
GOOD = {"annual_kwh": 1200.0, "monthly_kwh": [100.0] * 12, "capacity_factor": 13.7, "kwh_per_kw": 1200.0}


def test_result_problem():
    assert pvwatts.result_problem(GOOD) is None
    assert pvwatts.result_problem({**GOOD, "monthly_kwh": [100.0] * 11})
    assert pvwatts.result_problem({**GOOD, "annual_kwh": 5000.0})                            # 各月加起來對不上
    assert pvwatts.result_problem({**GOOD, "capacity_factor": 120})
    assert pvwatts.result_problem("x")


def test_master_verify_physical_limit():
    from nexus.tasks.dispatch import Dispatcher
    m = manifest.build("sim.pvwatts", {"lat": 25.0, "lon": 121.5, "year": 2023, "capacity_kw": 0.1})
    t = {"type": "sim.pvwatts", "manifest_json": json.dumps(m)}
    ok, why = Dispatcher.verify(t, GOOD)                             # 0.1 kW 不可能一年 1200 kWh
    assert not ok and "8760" in why
    m = manifest.build("sim.pvwatts", {"lat": 25.0, "lon": 121.5, "year": 2023, "capacity_kw": 1})
    assert Dispatcher.verify({"type": "sim.pvwatts", "manifest_json": json.dumps(m)}, GOOD) == (True, "")


# ---------- 真的跑 PySAM（這台有裝才跑；Linux 沒有 Job Object → use_job=False） ----------
@pytest.mark.skipif(not HAVE_PYSAM, reason="沒有 PySAM")
def test_probe_python():
    ok, ver = pvwatts.probe_python(sys.executable)
    assert ok and ver
    assert pvwatts.probe_python("")[0] is False and pvwatts.probe_python("/no/such/python")[0] is False


@pytest.mark.skipif(not HAVE_PYSAM, reason="沒有 PySAM")
def test_run_in_child(tmp_path):
    res = power.to_resource(fake_power(), 25.04, 121.56, 2023)
    params = {"lat": 25.04, "lon": 121.56, "year": 2023, "capacity_kw": 1000, "tilt": 20, "azimuth": 180, "losses": 14}
    r = pvwatts.run(params, res, sys.executable, str(tmp_path / "sb"), use_job=sys.platform == "win32")
    assert pvwatts.result_problem(r) is None and 800_000 < r["annual_kwh"] < 2_500_000
    assert list((tmp_path / "sb" / "work").iterdir()) == []          # 工作資料夾有刪
    text = nd.sim_result(json.dumps({**r, "weather": {"source": "NASA POWER", "cached": True}}))
    assert text.startswith("年發電量 ") and "（用快取）" in text


@pytest.mark.skipif(not HAVE_PYSAM, reason="沒有 PySAM")
def test_child_has_no_network(tmp_path):
    """模擬子程序一開始就把網路關掉（就算 PySAM 被打進去，也連不出去）"""
    script = tmp_path / "s.py"
    script.write_text(pvwatts.CHILD_SCRIPT.split("def out(obj):")[0] +
                      "import socket\ntry:\n    socket.create_connection(('127.0.0.1', 9))\n    print('連得到')\n"
                      "except OSError as e:\n    print('擋住', e)\n", encoding="utf-8")
    import subprocess
    p = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=30)
    assert "擋住" in p.stdout and "不准連網" in p.stdout


def test_child_reports_missing_pysam(tmp_path):
    """這個 Python 沒有 PySAM → 回報 SANDBOX_UNAVAILABLE（不會當掉）"""
    if HAVE_PYSAM:
        pytest.skip("這台有 PySAM，換不出「沒有」的情況")
    res = power.to_resource(fake_power(), 25.0, 121.5, 2023)
    with pytest.raises(pvwatts.SimError) as e:
        pvwatts.run({"capacity_kw": 1}, res, sys.executable, str(tmp_path / "sb"), use_job=False)
    assert e.value.code == "SANDBOX_UNAVAILABLE"

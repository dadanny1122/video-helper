"""
NASA POWER 逐時氣象 → PySAM 的 solar_resource_data（在節點「主程式」裡跑，不在沙箱子程序裡）。

安全：只准 https://power.larc.nasa.gov（寫死，不能從任務參數改）、解析後的 IP 一定要是公開網路（擋 DNS 被指到內網）、
回應最大 40 MB、60 秒逾時。抓到的資料存在節點的快取資料夾，同一點同一年下次直接用。
"""
import ipaddress
import json
import os
import socket
import ssl
import urllib.error
import urllib.parse
import urllib.request

HOST = "power.larc.nasa.gov"
PATH = "/api/temporal/hourly/point"
PARAMS = ("ALLSKY_SFC_SW_DWN", "ALLSKY_SFC_SW_DNI", "ALLSKY_SFC_SW_DIFF", "T2M", "WS10M")
MAX_BYTES = 40 << 20
TIMEOUT = 60
MAX_MISSING = 0.05                     # 日照缺值超過 5% 就不算（結果會失真）


class WeatherError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def url_for(lat: float, lon: float, year: int) -> str:
    q = {"parameters": ",".join(PARAMS), "community": "RE", "longitude": f"{lon:.4f}", "latitude": f"{lat:.4f}",
         "start": f"{year}0101", "end": f"{year}1231", "format": "JSON", "time-standard": "LST"}
    return f"https://{HOST}{PATH}?{urllib.parse.urlencode(q)}"


def cache_name(lat: float, lon: float, year: int) -> str:
    return f"power_{lat:.4f}_{lon:.4f}_{year}.json"


def _check_dns():
    try:
        infos = socket.getaddrinfo(HOST, 443, type=socket.SOCK_STREAM)
    except OSError as e:
        raise WeatherError("NETWORK", f"找不到 {HOST}：{e}") from e
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise WeatherError("NET_BLOCKED", f"{HOST} 被解析到非公開位址 {ip}，不連")


def _ssl_context():
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def download(lat: float, lon: float, year: int, opener=None) -> dict:
    """抓一整年逐時資料，回傳 NASA POWER 原始 JSON（dict）。opener 給測試換掉網路"""
    url = url_for(lat, lon, year)
    if opener is None:
        _check_dns()

        class _NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k):
                return None                        # 不跟著轉址（轉到別的網站就不是我們同意的來源）
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=_ssl_context()), _NoRedirect)
    try:
        with opener.open(urllib.request.Request(url, headers={"User-Agent": "VideoHelper-Nexus/sim.pvwatts"}),
                         timeout=TIMEOUT) as r:
            if getattr(r, "status", 200) != 200:
                raise WeatherError("NETWORK", f"NASA POWER 回 HTTP {r.status}")
            data = r.read(MAX_BYTES + 1)
    except WeatherError:
        raise
    except urllib.error.HTTPError as e:
        code = "RATE_LIMITED" if e.code == 429 else ("INVALID_INPUT" if e.code in (400, 422) else "NETWORK")
        raise WeatherError(code, f"NASA POWER 回 HTTP {e.code}") from e
    except (OSError, ValueError) as e:
        raise WeatherError("NETWORK", f"連不到 NASA POWER：{type(e).__name__}") from e
    if len(data) > MAX_BYTES:
        raise WeatherError("TOO_BIG", "NASA POWER 回的資料太大")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise WeatherError("NETWORK", "NASA POWER 回的不是 JSON") from e


def to_resource(raw: dict, lat: float, lon: float, year: int) -> dict:
    """NASA POWER JSON → PySAM solar_resource_data（8760 小時；閏年拿掉 2/29）"""
    try:
        par = raw["properties"]["parameter"]
        fill = float((raw.get("header") or {}).get("fill_value", -999))
        coords = (raw.get("geometry") or {}).get("coordinates") or [lon, lat, 0]
        elev = float(coords[2]) if len(coords) > 2 and isinstance(coords[2], (int, float)) else 0.0
        keys = sorted(k for k in par["ALLSKY_SFC_SW_DWN"] if k.startswith(str(year)))
    except (KeyError, TypeError, ValueError) as e:
        raise WeatherError("NETWORK", "NASA POWER 資料格式看不懂") from e
    keys = [k for k in keys if k[4:8] != "0229"]
    if len(keys) != 8760:
        raise WeatherError("INVALID_INPUT", f"{year} 年資料只有 {len(keys)} 小時（要 8760）")
    out = {"lat": lat, "lon": lon, "tz": round(lon / 15), "elev": elev,
           "year": [], "month": [], "day": [], "hour": [], "minute": [], "gh": [], "dn": [], "df": [], "tdry": [], "wspd": []}
    missing = 0
    for k in keys:
        out["year"].append(int(k[0:4]))
        out["month"].append(int(k[4:6]))
        out["day"].append(int(k[6:8]))
        out["hour"].append(int(k[8:10]))
        out["minute"].append(30)                          # 小時平均值 → 放在半點
        for src, dst, lo in (("ALLSKY_SFC_SW_DWN", "gh", 0), ("ALLSKY_SFC_SW_DNI", "dn", 0), ("ALLSKY_SFC_SW_DIFF", "df", 0),
                             ("T2M", "tdry", None), ("WS10M", "wspd", 0)):
            v = par.get(src, {}).get(k)
            bad = not isinstance(v, (int, float)) or v == fill
            if bad and dst == "gh":
                missing += 1
            out[dst].append((lo if lo is not None else 20.0) if bad else (max(lo, float(v)) if lo is not None else float(v)))
    if missing / 8760 > MAX_MISSING:
        raise WeatherError("INVALID_INPUT", f"日照資料缺了 {missing} 小時（超過 5%），這一年不能用")
    out["missing_hours"] = missing
    return out


def load(lat: float, lon: float, year: int, cache_dir: str, opener=None) -> tuple[dict, bool]:
    """回傳 (resource, 是不是用快取)。快取壞掉就重抓"""
    os.makedirs(cache_dir, exist_ok=True)
    path = os.path.join(cache_dir, cache_name(lat, lon, year))
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return to_resource(json.load(f), lat, lon, year), True
        except (OSError, ValueError, WeatherError):
            pass
    raw = download(lat, lon, year, opener)
    res = to_resource(raw, lat, lon, year)                 # 先確定看得懂才存
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(raw, f)
    os.replace(tmp, path)
    return res, False

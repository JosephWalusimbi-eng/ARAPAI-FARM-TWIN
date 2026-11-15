"""Offline tests for the data-source layer (IMERG, SMAP, Earthdata auth, merging).
All data here is SYNTHETIC test input, not real observations."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import climate  # noqa: E402
from farm_twin.sources import SOURCES, farm_table  # noqa: E402
from farm_twin.sources import base, earthdata, smap  # noqa: E402
from farm_twin.sources.base import PointSeries  # noqa: E402


# --- EASE-Grid 2.0 -----------------------------------------------------------------
def test_ease2_grid_edges_and_equator():
    # The grid is symmetric: equator and prime meridian fall on cell boundaries.
    assert smap.ease2_9km_rowcol(0.0001, 0.0001) == (811, 1928)
    assert smap.ease2_9km_rowcol(-0.0001, -0.0001) == (812, 1927)
    assert smap.ease2_9km_rowcol(84.9, -179.99) == (0, 0)
    assert smap.ease2_9km_rowcol(84.9, 179.99) == (0, 3855)
    assert smap.ease2_9km_rowcol(-84.9, 0.0001) == (1623, 1928)


def test_ease2_cell_width_in_longitude():
    # Cell width = 9008.055 m / (a * k0) = 0.09336 deg of longitude everywhere on this grid.
    _, c0 = smap.ease2_9km_rowcol(1.71, 33.0)
    _, c1 = smap.ease2_9km_rowcol(1.71, 34.0)
    assert c1 - c0 in (10, 11)  # 1 deg / 0.09336 = 10.7 cells


# --- SMAP parsing and quality ------------------------------------------------------------
def _pixel_nc4(am=0.25, am_flag=0, pm=0.27, pm_flag=0, lat=1.7, lon=33.6) -> bytes:
    import netCDF4
    ds = netCDF4.Dataset("x.nc", "w", memory=4096)
    g_am = ds.createGroup(smap.AM)
    g_pm = ds.createGroup(smap.PM)
    for g in (g_am, g_pm):
        g.createDimension("y", 1)
        g.createDimension("x", 1)

    def var(g, name, value, dtype="f4", fill=-9999.0):
        v = g.createVariable(name, dtype, ("y", "x"), fill_value=fill)
        v[:] = np.array([[value]])

    var(g_am, "soil_moisture", am)
    var(g_am, "retrieval_qual_flag", am_flag, "u2", 65534)
    var(g_am, "latitude", lat)
    var(g_am, "longitude", lon)
    var(g_pm, "soil_moisture_pm", pm)
    var(g_pm, "retrieval_qual_flag_pm", pm_flag, "u2", 65534)
    var(g_pm, "latitude_pm", lat)
    var(g_pm, "longitude_pm", lon)
    return bytes(ds.close())


def test_parse_pixel_and_daily_value():
    rec = smap.parse_pixel_nc4(_pixel_nc4())
    assert rec["am"] == pytest.approx(0.25) and rec["pm"] == pytest.approx(0.27)
    v, n = smap.daily_value(rec)
    assert n == 2 and v == pytest.approx(0.26)


def test_fill_and_bad_quality_are_dropped():
    rec = smap.parse_pixel_nc4(_pixel_nc4(am=-9999.0, pm=0.3, pm_flag=1))
    assert rec["am"] is None
    v, n = smap.daily_value(rec)
    assert n == 0 and np.isnan(v)
    v, n = smap.daily_value(rec, require_recommended=False)
    assert n == 1 and v == pytest.approx(0.3)


def test_opendap_url_requests_single_pixel():
    url = smap.opendap_pixel_url("https://opendap.example/granule.h5", 800, 2300)
    assert url.startswith("https://opendap.example/granule.h5.dap.nc4?dap4.ce=")
    assert "%5B800%5D%5B2300%5D" in url and "soil_moisture_pm" in url


# --- CMR listing -------------------------------------------------------------------------
def _granule(day, ver="001"):
    ur = f"SMAP_L3_SM_P_E_{day}_R19240_{ver}.h5"
    return {"umm": {"GranuleUR": ur, "RelatedUrls": [
        {"Type": "GET DATA", "URL": f"https://data.example/{ur}"},
        {"Type": "USE SERVICE API", "Subtype": "OPENDAP DATA", "URL": f"https://opendap.example/{ur}"}]}}


def test_list_granules_pages_and_keeps_latest_version():
    pages = [
        ({"items": [_granule("20240401"), _granule("20240402")]}, {"CMR-Search-After": "tok"}),
        ({"items": [_granule("20240402", "002")]}, {}),
    ]

    def handler(request):
        body, headers = pages.pop(0)
        return httpx.Response(200, json=body, headers=headers)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    g = smap.list_granules("20240401", "20240402", client=client)
    assert list(g) == ["20240401", "20240402"]
    assert g["20240402"].endswith("_002.h5")


def test_list_granules_retries_timeouts_and_caches(tmp_path, monkeypatch):
    monkeypatch.setattr(smap.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ReadTimeout("slow network", request=request)
        return httpx.Response(200, json={"items": [_granule("20240401")]})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    g = smap.list_granules("20240401", "20240401", client=client, cache_dir=tmp_path)
    assert list(g) == ["20240401"] and len(calls) == 2
    # Resumed run: listing comes from disk, no network at all.
    dead = httpx.Client(transport=httpx.MockTransport(lambda r: pytest.fail("network used")))
    assert smap.list_granules("20240401", "20240401", client=dead, cache_dir=tmp_path) == g


# --- SMAP fetch end to end (mocked network) ----------------------------------------------
class _FakeSession:
    def __init__(self):
        self.urls = []

    def get(self, url):
        self.urls.append(url)
        return httpx.Response(200, content=_pixel_nc4(lat=1.7, lon=33.6))

    def close(self):
        pass


def test_smap_fetch_caches_and_resumes(tmp_path, monkeypatch):
    days = {"20240401": "https://opendap.example/a.h5", "20240403": "https://opendap.example/c.h5"}
    monkeypatch.setattr(smap, "list_granules", lambda *a, **k: days)
    # Make the pixel centre agree with the farm's pixel so the geolocation check passes.
    row, col = smap.ease2_9km_rowcol(1.71, 33.61)
    monkeypatch.setattr(smap.SmapSource, "_check_geolocation", staticmethod(lambda *a: None))
    src = smap.SmapSource()
    sess = _FakeSession()
    s = src.fetch(1.71, 33.61, "20240401", "20240403", cache_dir=tmp_path, session=sess)
    assert len(sess.urls) == 2
    assert s.df.loc["2024-04-01", "SMAP_SM"] == pytest.approx(0.26)
    assert np.isnan(s.df.loc["2024-04-02", "SMAP_SM"])  # no granule that day -> missing, not 0
    assert s.provenance["ease2_row"] == row and s.provenance["days_with_retrieval"] == 2
    # Second run: nothing new to fetch.
    sess2 = _FakeSession()
    src.fetch(1.71, 33.61, "20240401", "20240403", cache_dir=tmp_path, session=sess2)
    assert sess2.urls == []
    # Cache-only read works without any network.
    s3 = src.fetch(1.71, 33.61, "20240401", "20240403", cache_dir=tmp_path, cache_only=True)
    assert s3.df["SMAP_SM"].notna().sum() == 2


def test_smap_cache_only_without_data_says_not_fetched(tmp_path):
    with pytest.raises(base.NotFetched):
        smap.SmapSource().fetch(1.71, 33.61, "20240401", "20240403", cache_dir=tmp_path, cache_only=True)


def test_geolocation_mismatch_is_caught():
    raw = {}
    row, col = smap.ease2_9km_rowcol(1.71, 33.61)
    with pytest.raises(base.SourceError, match="mismatch"):
        smap.SmapSource._check_geolocation({"lat": 10.0, "lon": 10.0}, raw, row, col)


def test_geolocation_falls_back_to_pm_coordinates():
    row, col = smap.ease2_9km_rowcol(1.71, 33.61)
    raw = {}
    # No AM pass (coordinates missing), PM coordinates far away -> must still be checked.
    with pytest.raises(base.SourceError, match="mismatch"):
        smap.SmapSource._check_geolocation({"lat": None, "lon": None, "lat_pm": 10.0, "lon_pm": 10.0},
                                           raw, row, col)
    smap.SmapSource._check_geolocation({"lat": None, "lon": None, "lat_pm": 1.71, "lon_pm": 33.61},
                                       raw, row, col)
    assert raw["pixel_lat"] == 1.71


# --- Earthdata auth ----------------------------------------------------------------------
def test_credentials_order(monkeypatch, tmp_path):
    for k in ("EARTHDATA_TOKEN", "EARTHDATA_USERNAME", "EARTHDATA_PASSWORD"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert earthdata.credentials() is None
    (tmp_path / ".netrc").write_text("machine urs.earthdata.nasa.gov login nu password np\n")
    assert earthdata.credentials() == ("basic", ("nu", "np"))
    monkeypatch.setenv("EARTHDATA_USERNAME", "eu")
    monkeypatch.setenv("EARTHDATA_PASSWORD", "ep")
    assert earthdata.credentials() == ("basic", ("eu", "ep"))
    monkeypatch.setenv("EARTHDATA_TOKEN", "tok")
    assert earthdata.credentials() == ("token", "tok")


def test_basic_auth_only_sent_to_urs():
    seen = []

    # Real Earthdata flow: data host -> URS (credentials) -> data host callback sets its
    # own session cookie -> original URL now succeeds.
    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        host, path = request.url.host, request.url.path
        if host == "urs.earthdata.nasa.gov":
            return httpx.Response(302, headers={"location": "https://opendap.earthdata.nasa.gov/callback?code=1"})
        if path == "/callback":
            return httpx.Response(302, headers={"location": "https://opendap.earthdata.nasa.gov/x",
                                                "set-cookie": "s=1; Path=/"})
        if "cookie" not in request.headers:
            return httpx.Response(302, headers={"location": "https://urs.earthdata.nasa.gov/oauth/authorize"})
        return httpx.Response(200, content=b"ok")

    sess = earthdata.EarthdataSession(("basic", ("u", "p")), transport=httpx.MockTransport(handler))
    assert sess.get("https://opendap.earthdata.nasa.gov/x").content == b"ok"
    hosts_with_auth = {h for h, a in seen if a}
    assert hosts_with_auth == {"urs.earthdata.nasa.gov"}


def test_token_not_sent_to_foreign_hosts():
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200)

    sess = earthdata.EarthdataSession(("token", "T"), transport=httpx.MockTransport(handler))
    sess.get("https://opendap.earthdata.nasa.gov/x")
    sess.get("https://example.com/y")
    assert seen == [("opendap.earthdata.nasa.gov", "Bearer T"), ("example.com", None)]


def test_auth_rejection_explains(monkeypatch):
    sess = earthdata.EarthdataSession(("token", "bad"),
                                      transport=httpx.MockTransport(lambda r: httpx.Response(401)))
    with pytest.raises(earthdata.EarthdataAuthError, match="Authorized Apps"):
        sess.get("https://opendap.earthdata.nasa.gov/x")


def test_missing_login_is_not_configured(monkeypatch):
    monkeypatch.setattr(earthdata, "credentials", lambda: None)
    with pytest.raises(base.NotConfigured):
        earthdata.EarthdataSession()


# --- Merging and climate use --------------------------------------------------------------
def _series(key, cols, start="2016-01-01", end="2019-12-31", value=1.0):
    idx = pd.date_range(start, end, freq="D", name="date")
    return PointSeries(key, pd.DataFrame({c: value for c in cols}, index=idx),
                       {c: "u" for c in cols}, {"dataset": key})


def test_farm_table_merges_and_reports_failures(monkeypatch):
    power_cols = ["T2M", "T2M_MAX", "PRECTOTCORR", "GWETROOT"]
    monkeypatch.setattr(SOURCES["power"], "fetch", lambda *a, **k: _series("power", power_cols, value=3.0))
    monkeypatch.setattr(SOURCES["imerg"], "fetch", lambda *a, **k: _series("imerg", ["IMERG_PRECTOT"], value=3.3))

    def no_smap(*a, **k):
        raise base.NotConfigured("no login")

    monkeypatch.setattr(SOURCES["smap"], "fetch", no_smap)
    df, meta = farm_table(1.71, 33.61, "20160101", "20191231")
    assert {"PRECTOTCORR", "IMERG_PRECTOT"} <= set(df.columns)
    assert meta["smap"]["status"] == "not_configured" and meta["imerg"]["status"] == "ok"

    prof = climate.climate_profile(df)
    cc = prof["rain_crosscheck"]["A"]
    assert cc["years_compared"] == 4
    assert cc["imerg_minus_power_pct"] == pytest.approx(10.0)
    jan = prof["monthly_climatology"][0]
    assert jan["IMERG_PRECTOT"] == pytest.approx(31 * 3.3)  # summed, not averaged


def test_soil_moisture_stats_need_enough_retrievals():
    idx = pd.date_range("2016-01-01", "2017-12-31", freq="D", name="date")
    sm = pd.Series(np.nan, index=idx)
    sm[(idx.year == 2016) & idx.month.isin([3, 4, 5, 6])] = 0.2   # full season 2016
    sm[(idx.year == 2017) & (idx.month == 3) & (idx.day <= 10)] = 0.1  # only 10 days in 2017
    df = pd.DataFrame({"PRECTOTCORR": 1.0, "T2M": 20.0, "SMAP_SM": sm})
    out = climate.soil_moisture_stats(df)["A"]
    assert out["summary"]["years_used"] == 1 and out["summary"]["mean_m3m3"] == 0.2

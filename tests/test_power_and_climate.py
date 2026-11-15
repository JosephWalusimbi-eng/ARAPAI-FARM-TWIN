"""Offline tests. The fixture is SYNTHETIC: it mimics the NASA POWER response
shape with simple, known numbers so the statistics can be checked exactly.
It is not real climate data."""
from __future__ import annotations

import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import climate, config, power  # noqa: E402


def synthetic_power_response(start="2020-01-01", end="2021-12-31", rain=2.0, missing=()):
    days = pd.date_range(start, end, freq="D")
    keys = [d.strftime("%Y%m%d") for d in days]
    params = {
        "T2M": {k: 24.0 for k in keys},
        "T2M_MAX": {k: 30.0 for k in keys},
        "T2M_MIN": {k: 18.0 for k in keys},
        "PRECTOTCORR": {k: rain for k in keys},
        "RH2M": {k: 70.0 for k in keys},
        "ALLSKY_SFC_SW_DWN": {k: 20.0 for k in keys},
        "GWETROOT": {k: 0.5 for k in keys},
    }
    for k in missing:
        params["PRECTOTCORR"][k] = -999.0
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [33.61, 1.71, 1100.0]},
        "properties": {"parameter": params},
        "header": {"title": "NASA/POWER (synthetic test fixture)", "fill_value": -999.0,
                   "start": keys[0], "end": keys[-1]},
        "parameters": {"PRECTOTCORR": {"units": "mm/day"}, "T2M": {"units": "C"}},
        "messages": [],
    }


def test_fill_values_become_nan_not_zero():
    raw = synthetic_power_response(missing=["20200105"])
    df = power.to_dataframe(raw)
    assert np.isnan(df.loc["2020-01-05", "PRECTOTCORR"])
    assert df.loc["2020-01-06", "PRECTOTCORR"] == 2.0
    assert isinstance(df.index, pd.DatetimeIndex)


def test_annual_and_season_totals():
    df = power.to_dataframe(synthetic_power_response(rain=2.0))
    prof = climate.climate_profile(df)
    by_year = {r["year"]: r["rain_mm"] for r in prof["annual"]["per_year"]}
    assert by_year[2020] == 366 * 2.0  # leap year
    assert by_year[2021] == 365 * 2.0
    season_a = prof["seasons"]["A"]  # Mar-Jun = 31+30+31+30 = 122 days
    assert season_a["summary"]["mean_rain_mm"] == 122 * 2.0
    assert season_a["summary"]["years_used"] == 2
    assert season_a["summary"]["cv"] == 0.0
    jan = prof["monthly_climatology"][0]
    assert jan["month"] == 1 and jan["PRECTOTCORR"] == 62.0


def test_incomplete_season_is_excluded_not_guessed():
    # Knock out 20 days of 2020 season A (> 5% of 122 days).
    missing = [d.strftime("%Y%m%d") for d in pd.date_range("2020-04-01", periods=20)]
    df = power.to_dataframe(synthetic_power_response(missing=missing))
    season = climate.season_stats(df)["A"]
    rows = {r["year"]: r for r in season["per_year"]}
    assert rows[2020].get("rain_mm") is None
    assert rows[2021]["rain_mm"] == 244.0
    assert season["summary"]["years_used"] == 1


def test_longest_dry_spell():
    rain = pd.Series([5, 0, 0, 0, 3, 0, 0, 2, 0.5, 0.2], dtype=float)
    assert climate._longest_dry_spell(rain, 1.0) == 3


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._data, self.text = status, data, str(data)

    def json(self):
        return self._data


def test_fetch_uses_cache(tmp_path, monkeypatch):
    calls = []

    def fake_get(url, params, timeout, headers):
        calls.append(params)
        return _Resp(200, synthetic_power_response())

    monkeypatch.setattr(power.httpx, "get", fake_get)
    a = power.fetch_daily(1.71, 33.61, "20200101", "20211231", cache_dir=tmp_path, source="api")
    b = power.fetch_daily(1.71, 33.61, "20200101", "20211231", cache_dir=tmp_path, source="api")
    assert a == b and len(calls) == 1
    assert calls[0]["community"] == "AG"
    assert "PRECTOTCORR" in calls[0]["parameters"]


def test_fetch_reports_api_error(tmp_path, monkeypatch):
    monkeypatch.setattr(power.httpx, "get",
                        lambda *a, **k: _Resp(422, {"messages": ["bad date range"]}))
    # A bad request is a real error: it must not silently fall back to S3.
    monkeypatch.setattr(power, "_fetch_s3", lambda *a: pytest.fail("should not fall back"))
    with pytest.raises(power.PowerError, match="bad date range"):
        power.fetch_daily(1.71, 33.61, cache_dir=tmp_path, source="auto")


def _boom(*a, **k):
    raise httpx.ConnectError("connection reset")


def test_fetch_gives_up_on_network_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(power.httpx, "get", _boom)
    monkeypatch.setattr(power.time, "sleep", lambda s: None)
    with pytest.raises(power.PowerUnavailable, match="unreachable"):
        power.fetch_daily(1.71, 33.61, cache_dir=tmp_path, retries=2, source="api")


def test_auto_falls_back_to_s3_and_caches(tmp_path, monkeypatch):
    s3_calls = []
    monkeypatch.setattr(power.httpx, "get", _boom)
    monkeypatch.setattr(power.time, "sleep", lambda s: None)

    def fake_s3(*a):
        s3_calls.append(a)
        return synthetic_power_response()

    monkeypatch.setattr(power, "_fetch_s3", fake_s3)
    a = power.fetch_daily(1.71, 33.61, cache_dir=tmp_path, retries=1, source="auto")
    # Second call must come from the S3 cache without touching the network.
    monkeypatch.setattr(power.httpx, "get", lambda *a, **k: pytest.fail("network used"))
    b = power.fetch_daily(1.71, 33.61, cache_dir=tmp_path, source="auto")
    assert a == b and len(s3_calls) == 1


def test_s3_reader_builds_api_shaped_response(monkeypatch, tmp_path):
    xr = pytest.importorskip("xarray")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)  # block checkpoints go here
    times = pd.date_range("2020-01-01", "2020-01-10", freq="D")
    lats, lons = np.array([1.0, 1.5, 2.0]), np.array([33.125, 33.75, 34.375])
    rain = np.full((len(times), 3, 3), 4.0, dtype="float32")
    rain[2, 1, 1] = np.nan  # a missing day at the nearest cell
    ds = xr.Dataset(
        {"PRECTOTCORR": (("time", "lat", "lon"), rain, {"units": "mm/day"}),
         "T2M": (("time", "lat", "lon"), np.full_like(rain, 25.0), {"units": "C"})},
        coords={"time": times, "lat": lats, "lon": lons})
    monkeypatch.setattr(xr, "open_zarr", lambda *a, **k: ds)

    raw = power._fetch_s3(1.71, 33.61, "20200102", "20200105", ["PRECTOTCORR", "T2M", "ALLSKY_SFC_SW_DWN"])
    assert raw["geometry"]["coordinates"][:2] == [33.75, 1.5]  # nearest cell
    assert list(raw["properties"]["parameter"]["PRECTOTCORR"]) == ["20200102", "20200103", "20200104", "20200105"]
    assert raw["properties"]["parameter"]["PRECTOTCORR"]["20200103"] == power.FILL
    assert "ALLSKY_SFC_SW_DWN" in raw["messages"][0]
    df = power.to_dataframe(raw)
    assert np.isnan(df.loc["2020-01-03", "PRECTOTCORR"]) and df.loc["2020-01-04", "PRECTOTCORR"] == 4.0
    assert power.units(raw) == {"PRECTOTCORR": "mm/day", "T2M": "C"}


def test_api_climate_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from farm_twin import api

    from farm_twin.sources import NotFetched

    monkeypatch.setattr(power, "fetch_daily", lambda *a, **k: synthetic_power_response())

    def not_fetched(*a, **k):
        raise NotFetched("not fetched")

    monkeypatch.setattr(api.SOURCES["imerg"], "fetch", not_fetched)
    monkeypatch.setattr(api.SOURCES["smap"], "fetch", not_fetched)
    client = TestClient(api.app)
    r = client.get("/api/climate")
    assert r.status_code == 200
    body = r.json()
    assert body["units"]["PRECTOTCORR"] == "mm/day"
    assert body["profile"]["seasons"]["B"]["summary"]["years_used"] == 2
    assert body["sources"]["smap"]["status"] == "not_fetched"
    assert client.get("/api/climate?start=2020&end=20211231").status_code == 422
    assert client.get("/api/climate?include=power,nope").status_code == 422
    assert client.get("/").status_code == 200
    assert {s["key"] for s in client.get("/api/sources").json()["sources"]} == {"power", "imerg", "smap", "era5"}


def test_pilot_farm_weights_sum_to_one():
    w = config.PILOT_FARM["farmer_preferences"]
    assert len(w) == 5
    assert abs(sum(w.values()) - 1.0) < 1e-9
    assert all(v is None for k, v in config.PILOT_FARM["soil_profile"].items() if k != "data_source")


def test_api_rotations_endpoint(monkeypatch):
    from fastapi.testclient import TestClient
    from farm_twin import api

    # Two synthetic years at 4 mm/day: seasons of ~490 mm, no dry spells.
    monkeypatch.setattr(power, "fetch_daily", lambda *a, **k: synthetic_power_response(rain=4.0))
    client = TestClient(api.app)
    r = client.get("/api/rotations?top=3")
    assert r.status_code == 200, r.text
    body = r.json()
    assert len(body["rotations"]) == 3
    assert body["meta"]["rotations_feasible"] == 357
    assert body["meta"]["confidence"]["level"] == "Low"
    w = client.get("/api/rotations?top=1&soil_health=0&farmer_utility=1").json()["meta"]["weights"]
    assert w["H"] == 0 and abs(sum(w.values()) - 1) < 0.005
    assert client.get("/api/rotations?alpha=2").status_code == 422


def test_api_languages_and_translated_rotations(monkeypatch):
    from fastapi.testclient import TestClient
    from farm_twin import api

    monkeypatch.setattr(power, "fetch_daily", lambda *a, **k: synthetic_power_response(rain=4.0))
    client = TestClient(api.app)
    codes = {l["code"] for l in client.get("/api/languages").json()["languages"]}
    assert {"en", "lg", "sw", "teo"} <= codes
    assert client.get("/api/i18n/sw").json()["strings"]["crop.maize"] == "Mahindi"
    assert client.get("/api/i18n/zz").status_code == 404
    body = client.get("/api/rotations?top=1&lang=lg").json()
    assert body["meta"]["language"]["code"] == "lg"
    assert "Kasooli" in body["controls"][0]["display"]
    assert client.get("/api/rotations?lang=zz").status_code == 422

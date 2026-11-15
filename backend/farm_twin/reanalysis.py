"""ERA5 (ECMWF/Copernicus reanalysis, 0.25 deg) daily rain and reference evapotranspiration (ET0).

Used as a THIRD rainfall opinion beside NASA POWER and GPM IMERG (which differ by 15-25%), and
ET0 is the input the planned v0.2 water-need upgrade (crop need = Kc x ET0) will use.
Evidence only: it does not change scores in spec v0.1.1.

Access: the open-meteo.com archive API, which serves ERA5 for a point with no login (ERA5 itself
is on the Copernicus CDS, which needs an account). ET0 there is FAO-56 Penman-Monteith computed
from ERA5 fields by Open-Meteo, not an ERA5 product. Only the farm's coordinates are sent.
"""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import climate, config
from .storage import atomic_write_text, read_json_safe

URL = "https://archive-api.open-meteo.com/v1/archive"
START, END = "2001-01-01", "2025-12-31"


class ReanalysisError(Exception):
    pass


def cache_path(lat: float, lon: float, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.CACHE_DIR) / f"era5_openmeteo_{lat:.4f}_{lon:.4f}_{START}_{END}.json"


def fetch(lat: float, lon: float, *, refresh: bool = False, cache_dir: Path | None = None) -> dict:
    path = cache_path(lat, lon, cache_dir)
    if not refresh and (cached := read_json_safe(path)):
        return cached
    params = {"latitude": lat, "longitude": lon, "start_date": START, "end_date": END, "models": "era5",
              "daily": "precipitation_sum,et0_fao_evapotranspiration", "timezone": "UTC"}
    try:
        r = httpx.get(URL, params=params, timeout=120)
        r.raise_for_status()
        d = r.json()
    except (httpx.HTTPError, ValueError) as exc:
        raise ReanalysisError(f"ERA5 request failed: {exc}") from exc
    daily = d["daily"]
    out = {"dataset": "ERA5 reanalysis, 0.25 deg, via Open-Meteo archive API",
           "grid_latitude": d.get("latitude"), "grid_longitude": d.get("longitude"),
           "units": d.get("daily_units"), "time": daily["time"],
           "rain_mm": daily["precipitation_sum"], "et0_mm": daily["et0_fao_evapotranspiration"]}
    atomic_write_text(path, json.dumps(out))
    return out


def load(lat: float, lon: float, cache_dir: Path | None = None) -> dict | None:
    return read_json_safe(cache_path(lat, lon, cache_dir))


def summarize(era: dict, other: pd.DataFrame | None = None, seasons: dict = config.SEASONS) -> dict:
    """Season rain and ET0 from ERA5, and (if `other` has them) the gap to POWER and IMERG."""
    df = pd.DataFrame({"ERA5_RAIN": era["rain_mm"], "ERA5_ET0": era["et0_mm"]},
                      index=pd.DatetimeIndex(pd.to_datetime(era["time"]), name="date"), dtype=float)
    out = {"dataset": era["dataset"], "grid": {"latitude": era.get("grid_latitude"),
                                               "longitude": era.get("grid_longitude")}, "seasons": {}}
    for key, spec in seasons.items():
        rain = climate.season_totals(df, "ERA5_RAIN", spec["months"])
        et0 = climate.season_totals(df, "ERA5_ET0", spec["months"])
        if len(rain) < 3:
            continue
        r = np.array(list(rain.values()))
        s = {"years": len(r), "era5_rain_mean_mm": round(float(r.mean()), 1),
             "era5_rain_p20_mm": round(float(np.percentile(r, 20)), 1),
             "era5_rain_p80_mm": round(float(np.percentile(r, 80)), 1),
             "era5_et0_mean_mm": round(float(np.mean(list(et0.values()))), 1) if et0 else None}
        if other is not None:
            for name, col in (("power", climate.RAIN), ("imerg", climate.IMERG_RAIN)):
                if col in other and other[col].notna().any():
                    o = climate.season_totals(other, col, spec["months"])
                    yrs = sorted(set(o) & set(rain))
                    if len(yrs) >= 3:
                        a, b = np.array([o[y] for y in yrs]), np.array([rain[y] for y in yrs])
                        s[f"{name}_mean_mm"] = round(float(a.mean()), 1)
                        s[f"era5_minus_{name}_pct"] = round(100 * (b.mean() - a.mean()) / a.mean(), 1)
                        s[f"correlation_with_{name}"] = round(float(np.corrcoef(a, b)[0, 1]), 2)
        out["seasons"][key] = s
    return out

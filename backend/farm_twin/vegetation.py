"""Vegetation greenness (NDVI) at the farm from NASA MODIS (MOD13Q1 / MYD13Q1, 250 m, 16-day).

What it is for: showing WHEN the land around the farm is green and when it is bare, and
checking our season windows (A: Mar-Jun, B: Aug-Nov) against what the satellite sees.
It is evidence, not a score input (engine spec v0.1.1 is unchanged).

Honest limits, shown in the app too:
- A MODIS pixel is 250 m (6.25 ha) and the pilot farm is 2 ha, so the value is the average of
  the farm AND its neighbours. Neighbouring perennials can hide bare-field periods.
- The farm boundary is an assumption: a 140 m square (about 2 ha) centred on the point.
- NDVI says how green, not which crop; clouds and haze leave gaps (kept missing, never zero).

Fetch once with `python scripts/fetch_vegetation.py`; the app reads only the cache.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, planetary
from .storage import atomic_write_text, read_json_safe

COLLECTION = "modis-13Q1-061"
ASSET_NDVI, ASSET_REL = "250m_16_days_NDVI", "250m_16_days_pixel_reliability"
SCALE = 0.0001
VALID = (-2000, 10000)          # MODIS NDVI valid range (fill value is -3000)
GOOD_RELIABILITY = (0, 1)       # 0 good, 1 marginal; 2 snow/ice, 3 cloudy are dropped
DEFAULT_YEARS = (2001, 2025)
WORKERS = 10
BARE_FRACTION = 0.25            # "low cover" = within the bottom quarter of the annual NDVI range


class VegetationError(Exception):
    pass


def cache_path(lat: float, lon: float, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.CACHE_DIR) / f"vegetation_modis_{lat:.4f}_{lon:.4f}.json"


def _rasterio():
    from .sources.generic import _rasterio as r
    return r()


def _read_pixel(item: dict, lat: float, lon: float) -> dict | None:
    rasterio = _rasterio()
    from rasterio.warp import transform
    from rasterio.windows import Window
    out: dict = {}
    for name, asset in (("ndvi", ASSET_NDVI), ("rel", ASSET_REL)):
        href = item["assets"][asset]["href"]
        for attempt in range(4):
            try:
                with rasterio.Env(GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2"):
                    with rasterio.open(planetary.signed(href, renew=attempt > 0)) as ds:
                        x, y = transform("EPSG:4326", ds.crs, [lon], [lat])
                        r, c = ds.index(x[0], y[0])
                        out[name] = int(ds.read(1, window=Window(c, r, 1, 1))[0, 0])
                        if name == "ndvi":
                            cx, cy = ds.xy(r, c)
                            lo, la = transform(ds.crs, "EPSG:4326", [cx], [cy])
                            out["pixel"] = (round(la[0], 5), round(lo[0], 5))
                break
            except Exception:  # noqa: BLE001 - network/token hiccups: retry, then give up on this date
                if attempt == 3:
                    return None
    v = out.get("ndvi")
    ok = v is not None and VALID[0] <= v <= VALID[1] and out.get("rel") in GOOD_RELIABILITY
    date = (item["properties"].get("start_datetime") or item["properties"].get("datetime") or "")[:10]
    return {"date": date, "sensor": item["id"][:3], "ndvi": round(v * SCALE, 4) if ok else None,
            "pixel": out.get("pixel")}


def fetch(lat: float, lon: float, years: tuple[int, int] = DEFAULT_YEARS, *, refresh: bool = False,
          cache_dir: Path | None = None, log=print) -> dict:
    """Read the MODIS NDVI series for the pixel containing the farm and cache it (resumable)."""
    path = cache_path(lat, lon, cache_dir)
    store = None if refresh else read_json_safe(path)
    if not store:
        store = {"records": {}, "years_done": []}
    for year in range(years[0], years[1] + 1):
        if year in store["years_done"]:
            continue
        items = planetary.search(COLLECTION, lat, lon, f"{year}-01-01", f"{year}-12-31")
        todo = [i for i in items if (i["properties"].get("start_datetime") or "")[:10] + i["id"][:3]
                not in store["records"]]
        with ThreadPoolExecutor(WORKERS) as pool:
            rows = list(pool.map(lambda it: _read_pixel(it, lat, lon), todo))
        failed = sum(r is None for r in rows)
        for r in rows:
            if r:
                store["records"][r["date"] + r["sensor"]] = r
        if failed == 0:
            store["years_done"].append(year)
        store["saved_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        atomic_write_text(path, json.dumps(store))
        log(f"  {year}: {len(items)} composites, {failed} unreadable")
    return store


def load(lat: float, lon: float, cache_dir: Path | None = None) -> dict | None:
    return read_json_safe(cache_path(lat, lon, cache_dir))


def _pct(xs: list[float], p: float) -> float:
    xs = sorted(xs)
    k = (len(xs) - 1) * p
    lo, hi = int(k), min(int(k) + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(store: dict, seasons: dict = config.SEASONS) -> dict:
    """Monthly NDVI climatology, season stats, peak months and low-cover months."""
    rows = [(dt.date.fromisoformat(r["date"]), r["ndvi"]) for r in store["records"].values()]
    valid = [(d, v) for d, v in rows if v is not None]
    if not valid:
        raise VegetationError("no valid NDVI values in the cache")
    # one value per calendar month per year (mean of that month's composites)
    by_ym: dict[tuple[int, int], list[float]] = {}
    for d, v in valid:
        by_ym.setdefault((d.year, d.month), []).append(v)
    ym = {k: statistics.fmean(v) for k, v in by_ym.items()}
    monthly = {}
    for m in range(1, 13):
        vals = [v for (y, mm), v in ym.items() if mm == m]
        if vals:
            monthly[m] = {"mean": round(statistics.fmean(vals), 3), "p20": round(_pct(vals, 0.2), 3),
                          "p80": round(_pct(vals, 0.8), 3), "years": len(vals)}
    means = {m: s["mean"] for m, s in monthly.items()}
    lo, hi = min(means.values()), max(means.values())
    low_cover = [m for m, v in means.items() if hi > lo and v <= lo + BARE_FRACTION * (hi - lo)]
    season_out = {}
    for key, s in seasons.items():
        per_year = []
        for y in sorted({y for (y, _) in ym}):
            vs = [ym[(y, m)] for m in s["months"] if (y, m) in ym]
            if len(vs) == len(s["months"]):
                per_year.append((y, statistics.fmean(vs)))
        vals = [v for _, v in per_year]
        if vals:
            season_out[key] = {"label": s["label"], "months": s["months"], "years": len(vals),
                               "mean": round(statistics.fmean(vals), 3),
                               "p20": round(_pct(vals, 0.2), 3), "p80": round(_pct(vals, 0.8), 3),
                               "greenest_year": max(per_year, key=lambda t: t[1])[0],
                               "least_green_year": min(per_year, key=lambda t: t[1])[0]}
    # two largest local maxima of the monthly curve = the two rainy-season greenness peaks
    ring = [means.get(m) for m in range(1, 13)]
    peaks = sorted((m for m in range(1, 13) if ring[m - 1] is not None
                    and ring[m - 1] >= (ring[m - 2] or 0) and ring[m - 1] >= (ring[m % 12] or 0)),
                   key=lambda m: -means[m])[:2]
    window_months = {m for s in seasons.values() for m in s["months"]}
    n_valid, n_all = len(valid), len(rows)
    pix = next((r["pixel"] for r in store["records"].values() if r.get("pixel")), None)
    return {
        "dataset": "NASA MODIS Terra+Aqua MOD13Q1/MYD13Q1 v061, NDVI, 250 m, 16-day",
        "access": "Microsoft Planetary Computer (public STAC, no login)",
        "period": {"start": min(d for d, _ in valid).isoformat(), "end": max(d for d, _ in valid).isoformat()},
        "pixel": {"latitude": pix[0], "longitude": pix[1]} if pix else None,
        "composites": {"total": n_all, "valid": n_valid, "valid_fraction": round(n_valid / n_all, 3)},
        "monthly": monthly,
        "seasons": season_out,
        "greenness_peaks_months": sorted(peaks),
        "low_cover_months": sorted(low_cover),
        "range": {"min_month_mean": lo, "max_month_mean": hi},
        "season_window_check": {
            "windows_months": sorted(window_months),
            "peaks_inside_windows": all(m in window_months or (m % 12) + 1 in window_months
                                        or ((m - 2) % 12) + 1 in window_months for m in peaks),
        },
        "footprint_note": ("A MODIS pixel is 250 m (about 6 ha); the pilot farm (2 ha, assumed 140 m "
                           "square) is smaller, so this is the greenness of the farm and its "
                           "neighbours together. Neighbouring perennial cover can hide bare-field periods."),
    }

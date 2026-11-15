"""Field-scale greenness from Landsat 8/9 (30 m), read on the farm's own 140 m square.

MODIS (250 m) cannot see a 2 ha farm. Landsat can: the 140 m square is about 5 x 5 pixels.
For each clear scene we average NDVI over the clear pixels of that square, and record the share
of those pixels that look bare (NDVI < 0.25). That share is the "bare-field" signal.

Honest limits (also shown in the app):
- The 140 m square is an assumption (no surveyed boundary); it may include a track or a neighbour.
- Landsat passes every 8 days (two satellites) but clouds remove many scenes in the rains,
  so months with few clear looks (the `n` column) are less certain.
- NDVI says how green the land is, not which crop is growing or why it is bare.
"""
from __future__ import annotations

import datetime as dt
import json
import statistics
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config, planetary, vegetation
from .storage import atomic_write_text, read_json_safe

COLLECTION = "landsat-c2-l2"
HALF_M = 70.0
BARE_NDVI = 0.25
MIN_CLEAR_FRACTION = 0.6        # a scene needs at least 60% clear pixels in the square
CLOUD_QUERY = {"eo:cloud_cover": {"lt": 85}}
DEFAULT_YEARS = (2013, 2025)    # Landsat 8 onward (Landsat 9 from late 2021)
WORKERS = 8
BAD_QA_BITS = 0b11111           # fill, dilated cloud, cirrus, cloud, cloud shadow
MIN_VALID_SCENES_PER_MONTH = 8  # below this a month is flagged as thin


def cache_path(lat: float, lon: float, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.CACHE_DIR) / f"vegetation_landsat_{lat:.4f}_{lon:.4f}.json"


def _read_scene(item: dict, lat: float, lon: float) -> dict | None:
    rasterio = vegetation._rasterio()
    import numpy as np
    from rasterio.warp import transform
    from rasterio.windows import Window
    arrays = {}
    for name in ("red", "nir08", "qa_pixel"):
        href = item["assets"][name]["href"]
        for attempt in range(4):
            try:
                with rasterio.Env(GDAL_HTTP_MAX_RETRY="3", GDAL_HTTP_RETRY_DELAY="2"):
                    with rasterio.open(planetary.signed(href, renew=attempt > 0)) as ds:
                        x, y = transform("EPSG:4326", ds.crs, [lon], [lat])
                        r0, c0 = ds.index(x[0], y[0])
                        h = int(round(HALF_M / ds.res[0]))
                        arrays[name] = ds.read(1, window=Window(c0 - h, r0 - h, 2 * h + 1, 2 * h + 1)).astype("int64")
                break
            except Exception:  # noqa: BLE001 - network hiccup: retry, then skip this scene
                if attempt == 3:
                    return None
    red = arrays["red"] * 0.0000275 - 0.2
    nir = arrays["nir08"] * 0.0000275 - 0.2
    clear = ((arrays["qa_pixel"] & BAD_QA_BITS) == 0) & (arrays["red"] > 0) & (arrays["nir08"] > 0)
    date = item["properties"]["datetime"][:10]
    if clear.mean() < MIN_CLEAR_FRACTION:
        return {"date": date, "ndvi": None, "clear": round(float(clear.mean()), 2)}
    with np.errstate(divide="ignore", invalid="ignore"):
        ndvi = (nir - red) / (nir + red)
    v = ndvi[clear]
    v = v[np.isfinite(v)]
    if v.size == 0:
        return {"date": date, "ndvi": None, "clear": 0.0}
    return {"date": date, "ndvi": round(float(v.mean()), 4), "min": round(float(v.min()), 3),
            "max": round(float(v.max()), 3), "bare_share": round(float((v < BARE_NDVI).mean()), 3),
            "clear": round(float(clear.mean()), 2), "platform": item["properties"].get("platform")}


def fetch(lat: float, lon: float, years: tuple[int, int] = DEFAULT_YEARS, *, refresh: bool = False,
          cache_dir: Path | None = None, log=print) -> dict:
    path = cache_path(lat, lon, cache_dir)
    store = None if refresh else read_json_safe(path)
    if not store:
        store = {"records": {}, "years_done": [], "square_m": 2 * HALF_M}
    for year in range(years[0], years[1] + 1):
        if year in store["years_done"]:
            continue
        items = planetary.search(COLLECTION, lat, lon, f"{year}-01-01", f"{year}-12-31", query=CLOUD_QUERY)
        todo = [i for i in items if i["id"] not in store["records"]]
        with ThreadPoolExecutor(WORKERS) as pool:
            rows = list(pool.map(lambda it: _read_scene(it, lat, lon), todo))
        failed = 0
        for it, r in zip(todo, rows):
            if r is None:
                failed += 1
            else:
                store["records"][it["id"]] = r
        # A scene that fails twice (a broken file on the server) is skipped, not retried forever.
        if failed == 0 or year in store.setdefault("retried", []):
            store["years_done"].append(year)
        elif failed:
            store["retried"].append(year)
        store["saved_at"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        atomic_write_text(path, json.dumps(store))
        log(f"  {year}: {len(items)} scenes, {failed} unreadable")
    return store


def load(lat: float, lon: float, cache_dir: Path | None = None) -> dict | None:
    return read_json_safe(cache_path(lat, lon, cache_dir))


def summarize(store: dict, seasons: dict = config.SEASONS) -> dict:
    rows = [(dt.date.fromisoformat(r["date"]), r) for r in store["records"].values()]
    ok = [(d, r) for d, r in rows if r.get("ndvi") is not None]
    if not ok:
        raise vegetation.VegetationError("no clear Landsat scenes in the cache")
    monthly = {}
    for m in range(1, 13):
        rs = [r for d, r in ok if d.month == m]
        if not rs:
            continue
        v = [r["ndvi"] for r in rs]
        monthly[m] = {"mean": round(statistics.fmean(v), 3), "p20": round(vegetation._pct(v, .2), 3),
                      "p80": round(vegetation._pct(v, .8), 3), "n": len(rs),
                      "bare_share": round(statistics.fmean(r["bare_share"] for r in rs), 3),
                      "thin": len(rs) < MIN_VALID_SCENES_PER_MONTH}
    means = {m: s["mean"] for m, s in monthly.items()}
    season_out = {}
    for key, s in seasons.items():
        rs = [r for d, r in ok if d.month in s["months"]]
        if rs:
            season_out[key] = {"label": s["label"], "months": s["months"], "scenes": len(rs),
                               "mean": round(statistics.fmean(r["ndvi"] for r in rs), 3),
                               "bare_share": round(statistics.fmean(r["bare_share"] for r in rs), 3)}
    ranked = sorted(means, key=lambda m: means[m])
    return {
        "dataset": "NASA/USGS Landsat 8 and 9 Collection 2 Level-2 surface reflectance, 30 m",
        "access": "Microsoft Planetary Computer (public STAC, no login)",
        "square_m": store.get("square_m", 2 * HALF_M),
        "period": {"start": min(d for d, _ in ok).isoformat(), "end": max(d for d, _ in ok).isoformat()},
        "scenes": {"total": len(rows), "clear": len(ok)},
        "monthly": monthly, "seasons": season_out,
        "least_green_months": sorted(ranked[:3]), "greenest_months": sorted(ranked[-2:]),
        "bare_threshold": BARE_NDVI,
        "limit_note": ("The farm square (140 m) is assumed. Clouds remove many scenes in the rainy months, so "
                       "months with few clear looks are less certain."),
    }

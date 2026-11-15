"""Terrain around the farm from NASADEM (NASA's reprocessed SRTM, 1 arc-second, about 30 m).

What it gives: elevation, slope and where the farm sits in its landscape (a rise, a slope or a
low spot that collects runoff). It feeds an erosion / runoff note, not the rotation scores.

Honest limits:
- At 30 m the 2 ha farm (assumed 140 m square) is only about 5 x 5 elevation cells, so slope is
  landscape-scale context, not a field survey. Small dips and bunds are invisible.
- SRTM measured 2000: it sees treetops and buildings, and changes since then are missed.
- Frost risk is not computed: at this latitude and elevation the minimum temperature in POWER
  never gets near freezing (checked in climate data), so it is shown as not applicable.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

from . import config, planetary
from .storage import atomic_write_text, read_json_safe

COLLECTION = "nasadem"
HALF_FARM_M = 70.0          # 140 m square, centred on the point (user decision, 14 Nov 2026)
HALF_WINDOW_M = 1000.0      # landscape window: 2 km square
# FAO-style slope classes, in percent
SLOPE_CLASSES = ((2, "flat"), (5, "gently sloping"), (10, "sloping"), (15, "strongly sloping"),
                 (1e9, "steep"))


class TerrainError(Exception):
    pass


def cache_path(lat: float, lon: float, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.CACHE_DIR) / f"terrain_nasadem_{lat:.4f}_{lon:.4f}.json"


def slope_class(percent: float) -> str:
    return next(name for limit, name in SLOPE_CLASSES if percent < limit)


def _horn(z, dx: float, dy: float):
    """Slope (percent) and aspect (degrees clockwise from north, downslope direction) per cell."""
    import numpy as np
    a, b, c = z[:-2, :-2], z[:-2, 1:-1], z[:-2, 2:]
    d, f = z[1:-1, :-2], z[1:-1, 2:]
    g, h, i = z[2:, :-2], z[2:, 1:-1], z[2:, 2:]
    dzdx = ((c + 2 * f + i) - (a + 2 * d + g)) / (8 * dx)   # east is +
    dzdy = ((g + 2 * h + i) - (a + 2 * b + c)) / (8 * dy)   # row index grows southward
    slope = np.hypot(dzdx, dzdy) * 100
    aspect = (np.degrees(np.arctan2(-dzdx, dzdy)) + 360) % 360  # bearing water flows toward (downhill)
    return slope, aspect


def fetch(lat: float, lon: float, *, refresh: bool = False, cache_dir: Path | None = None) -> dict:
    path = cache_path(lat, lon, cache_dir)
    if not refresh and (cached := read_json_safe(path)):
        return cached
    from .sources.generic import _rasterio
    rasterio = _rasterio()
    import numpy as np
    from rasterio.windows import Window

    items = planetary.search(COLLECTION, lat, lon, "2000-01-01", "2001-01-01")
    if not items:
        raise TerrainError("no NASADEM tile covers this point")
    href = items[0]["assets"]["elevation"]["href"]
    with rasterio.open(planetary.signed(href)) as ds:
        r0, c0 = ds.index(lon, lat)
        res_deg = ds.res[0]
        dy = res_deg * 111_320.0
        dx = dy * math.cos(math.radians(lat))
        half = int(HALF_WINDOW_M / dx) + 1
        win = Window(c0 - half, r0 - half, 2 * half + 1, 2 * half + 1)
        z = ds.read(1, window=win).astype("float64")
        nod = ds.nodata
    if nod is not None:
        z[z == nod] = np.nan
    if np.isnan(z).mean() > 0.05:
        raise TerrainError("too many missing elevation cells around the farm")
    z = np.where(np.isnan(z), np.nanmean(z), z)
    slope, aspect = _horn(z, dx, dy)             # shrinks the window by one cell on each side
    zc = z[1:-1, 1:-1]
    n = zc.shape[0]
    centre = n // 2
    fh = max(1, int(round(HALF_FARM_M / dx)))
    sl = (slice(centre - fh, centre + fh + 1),) * 2
    farm_z, farm_slope = zc[sl], slope[sl]
    fs = float(farm_slope.mean())
    # mean downslope direction of the farm cells (vector mean, so 359 and 1 average to 0)
    ang = np.radians(aspect[sl])
    mean_dir = float((math.degrees(math.atan2(np.sin(ang).mean(), np.cos(ang).mean())) + 360) % 360)
    pct_lower = float((zc < farm_z.mean()).mean() * 100)   # share of the 2 km window lower than the farm
    compass = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((mean_dir + 22.5) // 45) % 8]
    if pct_lower > 70:
        position = "high ground: water drains away from the farm"
    elif pct_lower < 30:
        position = "low ground: runoff from higher land may collect here"
    else:
        position = "mid-slope: neither a clear rise nor a low spot"
    out = {
        "dataset": "NASADEM (NASA reprocessed SRTM), 1 arc-second (~30 m), elevations from the 2000 shuttle mission",
        "access": "Microsoft Planetary Computer (public STAC, no login)",
        "farm_footprint": f"{2 * fh + 1} x {2 * fh + 1} cells (~{round((2 * fh + 1) * dx)} m square, assumed 140 m)",
        "elevation_m": {"farm_mean": round(float(farm_z.mean()), 1), "farm_min": round(float(farm_z.min()), 1),
                        "farm_max": round(float(farm_z.max()), 1),
                        "window_min": round(float(zc.min()), 1), "window_max": round(float(zc.max()), 1)},
        "slope_percent": {"farm_mean": round(fs, 2), "farm_max": round(float(farm_slope.max()), 2),
                          "window_mean": round(float(slope.mean()), 2),
                          "window_p90": round(float(np.percentile(slope, 90)), 2)},
        "slope_class": slope_class(fs),
        "downslope_direction": {"degrees": round(mean_dir), "compass": compass},
        "landscape_position": {"percent_of_window_lower_than_farm": round(pct_lower, 1), "reading": position},
        "window": f"{n * dx / 1000:.1f} km square around the farm",
        "frost": {"applicable": False,
                  "note": "Not applicable here: the farm is about 1.7 deg N at ~1,100 m; see POWER minimum temperatures."},
        "limits": ("At 30 m the farm is a handful of cells: this is landscape context, not a field survey. "
                   "SRTM dates from 2000 and sees treetops and buildings."),
    }
    atomic_write_text(path, json.dumps(out))
    return out


def load(lat: float, lon: float, cache_dir: Path | None = None) -> dict | None:
    return read_json_safe(cache_path(lat, lon, cache_dir))

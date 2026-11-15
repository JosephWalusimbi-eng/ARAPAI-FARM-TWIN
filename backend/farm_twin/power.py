"""NASA POWER daily point data with on-disk caching and two access routes.

1. "api": the POWER REST API (power.larc.nasa.gov).
2. "s3":  NASA POWER's official Zarr archive on AWS Open Data (s3://nasa-power),
          read over public HTTPS. Same MERRA-2 daily (LST) data, no account needed.
          Used automatically when the API is unreachable (some networks block it).

Both routes return the same dict shape as the API, so the rest of the code doesn't care.

Usage:
    raw = fetch_daily(lat=1.71, lon=33.61)      # source="auto": API, then S3
    df = to_dataframe(raw)                       # daily DataFrame, fill values -> NaN
"""
from __future__ import annotations

import hashlib
import json
import math
import shutil
import time
from pathlib import Path

import httpx
import numpy as np
import pandas as pd

from . import config
from .storage import atomic_write_text, read_json_safe

FILL = -999.0


class PowerError(RuntimeError):
    """NASA POWER returned an error or an unusable response."""


class PowerUnavailable(PowerError):
    """NASA POWER could not be reached (network failure, 5xx, rate limit)."""


def _cache_path(lat: float, lon: float, start: str, end: str, params: list[str],
                cache_dir: Path, source: str) -> Path:
    key = f"{source}|{config.POWER_COMMUNITY}|{','.join(sorted(params))}"
    digest = hashlib.sha1(key.encode()).hexdigest()[:10]
    return cache_dir / f"power_{source}_{lat:.4f}_{lon:.4f}_{start}_{end}_{digest}.json"


def fetch_daily(lat: float, lon: float, start: str = config.DEFAULT_START,
                end: str = config.DEFAULT_END, params: list[str] | None = None, *,
                source: str | None = None, cache_dir: Path = config.CACHE_DIR,
                refresh: bool = False, retries: int = 3, timeout: float = 120.0) -> dict:
    """Return NASA POWER daily data for a point, using the cache when possible.

    source: "auto" (API, falling back to S3), "api" or "s3". Defaults to config.POWER_SOURCE.
    """
    params = params or config.POWER_PARAMETERS
    source = source or config.POWER_SOURCE
    if source not in ("auto", "api", "s3"):
        raise ValueError(f"unknown source {source!r}")
    routes = ["api", "s3"] if source == "auto" else [source]

    if not refresh:
        for route in routes:
            cached = read_json_safe(_cache_path(lat, lon, start, end, params, cache_dir, route))
            if cached is not None:
                return cached

    errors = []
    for route in routes:
        try:
            if route == "api":
                data = _fetch_api(lat, lon, start, end, params, retries, timeout)
            else:
                data = _fetch_s3(lat, lon, start, end, params)
        except PowerUnavailable as exc:
            errors.append(f"{route}: {exc}")
            continue
        checkpoint = data.get("header", {}).pop("_checkpoint", None)
        atomic_write_text(_cache_path(lat, lon, start, end, params, cache_dir, route), json.dumps(data))
        if checkpoint:  # per-block S3 checkpoints are only removed once the result is saved
            shutil.rmtree(checkpoint, ignore_errors=True)
        return data
    raise PowerUnavailable("; ".join(errors))


# --- Route 1: REST API --------------------------------------------------------

def _fetch_api(lat, lon, start, end, params, retries, timeout) -> dict:
    query = {
        "parameters": ",".join(params),
        "community": config.POWER_COMMUNITY,
        "latitude": lat,
        "longitude": lon,
        "start": start,
        "end": end,
        "format": "JSON",
    }
    last_exc: Exception | None = None
    for attempt in range(retries):
        try:
            resp = httpx.get(config.POWER_DAILY_URL, params=query, timeout=timeout,
                             headers={"User-Agent": "farm-twin-prototype"})
        except httpx.HTTPError as exc:  # network-level failure: retry with backoff
            last_exc = exc
            time.sleep(2 ** attempt)
            continue
        if resp.status_code >= 500 or resp.status_code == 429:
            last_exc = PowerError(f"HTTP {resp.status_code}")
            time.sleep(2 ** attempt)
            continue
        if resp.status_code != 200:
            raise PowerError(f"NASA POWER HTTP {resp.status_code}: {_messages(resp)}")
        data = resp.json()
        if "properties" not in data or "parameter" not in data["properties"]:
            raise PowerError(f"Unexpected NASA POWER response: {str(data)[:300]}")
        data.setdefault("header", {})["access"] = "NASA POWER REST API"
        return data
    raise PowerUnavailable(f"NASA POWER API unreachable after {retries} attempts: {last_exc}")


def _messages(resp: httpx.Response) -> str:
    try:
        body = resp.json()
        return "; ".join(map(str, body.get("messages") or body.get("errors") or [body]))
    except ValueError:
        return resp.text[:300]


# --- Route 2: AWS Open Data Zarr -----------------------------------------------

def _fetch_s3(lat, lon, start, end, params) -> dict:
    """Read the nearest grid cell from POWER's daily MERRA-2 Zarr store on S3."""
    from .sources.zarr_point import ZarrReadError, read_point

    try:
        pt = read_point(config.POWER_S3_DAILY_ZARR, params, lat, lon, start, end)
    except ImportError as exc:  # pragma: no cover - dependency hint
        raise PowerUnavailable("S3 route needs xarray, zarr<3, fsspec and aiohttp") from exc
    except ZarrReadError as exc:
        raise PowerUnavailable(f"NASA POWER S3 archive unreachable: {exc}") from exc

    available = list(pt.df.columns)
    keys = pt.df.index.strftime("%Y%m%d")
    parameter = {}
    for p in available:
        parameter[p] = {k: (FILL if not math.isfinite(v) else round(float(v), 2))
                        for k, v in zip(keys, pt.df[p].to_numpy())}
    missing = [p for p in params if p not in available]
    return {
        "type": "Feature",
        "geometry": {"type": "Point", "coordinates": [pt.cell_lon, pt.cell_lat, None]},
        "properties": {"parameter": parameter},
        "header": {
            "title": "NASA/POWER MERRA-2 daily (LST), AWS Open Data Zarr",
            "access": "NASA POWER AWS Open Data (s3://nasa-power)",
            "store": config.POWER_S3_DAILY_ZARR,
            "fill_value": FILL,
            "start": keys[0] if len(keys) else start,
            "end": keys[-1] if len(keys) else end,
            "_checkpoint": str(pt.checkpoint) if pt.checkpoint else None,  # removed before caching
        },
        "parameters": {p: {"units": pt.units.get(p, ""),
                           "longname": pt.attrs.get(p, {}).get("long_name", "")} for p in available},
        "messages": [f"Not in the MERRA-2 daily store, left out: {', '.join(missing)}"] if missing else [],
    }


# --- Parsing --------------------------------------------------------------------

def to_dataframe(raw: dict) -> pd.DataFrame:
    """Daily DataFrame indexed by date. POWER fill values become NaN, never zeros."""
    series = raw["properties"]["parameter"]
    df = pd.DataFrame(series)
    df.index = pd.to_datetime(df.index, format="%Y%m%d")
    df.index.name = "date"
    fill = raw.get("header", {}).get("fill_value", FILL)
    df = df.astype(float).replace(float(fill), np.nan)
    # Some responses use -999 even when header says otherwise; treat any -999 as missing.
    return df.mask(df <= FILL).sort_index()


def units(raw: dict) -> dict[str, str]:
    """Units per parameter, as reported by the source."""
    meta = raw.get("parameters", {})
    return {k: v.get("units", "") for k, v in meta.items()}


def source_info(raw: dict) -> dict:
    """Provenance to store alongside every derived result."""
    header = raw.get("header", {})
    coords = raw.get("geometry", {}).get("coordinates", [None, None, None])
    return {
        "dataset": "NASA POWER (daily, point)",
        "access": header.get("access", "NASA POWER REST API"),
        "community": config.POWER_COMMUNITY,
        "title": header.get("title"),
        "sources": header.get("sources"),
        "start": header.get("start"),
        "end": header.get("end"),
        "longitude": coords[0],
        "latitude": coords[1],
        "elevation_m": coords[2] if len(coords) > 2 else None,
        "messages": raw.get("messages", []),
        "spatial_note": "POWER values are grid-cell estimates (MERRA-2 cell is 0.5 x 0.625 deg), "
                        "not plot-level measurements.",
    }

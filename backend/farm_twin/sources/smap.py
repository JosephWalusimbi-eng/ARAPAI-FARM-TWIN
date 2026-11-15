"""SMAP L3 enhanced radiometer soil moisture (SPL3SMP_E v006, 9 km EASE-Grid 2.0, daily).

How it works, without downloading whole global files:
  1. Compute the farm's row/column on the 9 km EASE-Grid 2.0 global grid (1624 x 3856).
  2. List the daily granules for the period from NASA CMR (no login needed).
  3. For each day, ask NASA's cloud OPeNDAP server for just that one pixel (needs Earthdata Login).
  4. Keep AM and PM retrievals flagged "recommended quality"; daily value = their mean.

Raw per-day results are cached per pixel and fetched incrementally, so an interrupted
run resumes where it stopped. Retrievals exist only on days the satellite passed over
the pixel (roughly every 2-3 days at the equator); other days stay missing, not zero.
"""
from __future__ import annotations

import json
import math
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import quote

import httpx
import numpy as np
import pandas as pd

from .. import config
from .base import NotFetched, PointSeries, Source, SourceError, atomic_write_text, read_json_safe
from .earthdata import EarthdataSession, credentials

SAVE_EVERY_DAYS = 20
SAVE_EVERY_SECONDS = 15.0

# --- EASE-Grid 2.0 global, 9 km (EPSG:6933) ------------------------------------------
_A = 6378137.0                    # WGS84 semi-major axis (m)
_E = 0.0818191908426215           # WGS84 eccentricity
_PHI1 = math.radians(30.0)        # standard parallel
_K0 = math.cos(_PHI1) / math.sqrt(1 - _E**2 * math.sin(_PHI1) ** 2)
CELL_M = 9008.055210146
N_ROWS, N_COLS = 1624, 3856
X_MIN = -CELL_M * N_COLS / 2      # -17367530.445...
Y_MAX = CELL_M * N_ROWS / 2       #   7314540.830...

AM = "Soil_Moisture_Retrieval_Data_AM"
PM = "Soil_Moisture_Retrieval_Data_PM"
VARIABLES = {  # our name -> path inside the SMAP HDF5 file (names verified in CMR UMM-Var)
    "am": f"{AM}/soil_moisture",
    "am_flag": f"{AM}/retrieval_qual_flag",
    "lat": f"{AM}/latitude",
    "lon": f"{AM}/longitude",
    "pm": f"{PM}/soil_moisture_pm",
    "pm_flag": f"{PM}/retrieval_qual_flag_pm",
    "lat_pm": f"{PM}/latitude_pm",
    "lon_pm": f"{PM}/longitude_pm",
}
SM_VALID = (0.02, 0.5)  # cm3/cm3, valid range from the product metadata


def _q(phi: float) -> float:
    s = math.sin(phi)
    return (1 - _E**2) * (s / (1 - _E**2 * s * s)
                          - (1 / (2 * _E)) * math.log((1 - _E * s) / (1 + _E * s)))


def ease2_9km_rowcol(lat: float, lon: float) -> tuple[int, int]:
    """Row/column of the 9 km EASE-Grid 2.0 global cell containing (lat, lon)."""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise ValueError("lat/lon out of range")
    x = _A * _K0 * math.radians(lon)
    y = _A * _q(math.radians(lat)) / (2 * _K0)
    col = int((x - X_MIN) // CELL_M)
    row = int((Y_MAX - y) // CELL_M)
    return min(max(row, 0), N_ROWS - 1), min(max(col, 0), N_COLS - 1)


# --- CMR granule listing ------------------------------------------------------------
_DATE_RE = re.compile(r"_(\d{8})_")


NETWORK_RETRIES = 9  # waits 1, 2, 4, ... capped at 60 s: rides out ~5 min of lost internet


def _backoff(attempt: int) -> float:
    return min(60.0, 2.0 ** attempt)


def _cmr_page(client: httpx.Client, params: dict, headers: dict,
              retries: int = NETWORK_RETRIES) -> httpx.Response:
    last = None
    for attempt in range(retries):
        try:
            resp = client.get(config.CMR_GRANULES_URL, params=params, headers=headers)
        except httpx.HTTPError as exc:  # timeouts, resets, DNS failure while offline
            last = exc
            time.sleep(_backoff(attempt))
            continue
        if resp.status_code == 200:
            return resp
        last = SourceError(f"HTTP {resp.status_code} {resp.text[:200]}")
        if resp.status_code < 500 and resp.status_code != 429:
            break
        time.sleep(_backoff(attempt))
    raise SourceError(f"CMR granule search failed after {retries} tries: {last!r}")


def list_granules(start: str, end: str, *, client: httpx.Client | None = None,
                  cache_dir: Path | None = None) -> dict[str, str]:
    """{YYYYMMDD: OPeNDAP URL} for every SMAP granule in the period (latest version per day).

    With cache_dir, the listing is saved so a resumed run (e.g. after a power cut) skips it.
    """
    cache_file = None
    if cache_dir is not None:
        cache_file = cache_dir / f"smap_granules_{config.SMAP_SHORT_NAME}_{config.SMAP_VERSION}_{start}_{end}.json"
        cached = read_json_safe(cache_file)
        if cached:
            return cached
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=180),
                                    headers={"User-Agent": "farm-twin-prototype"})
    t0 = f"{start[:4]}-{start[4:6]}-{start[6:]}T00:00:00Z"
    t1 = f"{end[:4]}-{end[4:6]}-{end[6:]}T23:59:59Z"
    params = {"short_name": config.SMAP_SHORT_NAME, "version": config.SMAP_VERSION,
              "temporal": f"{t0},{t1}", "page_size": 500}
    found: dict[str, tuple[str, str]] = {}
    search_after = None
    try:
        while True:
            headers = {"CMR-Search-After": search_after} if search_after else {}
            resp = _cmr_page(client, params, headers)
            items = resp.json().get("items", [])
            for item in items:
                umm = item["umm"]
                ur = umm["GranuleUR"]
                m = _DATE_RE.search(ur)
                url = next((u["URL"] for u in umm.get("RelatedUrls", [])
                            if u.get("Subtype") == "OPENDAP DATA"), None)
                if not m or not url:
                    continue
                day = m.group(1)
                if day not in found or ur > found[day][0]:  # newer processing wins
                    found[day] = (ur, url)
            search_after = resp.headers.get("CMR-Search-After")
            if not items or not search_after:
                break
    finally:
        if own:
            client.close()
    result = {d: v[1] for d, v in sorted(found.items())}
    if cache_file is not None and result:
        atomic_write_text(cache_file, json.dumps(result))
    return result


# --- OPeNDAP single-pixel read --------------------------------------------------------
def opendap_pixel_url(granule_url: str, row: int, col: int) -> str:
    ce = ";".join(f"/{path}[{row}][{col}]" for path in VARIABLES.values())
    return f"{granule_url}.dap.nc4?dap4.ce={quote(ce, safe='')}"


def _find_var(ds, path: str):
    node = ds
    parts = path.split("/")
    try:
        for g in parts[:-1]:
            node = node.groups[g]
        return node.variables[parts[-1]]
    except KeyError:
        flat = path.replace("/", "_")  # some servers flatten group names
        return ds.variables.get(flat)


def parse_pixel_nc4(content: bytes) -> dict:
    """Pull the single-pixel values out of a DAP4 netCDF-4 response."""
    import netCDF4
    out = {}
    with netCDF4.Dataset("smap_pixel.nc", mode="r", memory=content) as ds:
        for name, path in VARIABLES.items():
            var = _find_var(ds, path)
            if var is None:
                raise SourceError(f"OPeNDAP response lacks {path}; variables: "
                                  f"{list(ds.variables)} groups: {list(ds.groups)}")
            arr = np.ma.masked_invalid(np.ma.asarray(var[...], dtype=float)).ravel()
            v = arr[0] if arr.size else np.ma.masked
            out[name] = None if np.ma.is_masked(v) or v in (-9999.0, 65534.0) else float(v)
    return out


def daily_value(raw: dict, require_recommended: bool = True) -> tuple[float, int]:
    """Mean of good-quality AM/PM retrievals and how many went into it."""
    vals = []
    for key in ("am", "pm"):
        v, flag = raw.get(key), raw.get(f"{key}_flag")
        if v is None or not (SM_VALID[0] <= v <= SM_VALID[1]):
            continue
        if require_recommended and (flag is None or int(flag) & 1):
            continue  # bit 0 set = not recommended quality
        vals.append(v)
    return (float(np.mean(vals)) if vals else float("nan")), len(vals)


# --- Source -------------------------------------------------------------------------------
class SmapSource(Source):
    key = "smap"
    label = "SMAP L3 enhanced soil moisture (9 km, daily)"
    columns = ["SMAP_SM", "SMAP_SM_N"]
    needs_login = True
    default_start = config.SMAP_DEFAULT_START

    def configured(self) -> bool:
        return credentials() is not None

    def raw_cache_path(self, row: int, col: int, cache_dir: Path) -> Path:
        return cache_dir / f"smap_raw_{config.SMAP_SHORT_NAME}_{config.SMAP_VERSION}_r{row}_c{col}.json"

    def _load_raw(self, path: Path) -> dict:
        return read_json_safe(path) or {"days": {}}

    def _save_raw(self, path: Path, raw: dict) -> None:
        atomic_write_text(path, json.dumps(raw))

    def fetch(self, lat, lon, start, end, *, refresh=False, cache_only=False,
              cache_dir: Path = config.CACHE_DIR, log=None, session=None,
              cmr_client=None) -> PointSeries:
        start = max(start, config.SMAP_DEFAULT_START)
        row, col = ease2_9km_rowcol(lat, lon)
        path = self.raw_cache_path(row, col, cache_dir)
        raw = self._load_raw(path)
        raw.update({"row": row, "col": col})
        window = pd.date_range(pd.to_datetime(start), pd.to_datetime(end), freq="D")
        keys = [d.strftime("%Y%m%d") for d in window]

        if not cache_only:
            granules = list_granules(start, end, client=cmr_client, cache_dir=cache_dir)
            todo = [d for d in keys if d in granules and (refresh or d not in raw["days"])]
            for d in keys:  # no granule that day (instrument outage): record as known-missing
                if d not in granules and d not in raw["days"]:
                    raw["days"][d] = None
            if todo:
                own = session is None
                session = session or EarthdataSession()
                try:
                    self._fetch_days(session, granules, todo, row, col, raw, path, log)
                finally:
                    if own:
                        session.close()
            self._save_raw(path, raw)
        elif not any(d in raw["days"] for d in keys):
            raise NotFetched("SMAP not fetched yet; run scripts/fetch_sources.py smap")

        sm, n = [], []
        for d in keys:
            rec = raw["days"].get(d)
            v, k = daily_value(rec, config.SMAP_REQUIRE_RECOMMENDED) if rec else (float("nan"), 0)
            sm.append(v)
            n.append(float(k))
        df = pd.DataFrame({"SMAP_SM": sm, "SMAP_SM_N": n},
                          index=pd.DatetimeIndex(window, name="date"))
        fetched = sum(1 for d in keys if d in raw["days"])
        return PointSeries(self.key, df, {"SMAP_SM": "m3/m3", "SMAP_SM_N": "count"}, {
            "dataset": f"SMAP {config.SMAP_SHORT_NAME} v{config.SMAP_VERSION} (9 km EASE-Grid 2.0)",
            "access": "NASA Earthdata cloud OPeNDAP (single-pixel requests)",
            "ease2_row": row, "ease2_col": col,
            "pixel_latitude": raw.get("pixel_lat"), "pixel_longitude": raw.get("pixel_lon"),
            "days_in_window": len(keys), "days_checked": fetched,
            "days_with_retrieval": int(np.isfinite(df["SMAP_SM"]).sum()),
            "quality_filter": "recommended quality only" if config.SMAP_REQUIRE_RECOMMENDED else "none",
            "spatial_note": "SMAP measures the top ~5 cm of soil, averaged over a 9 km pixel.",
        })

    def _fetch_days(self, session, granules, todo, row, col, raw, path, log):
        def one(day):
            url = opendap_pixel_url(granules[day], row, col)
            last = None
            server_errors = 0
            for attempt in range(NETWORK_RETRIES):
                try:
                    resp = session.get(url)
                except httpx.HTTPError as exc:  # offline / timeout: wait for the network
                    last = exc
                    time.sleep(_backoff(attempt))
                    continue
                if resp.status_code == 200:
                    return day, parse_pixel_nc4(resp.content)
                last = SourceError(f"HTTP {resp.status_code}: {resp.text[:200]}")
                server_errors += 1
                if resp.status_code < 500 or server_errors >= 3:
                    break
                time.sleep(_backoff(attempt))
            raise SourceError(f"{day}: {last}")

        done = failed = 0
        errors = []
        last_save = time.monotonic()
        pool = ThreadPoolExecutor(max_workers=config.SMAP_WORKERS)
        try:
            futures = [pool.submit(one, d) for d in todo]
            for fut in as_completed(futures):
                try:
                    day, rec = fut.result()
                except SourceError as exc:
                    failed += 1
                    errors.append(str(exc))
                    if failed >= 10 and done == 0:  # systematic problem: stop early, explain
                        raise SourceError("SMAP requests keep failing. First errors: "
                                          + " | ".join(errors[:3])) from exc
                    continue
                self._check_geolocation(rec, raw, row, col)
                raw["days"][day] = rec
                done += 1
                # Checkpoint often: a power cut loses at most ~SAVE_EVERY days or seconds.
                if done % SAVE_EVERY_DAYS == 0 or time.monotonic() - last_save > SAVE_EVERY_SECONDS:
                    self._save_raw(path, raw)
                    last_save = time.monotonic()
                if log and done % 100 == 0:
                    log(f"  SMAP: {done}/{len(todo)} days")
        finally:
            # Ctrl-C, an error or a normal finish: keep everything fetched so far.
            pool.shutdown(wait=False, cancel_futures=True)
            self._save_raw(path, raw)
        if log:
            log(f"  SMAP: {done} days fetched, {failed} failed (rerun to retry failures)")

    @staticmethod
    def _check_geolocation(rec, raw, row, col):
        # Coordinates are only filled where that pass had data; use AM, else PM.
        lat, lon = rec.get("lat"), rec.get("lon")
        if lat is None or lon is None or lat < -90:
            lat, lon = rec.get("lat_pm"), rec.get("lon_pm")
        if lat is None or lon is None or lat < -90:
            return
        if "pixel_lat" not in raw:
            r2, c2 = ease2_9km_rowcol(lat, lon)
            if (r2, c2) != (row, col):
                raise SourceError(f"EASE-Grid indexing mismatch: pixel ({row},{col}) reports "
                                  f"centre {lat},{lon} which maps to ({r2},{c2})")
            raw["pixel_lat"], raw["pixel_lon"] = lat, lon  # store only once verified

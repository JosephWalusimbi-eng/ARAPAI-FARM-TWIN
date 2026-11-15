"""Fetch and cache data sources for a farm location.

    python scripts/fetch_sources.py --list                 # what is registered / configured
    python scripts/fetch_sources.py imerg                  # GPM IMERG rainfall, 2001-2025
    python scripts/fetch_sources.py smap --check           # Earthdata login + one SMAP day
    python scripts/fetch_sources.py smap                   # SMAP soil moisture, 2015-04 to 2025
    python scripts/fetch_sources.py smap --start 20230101  # shorter window (faster)
    python scripts/fetch_sources.py power imerg smap       # all of them

SMAP needs Earthdata Login: put EARTHDATA_TOKEN=... (or EARTHDATA_USERNAME / EARTHDATA_PASSWORD)
in a .env file in the project root. SMAP runs are resumable: rerun to continue or retry.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import climate, config  # noqa: E402
from farm_twin.sources import SOURCES, SourceError, farm_table  # noqa: E402
from farm_twin.sources import earthdata, smap  # noqa: E402


def log(msg: str) -> None:
    print(msg, flush=True)


def check_smap(lat: float, lon: float) -> int:
    row, col = smap.ease2_9km_rowcol(lat, lon)
    log(f"Earthdata login: {earthdata.describe()}")
    log(f"SMAP 9 km EASE-Grid 2.0 pixel for ({lat}, {lon}): row {row}, col {col}")
    granules = smap.list_granules("20240401", "20240410")
    log(f"CMR: {len(granules)} SMAP granules found for 2024-04-01..10")
    if not granules:
        return 1
    day, url = next(iter(granules.items()))
    sess = earthdata.EarthdataSession()
    try:
        resp = sess.get(smap.opendap_pixel_url(url, row, col))
    finally:
        sess.close()
    log(f"OPeNDAP {day}: HTTP {resp.status_code}, {len(resp.content)} bytes")
    if resp.status_code != 200:
        log(resp.text[:500])
        return 1
    rec = smap.parse_pixel_nc4(resp.content)
    log(f"Pixel values: {rec}")
    log(f"Daily value (quality-filtered mean, retrievals): {smap.daily_value(rec)}")
    if rec.get("lat") is not None and rec["lat"] > -90:
        log(f"Pixel centre reported by file: {rec['lat']:.4f}, {rec['lon']:.4f} "
            f"(farm at {lat}, {lon}) -> maps back to {smap.ease2_9km_rowcol(rec['lat'], rec['lon'])}")
    log("SMAP access OK.")
    return 0


def main() -> int:
    loc = config.PILOT_FARM["location"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("sources", nargs="*", help=f"any of {list(SOURCES)}")
    ap.add_argument("--lat", type=float, default=loc["latitude"])
    ap.add_argument("--lon", type=float, default=loc["longitude"])
    ap.add_argument("--start", default=config.DEFAULT_START)
    ap.add_argument("--end", default=config.DEFAULT_END)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--list", action="store_true", help="show registered sources and status")
    ap.add_argument("--check", action="store_true", help="SMAP only: test login with one day")
    args = ap.parse_args()

    if args.list or not args.sources:
        log(f"Earthdata login: {earthdata.describe()}")
        for s in SOURCES.values():
            st = s.status()
            need = "needs Earthdata login" if st["needs_login"] else "no login"
            log(f"  {st['key']:6} {st['label']}  [{need}; configured={st['configured']}]")
        return 0
    if args.check:
        return check_smap(args.lat, args.lon)

    rc = 0
    for key in args.sources:
        if key not in SOURCES:
            log(f"Unknown source {key!r}; known: {list(SOURCES)}")
            return 2
        log(f"== {key}: {SOURCES[key].label}")
        try:
            series = SOURCES[key].fetch(args.lat, args.lon, args.start, args.end,
                                        refresh=args.refresh, log=log)
        except SourceError as exc:
            log(f"FAILED: {exc}")
            rc = 1
            continue
        df = series.df
        first, last = df.dropna(how="all").index.min(), df.dropna(how="all").index.max()
        log(f"  {len(df)} days in window, data from {first:%Y-%m-%d} to {last:%Y-%m-%d}")
        for c in df.columns:
            log(f"  {c}: {df[c].notna().sum()} valid days, mean {df[c].mean():.3f} {series.units.get(c, '')}")
        for k in ("latitude", "longitude", "pixel_latitude", "pixel_longitude", "days_with_retrieval"):
            if series.provenance.get(k) is not None:
                log(f"  {k}: {series.provenance[k]}")

    # Show what the combined profile gains from the extra sources.
    df, meta = farm_table(args.lat, args.lon, args.start, args.end,
                          cache_only=tuple(SOURCES), include=tuple(SOURCES))
    prof = climate.climate_profile(df) if "PRECTOTCORR" in df else {}
    for key, cc in prof.get("rain_crosscheck", {}).items():
        log(f"Rain cross-check season {key}: {cc}")
    for key, sm in prof.get("soil_moisture", {}).items():
        log(f"SMAP season {key}: {sm['summary']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

"""Fetch NASA POWER daily data for a point and print a climate summary.

    python scripts/fetch_power.py                       # Soroti pilot farm, 2001-2025
    python scripts/fetch_power.py --lat 0.35 --lon 32.58 --start 20150101 --end 20241231
    python scripts/fetch_power.py --refresh --csv data/soroti_daily.csv
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import climate, config, power  # noqa: E402


def main() -> int:
    loc = config.PILOT_FARM["location"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=loc["latitude"])
    ap.add_argument("--lon", type=float, default=loc["longitude"])
    ap.add_argument("--start", default=config.DEFAULT_START)
    ap.add_argument("--end", default=config.DEFAULT_END)
    ap.add_argument("--refresh", action="store_true", help="ignore the cache and re-download")
    ap.add_argument("--source", choices=["auto", "api", "s3"], default=None,
                    help="auto = API then AWS S3 archive (default); s3 skips the API")
    ap.add_argument("--csv", type=Path, help="also write the daily table to this CSV")
    args = ap.parse_args()

    print(f"Fetching NASA POWER daily data for ({args.lat}, {args.lon}) {args.start}-{args.end} ...",
          flush=True)
    try:
        raw = power.fetch_daily(args.lat, args.lon, args.start, args.end,
                                source=args.source, refresh=args.refresh)
    except power.PowerError as exc:
        print(f"FAILED: {exc}", file=sys.stderr, flush=True)
        return 1

    df = power.to_dataframe(raw)
    u = power.units(raw)
    src = power.source_info(raw)
    prof = climate.climate_profile(df)

    print(f"Source: {src['access']}  grid cell ({src['latitude']}, {src['longitude']})")
    for msg in src["messages"]:
        print(f"Note: {msg}")
    print(f"Got {len(df)} days, {df.index.min():%Y-%m-%d} to {df.index.max():%Y-%m-%d}")
    print("Units:", ", ".join(f"{k}={v}" for k, v in u.items()))
    print("Completeness:", ", ".join(f"{k}={v:.1%}" for k, v in prof["completeness"].items()))
    print(f"Mean annual rainfall: {prof['annual']['mean_rain_mm']} mm   Mean T2M: {prof['annual']['mean_t2m_c']} C")
    for key, s in prof["seasons"].items():
        sm = s["summary"]
        print(f"Season {key} ({s['label']}, months {s['months']}): "
              f"mean {sm.get('mean_rain_mm')} mm, CV {sm.get('cv')}, "
              f"dry-year (P20) {sm.get('p20_rain_mm')} mm, "
              f"longest dry spell avg {sm.get('mean_longest_dry_spell_days')} d, "
              f"years used {sm['years_used']}")
    if args.csv:
        args.csv.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(args.csv)
        print(f"Wrote {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

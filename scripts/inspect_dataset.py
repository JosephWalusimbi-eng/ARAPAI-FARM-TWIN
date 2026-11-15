"""Triage a dataset in one command: what is inside, and what is its value at the farm?

    python scripts/inspect_dataset.py <path-or-url>
    python scripts/inspect_dataset.py data.nc --var NDVI --lat 1.71 --lon 33.61 --csv out.csv

Formats: NetCDF/HDF5, Zarr, GeoTIFF/COG, CSV. URLs are downloaded once to data/incoming/
(Earthdata hosts use the login from .env). Prints the structure, the nearest grid cell to the
farm and its distance, a preview of the values, and the line that plugs it into the app.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config  # noqa: E402
from farm_twin.sources import generic  # noqa: E402


def main(argv=None) -> int:
    loc = config.PILOT_FARM["location"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("src")
    ap.add_argument("--var", action="append", help="variable(s) to read (default: all numeric)")
    ap.add_argument("--lat", type=float, default=loc["latitude"])
    ap.add_argument("--lon", type=float, default=loc["longitude"])
    ap.add_argument("--csv", help="also save the farm's values to this CSV")
    args = ap.parse_args(argv)
    try:
        print("== Structure")
        print(json.dumps(generic.describe(args.src), indent=1, default=str)[:4000])
        df, info = generic.read_point(args.src, args.lat, args.lon, args.var)
    except generic.DatasetError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"\n== At the farm ({args.lat}, {args.lon})")
    print(json.dumps(info, indent=1, default=str))
    print(df.head(10).to_string())
    if len(df) > 10:
        print(f"... {len(df)} rows" + (f", {df.index.min().date()} to {df.index.max().date()}" if info.get("time") else ""))
        print(df.describe().loc[["count", "mean", "min", "max"]].to_string())
    if args.csv:
        df.to_csv(args.csv)
        print(f"Saved {args.csv}")
    if info.get("time"):
        vars_ = args.var or list(df.columns)
        print("\n== To add it to the app (sources/__init__.py):")
        print(f'  SOURCES["new"] = GenericSource("new", "<label>", {args.src!r}, variables={vars_!r}, prefix="NEW")')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

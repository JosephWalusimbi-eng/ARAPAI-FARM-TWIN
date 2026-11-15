"""Fetch SoilGrids soil estimates for the pilot farm (or a given point) and cache them.

    python scripts/fetch_soil.py
    python scripts/fetch_soil.py --lat 1.71 --lon 33.61 --refresh

SoilGrids is slow (~30 s) and rate-limited, so the app reads only the cached result.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, soil  # noqa: E402


def main(argv=None) -> int:
    loc = config.PILOT_FARM["location"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=loc["latitude"])
    ap.add_argument("--lon", type=float, default=loc["longitude"])
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    try:
        s = soil.fetch(args.lat, args.lon, refresh=args.refresh)
    except soil.SoilError as exc:
        print(f"FAILED: {exc}")
        return 1
    print(f"{s['source']}, {s['resolution']}, {s['depth']} ({s['kind']})")
    print(f"  texture class: {s['texture_class']}")
    for name, p in s["properties"].items():
        print(f"  {name:<15} {p['value']} {p['unit']}  (5-95%: {p['low']} to {p['high']})")
    print(f"Cached: {soil.cache_path(args.lat, args.lon)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

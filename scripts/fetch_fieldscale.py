"""Fetch Landsat field-scale NDVI (140 m square) for the pilot farm and cache it. Resumable.

    python scripts/fetch_fieldscale.py
    python scripts/fetch_fieldscale.py --years 2013 2025
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, fieldscale  # noqa: E402


def main(argv=None) -> int:
    loc = config.PILOT_FARM["location"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=loc["latitude"])
    ap.add_argument("--lon", type=float, default=loc["longitude"])
    ap.add_argument("--years", type=int, nargs=2, default=list(fieldscale.DEFAULT_YEARS))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    store = fieldscale.fetch(args.lat, args.lon, tuple(args.years), refresh=args.refresh)
    print(json.dumps(fieldscale.summarize(store), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

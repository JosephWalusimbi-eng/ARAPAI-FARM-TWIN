"""Fetch MODIS NDVI for the pilot farm (or a point) and cache it. Resumable: rerun after a cut.

    python scripts/fetch_vegetation.py
    python scripts/fetch_vegetation.py --years 2015 2025 --refresh
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, vegetation  # noqa: E402


def main(argv=None) -> int:
    loc = config.PILOT_FARM["location"]
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lat", type=float, default=loc["latitude"])
    ap.add_argument("--lon", type=float, default=loc["longitude"])
    ap.add_argument("--years", type=int, nargs=2, default=list(vegetation.DEFAULT_YEARS))
    ap.add_argument("--refresh", action="store_true")
    args = ap.parse_args(argv)
    store = vegetation.fetch(args.lat, args.lon, tuple(args.years), refresh=args.refresh)
    print(json.dumps(vegetation.summarize(store), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

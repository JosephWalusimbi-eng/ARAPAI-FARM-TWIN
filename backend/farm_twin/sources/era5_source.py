"""ERA5 daily reference evapotranspiration (ET0) as a Source, read from the cache.

Cache-only by design: the app never calls the internet while answering a request. Fetch once with
`python scripts/fetch_sources.py era5` (or rely on the committed data snapshot).
ET0 is FAO-56 Penman-Monteith from ERA5 fields, computed by Open-Meteo (see farm_twin/reanalysis.py).
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from .. import config, reanalysis
from .base import NotFetched, PointSeries, Source, SourceError


class Era5Source(Source):
    key = "era5"
    label = "ERA5 reference evapotranspiration (ET0, 0.25 deg)"
    columns = ["ERA5_ET0"]

    def fetch(self, lat, lon, start, end, *, refresh=False, cache_only=False,
              cache_dir: Path = config.CACHE_DIR, log=None) -> PointSeries:
        era = None if refresh else reanalysis.load(lat, lon, cache_dir)
        if era is None:
            if cache_only:
                raise NotFetched("ERA5 not fetched yet for this location")
            try:
                era = reanalysis.fetch(lat, lon, refresh=refresh, cache_dir=cache_dir)
            except reanalysis.ReanalysisError as exc:
                raise SourceError(str(exc)) from exc
        idx = pd.DatetimeIndex(pd.to_datetime(era["time"]), name="date")
        df = pd.DataFrame({"ERA5_ET0": era["et0_mm"]}, index=idx, dtype=float).loc[start:end]
        return PointSeries(self.key, df, {"ERA5_ET0": "mm/day"}, {
            "dataset": era["dataset"], "access": "Open-Meteo archive API (ERA5, no login)",
            "latitude": era.get("grid_latitude"), "longitude": era.get("grid_longitude"),
            "spatial_note": "ERA5 is a 0.25 deg (~28 km) grid; ET0 is FAO-56 Penman-Monteith computed by Open-Meteo.",
        })

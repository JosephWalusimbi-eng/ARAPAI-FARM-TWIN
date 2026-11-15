"""GPM IMERG daily precipitation, read from NASA POWER's AWS Open Data archive.

No Earthdata login needed. 0.1 deg grid (~11 km), so it is a finer, satellite-based
cross-check on the MERRA-2 rainfall that POWER reports.
"""
from __future__ import annotations

from pathlib import Path

from .. import config
from .base import NotFetched, PointSeries, Source, SourceError
from .zarr_point import ZarrReadError, read_point


class ImergSource(Source):
    key = "imerg"
    label = "GPM IMERG daily precipitation (0.1 deg)"
    columns = list(config.IMERG_PARAMETERS)

    def fetch(self, lat, lon, start, end, *, refresh=False, cache_only=False,
              cache_dir: Path = config.CACHE_DIR, log=None) -> PointSeries:
        if not refresh:
            cached = self.load_cached(lat, lon, start, end, cache_dir)
            if cached is not None:
                return cached
        if cache_only:
            raise NotFetched("IMERG not fetched yet for this location/period")
        try:
            pt = read_point(config.IMERG_S3_DAILY_ZARR, self.columns, lat, lon, start, end, log=log)
        except ZarrReadError as exc:
            raise SourceError(f"IMERG archive read failed: {exc}") from exc
        series = PointSeries(self.key, pt.df, pt.units, {
            "dataset": "GPM IMERG daily (via NASA POWER)",
            "access": "NASA POWER AWS Open Data (s3://nasa-power)",
            "store": config.IMERG_S3_DAILY_ZARR,
            "latitude": pt.cell_lat, "longitude": pt.cell_lon,
            "status": {v: a.get("status") for v, a in pt.attrs.items()},
            "spatial_note": "IMERG values are 0.1 deg (~11 km) grid-cell estimates.",
        })
        self.save(series, lat, lon, start, end, cache_dir)
        pt.discard_checkpoint()  # only after the final result is on disk
        return series

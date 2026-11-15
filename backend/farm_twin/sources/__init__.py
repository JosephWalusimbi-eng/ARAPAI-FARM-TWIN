"""Data source registry and the merged daily farm table.

To add a dataset (e.g. a new NASA resource on challenge day):
  1. Write a Source subclass in this package (see imerg.py for a public Zarr store,
     smap.py for an Earthdata/OPeNDAP product). fetch() returns a PointSeries whose
     columns are prefixed with the dataset name (IMERG_..., SMAP_...).
  2. Add an instance to SOURCES below.
  3. Use the new columns in climate.py / the engine. Nothing else changes.
"""
from __future__ import annotations

from .. import config
from .base import NotConfigured, NotFetched, PointSeries, Source, SourceError
from .era5_source import Era5Source
from .imerg import ImergSource
from .power_source import PowerSource
from .smap import SmapSource

SOURCES: dict[str, Source] = {s.key: s for s in (PowerSource(), ImergSource(), SmapSource(), Era5Source())}

__all__ = ["SOURCES", "farm_table", "PointSeries", "Source", "SourceError",
           "NotConfigured", "NotFetched"]


def farm_table(lat: float, lon: float, start: str = config.DEFAULT_START,
               end: str = config.DEFAULT_END, include=("power", "imerg", "smap"), *,
               cache_only: tuple[str, ...] = (), refresh: bool = False, log=None):
    """Merge daily series from several sources on date.

    Returns (df, meta). meta[key] = {"status": ok|not_configured|not_fetched|error, ...}.
    POWER is required; any other source failing is reported in meta, not raised.
    """
    import pandas as pd

    frames, meta = [], {}
    for key in include:
        src = SOURCES[key]
        try:
            series = src.fetch(lat, lon, start, end, refresh=refresh,
                               cache_only=key in cache_only, log=log)
        except NotConfigured as exc:
            meta[key] = {"status": "not_configured", "detail": str(exc)}
        except NotFetched as exc:
            meta[key] = {"status": "not_fetched", "detail": str(exc)}
        except SourceError as exc:
            if key == "power":
                raise
            meta[key] = {"status": "error", "detail": str(exc)}
        else:
            frames.append(series.df)
            meta[key] = {"status": "ok", "units": series.units, "provenance": series.provenance,
                         "columns": list(series.df.columns)}
        meta[key]["label"] = src.label
    df = pd.concat(frames, axis=1).sort_index() if frames else pd.DataFrame()
    return df, meta

"""Common contract for every data source.

A source turns (lat, lon, start, end) into a PointSeries: a daily table for that
location plus units and provenance. Adding a new NASA dataset = writing one class
with `fetch()` and registering it in sources/__init__.py.
"""
from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from .. import config
from ..storage import atomic_write_text, read_json_safe  # noqa: F401 (re-exported)


class SourceError(RuntimeError):
    """The source could not deliver data (network, auth, format)."""


class NotConfigured(SourceError):
    """The source needs credentials or setup that is missing."""


class NotFetched(SourceError):
    """Cache-only read requested, but nothing has been fetched yet."""


@dataclass
class PointSeries:
    key: str                          # source key, e.g. "imerg"
    df: pd.DataFrame                  # daily, DatetimeIndex "date"; NaN = missing, never 0
    units: dict[str, str]
    provenance: dict = field(default_factory=dict)

    def to_json(self) -> dict:
        data = {c: {d.strftime("%Y%m%d"): (None if math.isnan(v) else round(float(v), 4))
                    for d, v in self.df[c].items()} for c in self.df.columns}
        return {"key": self.key, "units": self.units, "provenance": self.provenance, "data": data}

    @classmethod
    def from_json(cls, obj: dict) -> "PointSeries":
        df = pd.DataFrame(obj["data"], dtype=float)
        df.index = pd.DatetimeIndex(pd.to_datetime(df.index, format="%Y%m%d"), name="date")
        return cls(obj["key"], df.sort_index(), obj["units"], obj["provenance"])


class Source:
    key: str = ""
    label: str = ""
    columns: list[str] = []
    needs_login: bool = False
    default_start: str = config.DEFAULT_START

    def configured(self) -> bool:
        return True

    def cache_path(self, lat: float, lon: float, start: str, end: str,
                   cache_dir: Path = config.CACHE_DIR) -> Path:
        return cache_dir / f"{self.key}_{lat:.4f}_{lon:.4f}_{start}_{end}.json"

    def load_cached(self, lat, lon, start, end, cache_dir: Path = config.CACHE_DIR):
        obj = read_json_safe(self.cache_path(lat, lon, start, end, cache_dir))
        return PointSeries.from_json(obj) if obj else None

    def save(self, series: PointSeries, lat, lon, start, end,
             cache_dir: Path = config.CACHE_DIR) -> None:
        atomic_write_text(self.cache_path(lat, lon, start, end, cache_dir),
                          json.dumps(series.to_json()))

    def fetch(self, lat: float, lon: float, start: str, end: str, *,
              refresh: bool = False, cache_only: bool = False,
              cache_dir: Path = config.CACHE_DIR, log=None) -> PointSeries:
        raise NotImplementedError

    def status(self) -> dict:
        return {"key": self.key, "label": self.label, "columns": self.columns,
                "needs_login": self.needs_login, "configured": self.configured()}

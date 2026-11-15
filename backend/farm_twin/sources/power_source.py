"""NASA POWER (MERRA-2 meteorology) as a Source. Wraps farm_twin.power."""
from __future__ import annotations

from pathlib import Path

from .. import config, power
from .base import NotFetched, PointSeries, Source, SourceError


class PowerSource(Source):
    key = "power"
    label = "NASA POWER daily meteorology (MERRA-2)"
    columns = list(config.POWER_PARAMETERS)

    def fetch(self, lat, lon, start, end, *, refresh=False, cache_only=False,
              cache_dir: Path = config.CACHE_DIR, log=None) -> PointSeries:
        if cache_only:
            # power.fetch_daily checks its own cache first; refuse network if nothing cached.
            for route in ("api", "s3"):
                p = power._cache_path(lat, lon, start, end, config.POWER_PARAMETERS, cache_dir, route)
                if p.exists() or p.with_name(p.name + ".bak").exists():
                    break
            else:
                raise NotFetched("NASA POWER not fetched yet for this location/period")
        try:
            raw = power.fetch_daily(lat, lon, start, end, refresh=refresh, cache_dir=cache_dir)
        except power.PowerError as exc:
            raise SourceError(str(exc)) from exc
        return PointSeries(self.key, power.to_dataframe(raw), power.units(raw),
                           power.source_info(raw))

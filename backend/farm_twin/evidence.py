"""Evidence layers shown beside the rotation ranking (NDVI greenness, terrain).

They are read from the cache only, never fetched here, and never change the scores
(engine spec v0.1.1). Missing layers are simply left out, so the app works without them.
"""
from __future__ import annotations

from . import config, fieldscale, reanalysis, terrain, vegetation

FOOTPRINT_NOTE = "Farm boundary assumed: a 140 m square (about 2 ha) centred on the farm point."


def build(lat: float, lon: float, seasons: dict = config.SEASONS) -> dict | None:
    out: dict = {"footprint_note": FOOTPRINT_NOTE}
    store = vegetation.load(lat, lon)
    if store and store.get("records"):
        try:
            out["vegetation"] = vegetation.summarize(store, seasons)
        except vegetation.VegetationError:
            pass
    fstore = fieldscale.load(lat, lon)
    if fstore and fstore.get("records"):
        try:
            out["field"] = fieldscale.summarize(fstore, seasons)
        except vegetation.VegetationError:
            pass
    era = reanalysis.load(lat, lon)
    if era:
        try:
            from .sources import farm_table
            df, _ = farm_table(lat, lon, config.DEFAULT_START, config.DEFAULT_END,
                               include=("power", "imerg"), cache_only=("power", "imerg"))
            out["rain_sources"] = reanalysis.summarize(era, df, seasons)
        except Exception:  # noqa: BLE001 - evidence is optional; never break the pack
            pass
    t = terrain.load(lat, lon)
    if t:
        out["terrain"] = t
    return out if len(out) > 1 else None

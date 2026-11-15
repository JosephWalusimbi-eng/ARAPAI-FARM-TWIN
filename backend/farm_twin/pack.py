"""Farm data pack: everything the app needs to work offline, in one small JSON download.

The phone downloads the pack once while connected. With it, the browser shows the climate
profile and runs the rotation engine (frontend/engine.js) with no network at all.
It holds processed summaries, not raw satellite data, so it stays small (tens of KB).
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json

from . import config, engine, i18n
from .crops import CROP_CATALOGUE_VERSION, CROP_PARAMS_STATUS, CROPS, DISEASE_PAIRS

PACK_VERSION = 1  # format of the pack itself
STALE_AFTER_DAYS = 30  # the app warns when a pack is older than this


def _season_summaries(seasons: dict) -> dict:
    return {k: {"label": v["label"], "months": v["months"], "summary": v["summary"]}
            for k, v in seasons.items()}


def _source_summary(meta: dict) -> dict:
    out = {}
    for key, m in meta.items():
        p = m.get("provenance") or {}
        out[key] = {"status": m["status"], "label": m.get("label", key),
                    "detail": m.get("detail"),
                    "provenance": {k: p[k] for k in ("dataset", "access", "latitude", "longitude",
                                                     "pixel_latitude", "pixel_longitude", "spatial_note")
                                   if k in p}}
    return out


def data_pack_version(climate: dict) -> str:
    """'<data end date>+<8-char content hash>': changes whenever the climate inputs change."""
    digest = hashlib.sha256(json.dumps(climate, sort_keys=True).encode("utf-8")).hexdigest()[:8]
    return f"{climate['period']['end']}+{digest}"


def climate_summary(profile: dict) -> dict:
    """The part of climate.climate_profile() the app and engine need."""
    climate = {
        "period": profile["period"],
        "annual": {"mean_rain_mm": profile["annual"]["mean_rain_mm"],
                   "mean_t2m_c": profile["annual"]["mean_t2m_c"],
                   "per_year": profile["annual"]["per_year"]},
        "monthly_climatology": profile["monthly_climatology"],
        "seasons": _season_summaries(profile["seasons"]),
    }
    if "rain_crosscheck" in profile:
        climate["rain_crosscheck"] = profile["rain_crosscheck"]
    if "soil_moisture" in profile:
        climate["soil_moisture"] = {k: {"label": v["label"], "summary": v["summary"]}
                                    for k, v in profile["soil_moisture"].items()}
    return climate


def build_pack(profile: dict, sources_meta: dict, farm: dict = config.PILOT_FARM,
               generated_at: dt.datetime | None = None, evidence: dict | None = None) -> dict:
    """profile: climate.climate_profile(); sources_meta: farm_table()'s meta."""
    generated_at = generated_at or dt.datetime.now(dt.timezone.utc)
    climate = climate_summary(profile)
    pack = {
        "pack_version": PACK_VERSION,
        "engine_spec": "v0.1.1",
        "versions": {"engine": engine.ENGINE_VERSION, "rules": engine.RULES_VERSION,
                     "crop_catalogue": CROP_CATALOGUE_VERSION,
                     "data_pack": data_pack_version(climate), "pack_format": PACK_VERSION},
        "generated_at": generated_at.isoformat(timespec="seconds"),
        "data_through": profile["period"]["end"],
        "stale_after_days": STALE_AFTER_DAYS,
        "farm": farm,
        "climate": climate,
        "sources": _source_summary(sources_meta),
        "engine": {
            "params": engine.EngineParams().__dict__,
            "seasons": config.SEASONS,
            "crops": {k: c.to_dict() for k, c in CROPS.items()},
            "disease_pairs": [sorted(pair) + [code] for pair, code in DISEASE_PAIRS.items()],
            "alpha_by_farm_type": engine.ALPHA_BY_FARM_TYPE,
            "weight_keys": engine.WEIGHT_KEYS,
            "crop_params_status": CROP_PARAMS_STATUS,
        },
        "languages": i18n.languages(),
        "i18n": {code: i18n.catalog(code) for code in i18n.codes()},
    }
    if evidence:  # kept outside "climate" so it never changes the data-pack version
        pack["evidence"] = evidence
    return pack

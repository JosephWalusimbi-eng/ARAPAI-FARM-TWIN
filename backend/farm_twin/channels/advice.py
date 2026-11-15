"""Hosted engine results for USSD and SMS, cached so a menu answers within a second.

USSD sessions time out after a few seconds, so the ranking for each language is computed
once and reused for ADVICE_TTL seconds.
"""
from __future__ import annotations

import time

from .. import climate, engine, sms, soil
from ..sources import farm_table

ADVICE_TTL = 600
_CACHE: dict[str, tuple[float, tuple]] = {}


def clear() -> None:
    _CACHE.clear()


def advice(lang: str):
    """(engine result, climate profile, farm) for the pilot farm in a language."""
    hit = _CACHE.get(lang)
    if hit and time.time() - hit[0] < ADVICE_TTL:
        return hit[1]
    farm = soil.pilot_farm()
    loc = farm["location"]
    df, _ = farm_table(loc["latitude"], loc["longitude"], include=("power", "era5"), cache_only=("era5",))
    profile = climate.climate_profile(df)
    res = engine.rank_rotations(profile, farm, lang=lang)
    out = (res, profile, farm)
    _CACHE[lang] = (time.time(), out)
    return out


def plan_sms(lang: str) -> dict:
    """The top plan as SMS text and segments."""
    res, profile, farm = advice(lang)
    return sms.plan_sms(res.rows[0], res.meta, farm, lang, profile["period"]["end"])

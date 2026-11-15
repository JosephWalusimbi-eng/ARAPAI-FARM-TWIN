"""Soil properties for a farm from ISRIC SoilGrids 2.0 (250 m, modelled estimates).

SoilGrids values are MODELLED estimates, not measurements: a lab test on the farm is more
reliable. We keep the 5-95% range SoilGrids publishes so the uncertainty stays visible.
In engine spec v0.1.1 soil data raises the confidence level (q_soil = 0.5); it does not
change scores (a soil factor is planned for v0.2).

Fetch once (slow, ~30 s) with `python scripts/fetch_soil.py`; the result is cached in
data/cache/ and the app reads only the cache.
"""
from __future__ import annotations

import datetime as dt
import json
import time
from pathlib import Path

import httpx

from . import config
from .storage import atomic_write_text, read_json_safe

SOILGRIDS_URL = "https://rest.isric.org/soilgrids/v2.0/properties/query"
DEPTHS = {"0-5cm": 5, "5-15cm": 10, "15-30cm": 15}  # thickness weights for a 0-30 cm average
# SoilGrids property -> (our field, unit after dividing by d_factor)
PROPERTIES = {
    "phh2o": ("ph", "pH"),
    "soc": ("organic_carbon", "g/kg"),
    "nitrogen": ("nitrogen", "g/kg"),
    "clay": ("clay", "%"),
    "sand": ("sand", "%"),
    "silt": ("silt", "%"),
    "cec": ("cec", "cmol(c)/kg"),
    "bdod": ("bulk_density", "kg/dm³"),
}


class SoilError(Exception):
    pass


def cache_path(lat: float, lon: float, cache_dir: Path | None = None) -> Path:
    return (cache_dir or config.CACHE_DIR) / f"soilgrids_{lat:.4f}_{lon:.4f}.json"


def _weighted(depths: list[dict], d_factor: float, key: str) -> float | None:
    total = weight = 0.0
    for d in depths:
        v = (d.get("values") or {}).get(key)
        w = DEPTHS.get(d.get("label"))
        if v is None or w is None:
            continue
        total += (v / d_factor) * w
        weight += w
    return round(total / weight, 2) if weight else None


def texture_class(sand: float | None, silt: float | None, clay: float | None) -> str | None:
    """USDA soil texture class from sand/silt/clay percentages."""
    if None in (sand, silt, clay) or sand + silt + clay <= 0:
        return None
    s = sand + silt + clay
    sand, silt, clay = 100 * sand / s, 100 * silt / s, 100 * clay / s
    if silt + 1.5 * clay < 15:
        return "sand"
    if silt + 2 * clay < 30:
        return "loamy sand"
    if (7 <= clay < 20 and sand > 52) or (clay < 7 and silt < 50):
        return "sandy loam"
    if 7 <= clay < 27 and 28 <= silt < 50 and sand <= 52:
        return "loam"
    if silt >= 80 and clay < 12:
        return "silt"
    if (silt >= 50 and 12 <= clay < 27) or (50 <= silt < 80 and clay < 12):
        return "silt loam"
    if 20 <= clay < 35 and silt < 28 and sand > 45:
        return "sandy clay loam"
    if 27 <= clay < 40 and 20 < sand <= 45:
        return "clay loam"
    if 27 <= clay < 40 and sand <= 20:
        return "silty clay loam"
    if clay >= 35 and sand > 45:
        return "sandy clay"
    if clay >= 40 and silt >= 40:
        return "silty clay"
    if clay >= 40:
        return "clay"
    return "loam"


def parse(body: dict, lat: float, lon: float) -> dict:
    """SoilGrids JSON -> 0-30 cm depth-weighted values with their 5-95% range."""
    props = {}
    for layer in (body.get("properties") or {}).get("layers", []):
        name = layer.get("name")
        if name not in PROPERTIES:
            continue
        field, unit = PROPERTIES[name]
        d = (layer.get("unit_measure") or {}).get("d_factor") or 1
        depths = layer.get("depths") or []
        props[field] = {"value": _weighted(depths, d, "mean"), "low": _weighted(depths, d, "Q0.05"),
                        "high": _weighted(depths, d, "Q0.95"), "unit": unit}
    if not props or all(p["value"] is None for p in props.values()):
        raise SoilError("SoilGrids returned no values for this point (water, urban or outside coverage?)")
    val = lambda f: (props.get(f) or {}).get("value")  # noqa: E731
    return {
        "source": "SoilGrids 2.0 (ISRIC)",
        "resolution": "250 m",
        "depth": "0-30 cm, depth-weighted",
        "kind": "modelled estimate",
        "location": {"latitude": lat, "longitude": lon},
        "fetched_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "texture_class": texture_class(val("sand"), val("silt"), val("clay")),
        "properties": props,
    }


def fetch(lat: float, lon: float, *, cache_dir: Path | None = None, refresh: bool = False,
          retries: int = 3, timeout: float = 120.0, client: httpx.Client | None = None) -> dict:
    path = cache_path(lat, lon, cache_dir)
    if not refresh:
        cached = read_json_safe(path)
        if cached:
            return cached
    params = [("lon", lon), ("lat", lat)] + [("property", p) for p in PROPERTIES] + \
             [("depth", d) for d in DEPTHS] + [("value", v) for v in ("mean", "Q0.05", "Q0.95")]
    own = client is None
    client = client or httpx.Client(timeout=httpx.Timeout(30, read=timeout))
    try:
        last = None
        for attempt in range(retries):
            try:
                r = client.get(SOILGRIDS_URL, params=params)
                if r.status_code == 429:  # SoilGrids allows a few calls per minute
                    raise httpx.HTTPStatusError("rate limited", request=r.request, response=r)
                r.raise_for_status()
                soil = parse(r.json(), lat, lon)
                path.parent.mkdir(parents=True, exist_ok=True)
                atomic_write_text(path, json.dumps(soil, ensure_ascii=False, indent=1))
                return soil
            except (httpx.HTTPError, ValueError) as exc:
                last = exc
                if attempt < retries - 1:
                    time.sleep(min(60, 15 * (attempt + 1)))
        raise SoilError(f"SoilGrids unavailable after {retries} tries: {last!r}")
    finally:
        if own:
            client.close()


def load_cached(lat: float, lon: float, cache_dir: Path | None = None) -> dict | None:
    return read_json_safe(cache_path(lat, lon, cache_dir))


def farm_with_soil(farm: dict, soil: dict | None) -> dict:
    """A copy of the farm record with its soil profile filled from SoilGrids (if fetched).
    Measured values (data_source 'lab') are never replaced by estimates."""
    if not soil or (farm.get("soil_profile") or {}).get("data_source", "").lower() in {"lab", "lab_test"}:
        return farm
    p = soil["properties"]
    val = lambda f: (p.get(f) or {}).get("value")  # noqa: E731
    return {**farm, "soil_profile": {
        "soil_type": soil.get("texture_class"), "ph": val("ph"), "organic_carbon": val("organic_carbon"),
        "nitrogen": val("nitrogen"), "phosphorus": None, "potassium": None,
        "data_source": "SoilGrids", "details": soil}}


def pilot_farm() -> dict:
    """The pilot farm with cached SoilGrids data attached when available."""
    loc = config.PILOT_FARM["location"]
    return farm_with_soil(config.PILOT_FARM, load_cached(loc["latitude"], loc["longitude"]))

"""FastAPI app. Run from the project root:

    python -m uvicorn farm_twin.api:app --app-dir backend --reload
"""
from __future__ import annotations

import contextlib
import mimetypes
import os
import re

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import climate, config, engine, evidence, i18n, pack, plans, sms, snapshot, soil
from .sources import SOURCES, SourceError, farm_table
from .sources import earthdata
from .channels.routes import router as channels_router

# Windows can map .js to text/plain via the registry; browsers then refuse ES modules.
mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("application/manifest+json", ".webmanifest")

@contextlib.asynccontextmanager
async def lifespan(app):
    # A fresh clone or cloud host has no downloads: restock the cache from the committed
    # snapshot (missing files only; local downloads always win). FARM_TWIN_SEED_SNAPSHOT=0 skips.
    if os.environ.get("FARM_TWIN_SEED_SNAPSHOT", "1") != "0":
        snapshot.seed()
    yield


app = FastAPI(title="ARAPAI FARM TWIN", version="0.2.0", lifespan=lifespan)

DATE_RE = re.compile(r"^\d{8}$")


app.include_router(channels_router)  # USSD / SMS / MoMo (Africa's Talking-style callbacks)


# HEAD as well as GET: uptime monitors and link checkers probe with HEAD, and a 405 looks like downtime.
@app.api_route("/api/health", methods=["GET", "HEAD"])
def health():
    return {"status": "ok"}


@app.get("/api/farms/pilot")
def pilot_farm():
    """The pilot farm, with cached SoilGrids estimates attached (python scripts/fetch_soil.py)."""
    return soil.pilot_farm()


@app.get("/api/languages")
def languages():
    """Available languages, their review status and how much farmer-facing text is translated."""
    return {"default": i18n.SOURCE, "languages": i18n.languages()}


@app.get("/api/i18n/{code}")
def translations(code: str):
    """All UI text for one language; untranslated keys fall back to English."""
    try:
        return i18n.catalog(code)
    except i18n.UnknownLanguage:
        raise HTTPException(404, f"unknown language {code!r}; see /api/languages") from None


@app.get("/api/sources")
def sources_status():
    """Which datasets are registered, which need a login, and whether one is configured."""
    return {"earthdata_login": earthdata.describe(),
            "sources": [s.status() for s in SOURCES.values()]}


@app.get("/api/climate")
def climate_endpoint(
    lat: float = Query(config.PILOT_FARM["location"]["latitude"], ge=-90, le=90),
    lon: float = Query(config.PILOT_FARM["location"]["longitude"], ge=-180, le=180),
    start: str = Query(config.DEFAULT_START, description="YYYYMMDD"),
    end: str = Query(config.DEFAULT_END, description="YYYYMMDD"),
    include: str = Query("power,imerg,smap", description="comma-separated source keys"),
    refresh: bool = False,
):
    if not (DATE_RE.match(start) and DATE_RE.match(end)) or start > end:
        raise HTTPException(422, "start/end must be YYYYMMDD with start <= end")
    keys = [k.strip() for k in include.split(",") if k.strip()]
    unknown = [k for k in keys if k not in SOURCES]
    if unknown:
        raise HTTPException(422, f"unknown sources: {unknown}; known: {list(SOURCES)}")
    if "power" not in keys:
        keys.insert(0, "power")
    try:
        # SMAP takes minutes to hours to fetch, so the web request only reads its cache;
        # fill it with: python scripts/fetch_sources.py smap
        df, meta = farm_table(lat, lon, start, end, include=tuple(keys),
                              cache_only=("smap",), refresh=refresh)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    power_meta = meta["power"]
    return {
        "source": power_meta["provenance"],
        "units": {k: v for m in meta.values() if m["status"] == "ok" for k, v in m["units"].items()},
        "sources": meta,
        "profile": climate.climate_profile(df),
    }


@app.get("/api/rotations")
def rotations_endpoint(
    start: str = Query(config.DEFAULT_START, description="YYYYMMDD, climate record start"),
    end: str = Query(config.DEFAULT_END, description="YYYYMMDD, climate record end"),
    top: int = Query(20, ge=1, le=400),
    climate_suitability: float | None = Query(None, ge=0),
    water_resilience: float | None = Query(None, ge=0),
    soil_health: float | None = Query(None, ge=0),
    rotation_compatibility: float | None = Query(None, ge=0),
    farmer_utility: float | None = Query(None, ge=0),
    alpha: float | None = Query(None, ge=0, le=1, description="food vs market weight"),
    residues_removed: bool | None = None,
    lang: str = Query(i18n.SOURCE, description="language code, see /api/languages"),
):
    """Rank every feasible 3-season rotation for the pilot farm (engine spec v0.1).

    Weight overrides replace the farm's farmer_preferences and are normalised to sum to 1.
    """
    if not (DATE_RE.match(start) and DATE_RE.match(end)) or start > end:
        raise HTTPException(422, "start/end must be YYYYMMDD with start <= end")
    if lang not in i18n.codes():
        raise HTTPException(422, f"unknown language {lang!r}; known: {i18n.codes()}")
    farm = soil.pilot_farm()
    loc = farm["location"]
    try:
        df, meta = farm_table(loc["latitude"], loc["longitude"], start, end, include=("power", "era5"),
                              cache_only=("era5",))
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    overrides = {k: v for k, v in {
        "climate_suitability": climate_suitability, "water_resilience": water_resilience,
        "soil_health": soil_health, "rotation_compatibility": rotation_compatibility,
        "farmer_utility": farmer_utility}.items() if v is not None}
    try:
        profile = climate.climate_profile(df)
        out = engine.recommend(profile, farm, top=top, weights=overrides,
                               alpha=alpha, residues_removed=residues_removed, lang=lang,
                               data_pack_version=pack.data_pack_version(pack.climate_summary(profile)))
    except engine.EngineError as exc:
        raise HTTPException(422, str(exc)) from exc
    out["meta"]["climate_source"] = meta["power"]["provenance"]
    return out


@app.get("/api/pack")
def farm_pack(refresh: bool = False):
    """Everything the app needs offline for the pilot farm: climate summaries, the engine's
    inputs and every language. The phone stores it and runs the engine locally."""
    farm = soil.pilot_farm()
    loc = farm["location"]
    try:
        df, meta = farm_table(loc["latitude"], loc["longitude"], config.DEFAULT_START,
                              config.DEFAULT_END, include=("power", "imerg", "smap", "era5"),
                              cache_only=("smap", "era5"), refresh=refresh)
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    return pack.build_pack(climate.climate_profile(df), meta, farm,
                           evidence=evidence.build(loc["latitude"], loc["longitude"]))


def farm_with_settings(farm: dict, previous_crop: str | None, farm_type: str | None) -> dict:
    """Apply the app's device settings (same rule as effectiveFarm() in frontend/app.js)."""
    from .crops import CROPS
    farm = {**farm, "farming_history": dict(farm.get("farming_history") or {}), "farm": dict(farm["farm"])}
    if previous_crop:
        if previous_crop not in CROPS:
            raise HTTPException(422, f"unknown crop {previous_crop!r}")
        hist = list(farm["farming_history"].get("previous_crops") or [])
        if hist:
            hist[-1] = previous_crop
        else:
            hist.append(previous_crop)
        farm["farming_history"]["previous_crops"] = hist
    if farm_type:
        if farm_type not in engine.ALPHA_BY_FARM_TYPE:
            raise HTTPException(422, f"unknown farm type {farm_type!r}")
        farm["farm"]["farm_type"] = farm_type
    return farm


@app.get("/api/sms")
def sms_plan(lang: str = Query(i18n.SOURCE), rotation: str | None = Query(
        None, description="comma-separated crop keys, e.g. cowpea,finger_millet,cowpea; default: top"),
        previous_crop: str | None = None, farm_type: str | None = None,
        residues_removed: bool | None = None,
        climate_suitability: float | None = Query(None, ge=0), water_resilience: float | None = Query(None, ge=0),
        soil_health: float | None = Query(None, ge=0), rotation_compatibility: float | None = Query(None, ge=0),
        farmer_utility: float | None = Query(None, ge=0)):
    """A rotation plan as SMS text and segments (the basic-phone channel, hosted engine).
    The optional settings mirror the app's device settings, so the SMS matches the app."""
    if lang not in i18n.codes():
        raise HTTPException(422, f"unknown language {lang!r}; known: {i18n.codes()}")
    farm = farm_with_settings(soil.pilot_farm(), previous_crop, farm_type)
    weights = {k: v for k, v in {
        "climate_suitability": climate_suitability, "water_resilience": water_resilience,
        "soil_health": soil_health, "rotation_compatibility": rotation_compatibility,
        "farmer_utility": farmer_utility}.items() if v is not None}
    loc = farm["location"]
    try:
        df, _ = farm_table(loc["latitude"], loc["longitude"], include=("power", "era5"), cache_only=("era5",))
    except SourceError as exc:
        raise HTTPException(502, str(exc)) from exc
    profile = climate.climate_profile(df)
    try:
        res = engine.rank_rotations(profile, farm, lang=lang, weights=weights,
                                    residues_removed=residues_removed)
    except engine.EngineError as exc:
        raise HTTPException(422, str(exc)) from exc
    if rotation:
        wanted = [c.strip() for c in rotation.split(",")]
        row = next((r for r in res.rows if r["rotation"] == wanted), None)
        if row is None:
            raise HTTPException(422, "that rotation is unknown or breaks a hard rule")
    else:
        row = res.rows[0]
    return sms.plan_sms(row, res.meta, farm, lang, profile["period"]["end"])


def _known_farm(farm_id: str) -> None:
    if farm_id != config.PILOT_FARM["farm_id"]:
        raise HTTPException(404, f"unknown farm {farm_id!r}")


@app.get("/api/farms/{farm_id}/plans")
def list_plans(farm_id: str):
    _known_farm(farm_id)
    return {"plans": plans.load(farm_id)}


@app.post("/api/farms/{farm_id}/plans")
def sync_plans(farm_id: str, body: dict = Body(...)):
    """Upload plans saved on a device. Safe to retry: known plan ids are ignored."""
    _known_farm(farm_id)
    try:
        return plans.merge(farm_id, body.get("plans"))
    except plans.PlanError as exc:
        raise HTTPException(422, str(exc)) from exc


class RevalidatingStatic(StaticFiles):
    """Static files that browsers must revalidate (ETag, so unchanged files cost a 304), so a new
    deploy's engine.js/app.js reach farmers at once instead of an old cached copy."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


if config.FRONTEND_DIR.exists():
    app.mount("/static", RevalidatingStatic(directory=config.FRONTEND_DIR), name="static")

    NO_CACHE = {"Cache-Control": "no-cache"}

    @app.api_route("/", methods=["GET", "HEAD"])
    def index():
        return FileResponse(config.FRONTEND_DIR / "index.html", headers=NO_CACHE)

    @app.api_route("/phone", methods=["GET", "HEAD"])
    def phone_page():
        """Smartphone showcase: the live app in a phone frame, a QR code, install and offline-test steps."""
        return FileResponse(config.FRONTEND_DIR / "phone.html", headers=NO_CACHE)

    @app.get("/sw.js")
    def service_worker():
        # Served from the root so it can make the whole app work offline.
        return FileResponse(config.FRONTEND_DIR / "sw.js", media_type="application/javascript",
                            headers=NO_CACHE)

    @app.get("/manifest.webmanifest")
    def manifest():
        return FileResponse(config.FRONTEND_DIR / "manifest.webmanifest",
                            media_type="application/manifest+json")

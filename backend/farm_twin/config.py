"""Project-wide settings: paths, the pilot farm, NASA POWER parameters and season calendar."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "data" / "cache"
FRONTEND_DIR = ROOT / "frontend"
CHANNELS_DIR = ROOT / "data" / "channels"  # USSD/SMS/payment state (git-ignored)


def _load_dotenv(path: Path) -> None:
    """Load KEY=VALUE lines from a git-ignored .env file (never overrides real env vars)."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_dotenv(ROOT / ".env")

# --- NASA POWER --------------------------------------------------------------
POWER_DAILY_URL = "https://power.larc.nasa.gov/api/temporal/daily/point"
POWER_COMMUNITY = "AG"  # Agroclimatology community: solar radiation comes back in MJ/m^2/day
# Official POWER archive on AWS Open Data; fallback when the API host is blocked.
POWER_S3_DAILY_ZARR = ("https://nasa-power.s3.us-west-2.amazonaws.com/"
                       "merra2/temporal/power_merra2_daily_temporal_lst.zarr")
# "auto" = try the API, fall back to S3. Set FARM_TWIN_POWER_SOURCE=s3 to skip the API.
POWER_SOURCE = os.environ.get("FARM_TWIN_POWER_SOURCE", "auto")

# Daily point parameters used by the climate profile. Units are read from the
# API response metadata rather than assumed here.
POWER_PARAMETERS = [
    "T2M",                # mean air temperature at 2 m
    "T2M_MAX",            # daily max air temperature at 2 m
    "T2M_MIN",            # daily min air temperature at 2 m
    "PRECTOTCORR",        # bias-corrected precipitation
    "RH2M",               # relative humidity at 2 m
    "ALLSKY_SFC_SW_DWN",  # all-sky surface shortwave radiation
    "GWETROOT",           # root-zone soil wetness (0-1, model-derived)
]

# Default analysis window: complete calendar years only.
DEFAULT_START = "20010101"
DEFAULT_END = "20251231"

# --- GPM IMERG (via the POWER AWS archive, no login) ---------------------------
IMERG_S3_DAILY_ZARR = ("https://nasa-power.s3.us-west-2.amazonaws.com/"
                       "imerg/temporal/power_imerg_daily_temporal_utc.zarr")
IMERG_PARAMETERS = ["IMERG_PRECTOT"]  # total precipitation, mm/day, 0.1 deg grid

# --- NASA Earthdata (SMAP, and most other NASA archives) -------------------------
# Credentials come from the environment or a git-ignored .env file:
#   EARTHDATA_TOKEN=...                      (preferred; generate at urs.earthdata.nasa.gov)
#   or EARTHDATA_USERNAME=... + EARTHDATA_PASSWORD=...
#   or a ~/.netrc / ~/_netrc entry for urs.earthdata.nasa.gov
EARTHDATA_URS_HOST = "urs.earthdata.nasa.gov"
CMR_GRANULES_URL = "https://cmr.earthdata.nasa.gov/search/granules.umm_json"

# --- SMAP L3 enhanced radiometer soil moisture (SPL3SMP_E, 9 km daily) ----------
SMAP_SHORT_NAME = "SPL3SMP_E"
SMAP_VERSION = "006"
SMAP_DEFAULT_START = "20150401"  # SMAP science data begins 2015-03-31
SMAP_WORKERS = 6                 # parallel OPeNDAP requests (one per day)
SMAP_REQUIRE_RECOMMENDED = True  # keep only retrievals flagged "recommended quality"

# --- Pilot farm (Teso, Uganda) -----------------------------------------------
# Schema follows the FARM TWIN blueprint. Soil values stay None until real data
# (SoilGrids/iSDA or a lab test) is attached -- never fill them with guesses.
PILOT_FARM = {
    "farm_id": "FT-UG-0001",
    "name": "Soroti pilot farm",
    "location": {"country": "Uganda", "region": "Teso", "district": "Soroti",
                 "latitude": 1.71, "longitude": 33.61},
    "farm": {"area_hectares": 2.0, "farm_type": "smallholder",
             "irrigation": False, "boundary": None},
    "soil_profile": {"soil_type": None, "ph": None, "organic_carbon": None,
                     "nitrogen": None, "phosphorus": None, "potassium": None,
                     "data_source": "pending"},
    "farming_history": {"previous_crops": ["maize"], "previous_seasons": []},
    # Five weights, one per score term in S(R); they must sum to 1.
    # Defaults from engine spec v0.1.1 (rotation raised 0.10 -> 0.20 so legume-heavy
    # sequences cannot win on nitrogen alone); the farmer can change them in the app.
    "farmer_preferences": {"climate_suitability": 0.10, "water_resilience": 0.25,
                           "soil_health": 0.25, "rotation_compatibility": 0.20,
                           "farmer_utility": 0.20},
}

# --- Growing seasons ---------------------------------------------------------
# Teso is bimodal. These month windows are a working ASSUMPTION for the
# prototype -- confirm against a local crop calendar (e.g. MAAIF / FAO GIEWS).
SEASONS = {
    "A": {"label": "First rains", "months": [3, 4, 5, 6]},
    "B": {"label": "Second rains", "months": [8, 9, 10, 11]},
}

DRY_DAY_MM = 1.0  # a day with < 1 mm rain counts as dry when measuring dry spells

# --- USSD / SMS / payments (basic-phone channel) ------------------------------
# The simulator and the simulated providers are on by default; real sandboxes are opt-in.
SIMULATOR_ENABLED = os.environ.get("FARM_TWIN_SIMULATOR", "1") != "0"
SMS_GATEWAY = os.environ.get("SMS_GATEWAY", "simulated")   # simulated | africastalking
MOMO_MODE = os.environ.get("MOMO_MODE", "simulated")       # simulated | sandbox
SMS_SENDER = os.environ.get("SMS_SENDER", "ARAPAI")
# USSD code chosen by the team (12 Nov 2026). In production it is a channel on a licensed
# aggregator's shared code (UCC rules); the final assignment depends on availability.
USSD_CODE = os.environ.get("USSD_CODE", "*384*2026#")
# Price of the paid "3-season plan by SMS" service: UGX 500, agreed 12 Nov 2026.
PLAN_PRICE_AMOUNT = os.environ.get("PLAN_PRICE_AMOUNT", "500")
PLAN_PRICE_CURRENCY = os.environ.get("PLAN_PRICE_CURRENCY", "UGX")

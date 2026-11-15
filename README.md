# ARAPAI FARM TWIN

NASA-Powered Farm Rotation Digital Twin. NASA Space Apps 2026 prototype, piloted on a 2 ha rain-fed farm near Soroti (Teso, Uganda).

It ranks every three-season crop rotation for the farm from NASA climate data, explains the
result in English, Luganda, Kiswahili and Ateso. After one online download of the farm's NASA data it keeps working without internet.

## Run

**Live app:** https://farm-twin-5n9n.onrender.com (open it a minute early: the free host sleeps when idle). The repo includes a data snapshot, so a fresh clone
runs immediately with no NASA downloads. Docker: `docker build -t farm-twin .` and
`docker run -p 8000:8000 farm-twin`. Hosting with HTTPS: `render.yaml` (Render, free).

**Challenge-day data:** `python scripts/inspect_dataset.py <file-or-url>` (NetCDF/HDF5, Zarr,
GeoTIFF/COG, CSV) shows what is inside and its value at the farm.

```bash
pip install -r requirements.txt

# 1. Fetch data (cached in data/cache/; reruns are instant)
python scripts/fetch_sources.py --list                # datasets and login status
python scripts/fetch_sources.py power imerg           # no login needed
python scripts/fetch_sources.py smap --check          # after setting up .env (below)
python scripts/fetch_sources.py smap                  # resumable; rerun to continue
python scripts/fetch_soil.py                          # SoilGrids soil estimates (~30 s)

# 2. Start the app at http://127.0.0.1:8000
python -m uvicorn farm_twin.api:app --app-dir backend --reload

# 3. Tests (offline, synthetic fixtures)
python -m pytest -q
```

## Datasets

| Key | Dataset | Resolution | Access | Login |
|---|---|---|---|---|
| `power` | NASA POWER daily meteorology (MERRA-2) | 0.5° × 0.625° | POWER API, falling back to the AWS archive | No |
| `imerg` | GPM IMERG daily precipitation | 0.1° | NASA POWER AWS archive (Zarr) | No |
| `smap` | SMAP SPL3SMP_E v006 surface soil moisture | 9 km | Earthdata cloud OPeNDAP, one pixel per day | **Yes** |

### Earthdata login (SMAP and most other NASA archives)

1. Create an account at https://urs.earthdata.nasa.gov.
2. Generate a token there (Profile → Generate Token), or use your username and password.
3. Copy `.env.example` to `.env` (git-ignored) and fill it in.
4. Run `python scripts/fetch_sources.py smap --check`. It lists the granules, fetches one day's pixel and verifies the pixel location.

A full SMAP history (2015–2025) is about 3,800 small requests. With 6 run in parallel that should take minutes, not hours, depending on the network. Days without a satellite overpass stay missing.

### After a power cut or crash

Run the same command again. Everything already downloaded is kept:

```bash
python scripts/fetch_sources.py power imerg smap
```

- **SMAP** saves its progress every 20 days or 15 seconds.
- **IMERG and the POWER AWS fallback** save each finished block, so a rerun downloads only the missing blocks.
- **Saves can't be corrupted by a cut:** every save is written to a temp file, flushed to disk, then swapped in, and the previous version is kept as `.bak`. If a cut leaves a damaged file, the `.bak` is used.

### Adding a new NASA dataset (for example, challenge-day resources)

1. Write a `Source` subclass in `backend/farm_twin/sources/`. Use `imerg.py` as the template for a public Zarr or HTTP store, and `smap.py` for an Earthdata-protected product. `fetch()` returns a daily `PointSeries` whose columns are prefixed with the dataset name.
2. Register it in `SOURCES` in `sources/__init__.py`.
3. Use the new columns in `climate.py` or the engine. The API, cache and status panel pick it up automatically.

Responses are cached in `data/cache/`, so only the first fetch for a location and date range goes over the network. Use `--refresh` (CLI) or `?refresh=true` (API) to re-download.

### Two routes to the same NASA POWER data

1. **REST API** (`power.larc.nasa.gov`). Some networks reset connections to this host.
2. **AWS Open Data archive** (`s3://nasa-power`, read over public HTTPS). This holds the same MERRA-2 daily data as Zarr and needs no account. It's used automatically when the API is unreachable. It has no `ALLSKY_SFC_SW_DWN`; the response says so in `messages`.

`--source auto|api|s3` (CLI) or `FARM_TWIN_POWER_SOURCE=s3` (env) picks the route. Every result records which route it came from in `source.access`.

## Layout

```
backend/farm_twin/
  config.py      pilot farm, dataset settings, season calendar, .env loading
  power.py       NASA POWER client (API + AWS fallback)
  climate.py     climate profile: rain, seasons, dry spells, IMERG cross-check, SMAP stats
  crops.py       crop parameter table (all values ASSUMED, pending agronomist review)
  engine.py      rotation engine (spec v0.1): state update, C W H K F scores, scenarios,
                 hard constraints, ranking, explanations, confidence
  i18n.py        translations (locales/*.json), English fallback
  pack.py        farm pack for offline use; plans.py: saved plans synced from devices
  api.py         FastAPI: /api/health, /api/farms/pilot, /api/sources, /api/climate,
                 /api/rotations, /api/languages, /api/i18n/<code>
  sources/
    base.py        Source / PointSeries contract, caching
    zarr_point.py  chunked, retrying point reader for public Zarr stores
    power_source.py, imerg.py, smap.py
    earthdata.py   Earthdata Login (token, user/password, .netrc)
frontend/index.html   page layout; frontend/app.js: offline-first app (pack, charts, settings, plans)
frontend/engine.js    rotation engine for the browser (mirrors engine.py)
frontend/sw.js        service worker (offline app files, map tiles)
scripts/fetch_sources.py, scripts/fetch_power.py
tests/
```

## NASA POWER parameters

`T2M`, `T2M_MAX`, `T2M_MIN`, `PRECTOTCORR`, `RH2M`, `ALLSKY_SFC_SW_DWN`, `GWETROOT`. These are daily point requests from the AG community. Units are read from the API response, not hard-coded.

## Data rules

- Missing values stay missing. A year or season with less than 95% rain coverage is reported but left out of the statistics.
- POWER values are grid-cell estimates, not plot measurements. Every API response says so in `source.spatial_note`.
- Season windows (A: Mar–Jun, B: Aug–Nov) are a **working assumption**. Check them against a Teso crop calendar.
- Soil fields in the pilot farm stay `null` until real SoilGrids, iSDA or lab data is attached.

## Rotation engine

`engine.py` follows the Rotation Engine Spec v0.1 (Claude Doc). It scores every feasible
three-season rotation (357 sequences) under four scenarios (Baseline, Dry, Wet, Hot), which
is 1,428 evaluations, and ranks them by Baseline S. Climate inputs come from the NASA POWER
season statistics. Each rotation reports its five sub-scores, per-season r, g and f_T, its
worst scenario, its Dry rank, and its largest penalty in plain words.

`GET /api/rotations?top=20` returns the result. Optional query parameters: the five weights
(`climate_suitability`, `water_resilience`, `soil_health`, `rotation_compatibility`,
`farmer_utility`, normalised to sum to 1), `alpha` (food vs market) and `residues_removed`.
Maize → Maize → Maize is always included as the control.

All tunable constants are in `engine.EngineParams`; crop values are in `crops.py`.

## Basic phones: USSD, SMS and MTN MoMo

Open http://127.0.0.1:8000/simulator: a phone that dials the ARAPAI FARM TWIN USSD code, gets the
menus in the farmer's language, pays for the full plan with MTN MoMo and receives it by SMS.
The backend speaks the Africa's Talking callback format (`POST /ussd`, `POST /sms/incoming`).
SMS and MoMo are simulated by default; set `SMS_GATEWAY=africastalking` (+ `AT_USERNAME`,
`AT_API_KEY`) or `MOMO_MODE=sandbox` (+ `MOMO_SUBSCRIPTION_KEY`) in `.env` to use the real sandboxes.

## Offline-first

Download once, decide anywhere. The app needs a connection only to download the farm's NASA-based data (and to refresh it later); after that it ranks
the plans on the phone with no internet and shows how old its data is. Fresh NASA data and sending saved plans to the server need a connection.

- `GET /api/pack` returns the farm pack: climate summaries, the engine's inputs and all languages
  (about 40 KB, 10 KB compressed). The app keeps it on the device and shows its date.
- `frontend/engine.js` runs the rotation engine in the browser. It mirrors `engine.py` operation by
  operation; `tests/test_offline.py` checks both rank every rotation identically (needs Node).
- `frontend/sw.js` (service worker) keeps the app files, the map library and viewed map tiles.
- Farm settings (last season's crop, farm type, residues) and weights are saved on the device.
- Saved plans queue on the device and sync to `POST /api/farms/<id>/plans` when online; retries are
  safe (plan ids are made on the device, so a plan is stored once).

Demo: open the app once online, then turn off Wi-Fi (or stop the server) and reload: change the
weights, the language or last season's crop, and the ranking updates on the device.
Offline mode needs `localhost` or HTTPS (browsers only allow service workers there).

## Languages

The app shows farmer-facing text in English, Luganda, Kiswahili and Ateso (pick in the header).
NASA, dataset names, units and numbers are never translated; technical panels may stay in English.
Text lives in `backend/farm_twin/locales/<code>.json`; anything missing falls back to English.
Luganda, Kiswahili and Ateso are machine translations by Sunbird AI (Sunflower), marked "draft" in the app until native speakers review them.

```bash
python scripts/translations.py status          # coverage per language
python scripts/translations.py export teo      # spreadsheet for a translator (written to translations/, not kept in the repo)
python scripts/translations.py import teo      # bring it back (checks {placeholders})
python scripts/sunbird_translate.py --lang teo   # translate with Sunbird AI (teo, lg or sw; needs SUNBIRD_API_TOKEN), add --apply to import
```

`GET /api/languages`, `GET /api/i18n/<code>`, and `GET /api/rotations?lang=<code>`.

## Crop photos

The small crop pictures in `frontend/crops/` are cropped copies of openly licensed photographs (CC BY, CC BY-SA or CC0). Photographers and licences are listed in
[frontend/crops/CREDITS.md](frontend/crops/CREDITS.md) and in the app (Photo credits, at the bottom of the page).


## Next steps

1. Native-speaker review of the translations.
2. Attach soil data (SoilGrids or iSDA) to raise confidence and, in v0.2, feed a soil factor.
3. v0.2 water need from Kc × Hargreaves ET0; optional IMERG or SMAP inputs to the water score.

## AI assistance (NASA Space Apps disclosure)

The idea, challenge choice, pilot farm and product decisions are the team's. Claude (Anthropic), through Claude Code, was used as a
Super Intelligence (SI) tool to help write and test the software and the documentation. The Luganda, Kiswahili and Ateso text is machine-translated by Sunbird AI (Sunflower), our primary translator. Sunbird's own language tiers rate its understanding of Luganda and Kiswahili as "Good" and of Ateso as "Moderate", so native speakers on our team review every language, Ateso most carefully: Joshua Benjamin Ssentongo (Luganda), Juliana Akello (Kiswahili) and Geresem Ogwapit (Ateso).

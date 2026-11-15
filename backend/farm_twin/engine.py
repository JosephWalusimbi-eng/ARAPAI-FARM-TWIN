"""Rotation engine, implementing "FARM TWIN — Rotation Engine Spec v0.1.1".

Ranks every feasible three-season rotation for one farm by S(R) in [0, 1] under four
climate scenarios. Transparent and rule-based: no machine learning, no yield prediction.
Scores are relative: they compare rotations on the same farm.

Labels used in the output:
  Observed: season climate statistics from NASA POWER
  Derived:  everything computed here (r, g, f_T, sub-scores, S, ranks)
  Assumed:  crop parameters, utility values, effective-rain fraction, residue factor
"""
from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from . import config, i18n
from .crops import CROP_CATALOGUE_VERSION, CROP_PARAMS_STATUS, CROPS, DISEASE_PAIRS, Crop

# Versions travel with every result and saved plan.
# ENGINE_VERSION: the algorithm; must equal ENGINE_VERSION in frontend/engine.js.
# RULES_VERSION: EngineParams, hard constraints and scenario definitions (the spec version).
ENGINE_VERSION = "0.2.0"
RULES_VERSION = "0.2.0"

SCENARIOS = ("baseline", "dry", "wet", "hot")
TERMS = ("C", "W", "H", "K", "F")
# farmer_preferences key -> score term
WEIGHT_KEYS = {"climate_suitability": "C", "water_resilience": "W", "soil_health": "H",
               "rotation_compatibility": "K", "farmer_utility": "F"}
ALPHA_BY_FARM_TYPE = {"smallholder": 0.6, "commercial": 0.2}  # Assumed
N_SLOTS = 3


class EngineError(ValueError):
    """The inputs cannot support a ranking (e.g. no complete seasons of climate data)."""


@dataclass(frozen=True)
class EngineParams:
    """Every tunable constant from the spec, in one place."""
    effective_rain_fraction: float = 0.75   # Pe = 0.75 P (Assumed)
    wet_factor: float = 1.5                 # f_wet fires when P > wet_factor * WR
    wet_penalty: float = 0.7
    hot_delta_c: float = 2.0                # Hot scenario: mean Tmax + 2 °C
    n_scale_kg_ha: float = 120.0            # H maps N3 in [-60, +60] onto [0, 1]
    h_n_weight: float = 0.6
    h_residue_weight: float = 0.4
    repeat_penalty: float = 0.60           # v0.1.1: was 0.35
    same_family_penalty: float = 0.15
    streak_penalty: float = 0.30           # v0.1.1: was 0.10
    streak_threshold: int = 3
    disease_penalty: float = 0.20
    season_window_days: int = 122
    residual_moisture_days: int = 30
    min_baseline_r: float = 0.5
    dry_rank_drop_flag: int = 5
    residue_kept: float = 1.0               # m: 1.0 residues kept, 0.3 removed (Assumed)
    residue_removed: float = 0.3


@dataclass(frozen=True)
class SlotClimate:
    slot: int        # 1-based
    season: str      # "A" / "B"
    year: int
    P: float         # season rain, mm
    D: float         # longest dry spell, days
    T: float         # mean daily Tmax, °C
    et0: float | None = None   # mean daily reference evapotranspiration, mm/day (ERA5-based); None = v0.1.1 fixed need

    @property
    def label(self) -> str:
        return f"{self.season} {self.year}"


# --- inputs -------------------------------------------------------------------

def next_slots(today: dt.date | None = None, seasons: dict = config.SEASONS,
               n: int = N_SLOTS) -> list[tuple[str, int]]:
    """The next n season slots whose planting (first month) is after today."""
    today = today or dt.date.today()
    order = sorted(seasons, key=lambda k: seasons[k]["months"][0])
    out, year = [], today.year
    while len(out) < n:
        for key in order:
            start = dt.date(year, seasons[key]["months"][0], 1)
            if start > today and len(out) < n:
                out.append((key, year))
        year += 1
    return out


def kc_average(crop: Crop) -> float:
    """Mean Kc over the crop cycle (FAO-56): flat initial and mid stages, straight-line development
    and late stages. Plain left-to-right arithmetic so the browser port gives identical numbers."""
    total = crop.st_ini + crop.st_dev + crop.st_mid + crop.st_late
    return (crop.st_ini * crop.kc_ini + crop.st_dev * ((crop.kc_ini + crop.kc_mid) / 2)
            + crop.st_mid * crop.kc_mid + crop.st_late * ((crop.kc_mid + crop.kc_end) / 2)) / total


def water_need(crop: Crop, sc: SlotClimate, params: EngineParams) -> tuple[float, str]:
    """Crop water need for one season slot, mm, and where it came from.

    v0.2: need = ET0 (mm/day, season mean) x days in the field x average Kc. A crop that fills a
    whole slot (cassava) uses the full season window. Without ET0 (old data pack, no ERA5 cache)
    the fixed v0.1.1 value is used, so nothing breaks.
    """
    if sc.et0 is None or crop.kc_mid <= 0:
        return crop.water_need_mm, "assumed"
    days = crop.days if crop.slots == 1 else params.season_window_days
    return sc.et0 * days * kc_average(crop), "fao56"


def scenario_climates(season_stats: dict, slots: list[tuple[str, int]],
                      params: EngineParams = EngineParams()) -> dict[str, list[SlotClimate]]:
    """Per-scenario (P, D, T) for each slot, from climate.season_stats() summaries."""
    picks = {  # scenario -> (rain stat, dry-spell stat, temperature offset)
        "baseline": ("mean_rain_mm", "mean_longest_dry_spell_days", 0.0),
        "dry": ("p20_rain_mm", "p80_longest_dry_spell_days", 0.0),
        "wet": ("p80_rain_mm", "p20_longest_dry_spell_days", 0.0),
        "hot": ("mean_rain_mm", "mean_longest_dry_spell_days", params.hot_delta_c),
    }
    out = {}
    for sc, (p_key, d_key, dT) in picks.items():
        row = []
        for i, (season, year) in enumerate(slots, start=1):
            summ = season_stats.get(season, {}).get("summary", {})
            vals = (summ.get(p_key), summ.get(d_key), summ.get("mean_tmax_c"))
            if not summ.get("years_used") or any(v is None for v in vals):
                raise EngineError(f"season {season}: no complete years of climate data "
                                  f"(need {p_key}, {d_key}, mean_tmax_c)")
            et0 = summ.get("mean_et0_mm_day")
            row.append(SlotClimate(i, season, year, float(vals[0]), float(vals[1]),
                                   float(vals[2]) + dT, None if et0 is None else float(et0)))
        out[sc] = row
    return out


def farm_weights(farm: dict, overrides: dict | None = None) -> dict[str, float]:
    """Term weights {C, W, H, K, F}, normalised to sum to 1."""
    prefs = dict(farm.get("farmer_preferences") or {})
    prefs.update(overrides or {})
    w = {term: float(prefs.get(key, 0.0)) for key, term in WEIGHT_KEYS.items()}
    if any(v < 0 for v in w.values()) or sum(w.values()) <= 0:
        raise EngineError("weights must be non-negative and not all zero")
    total = sum(w.values())
    return {k: v / total for k, v in w.items()}


def history_state(farm: dict) -> tuple[str | None, int]:
    """(previous crop c0, same-family streak k0) from farming_history (last = most recent)."""
    hist = [c for c in (farm.get("farming_history") or {}).get("previous_crops") or []]
    if not hist:
        return None, 0
    c0 = hist[-1]
    fam = CROPS[c0].family if c0 in CROPS else None
    k = 0
    for c in reversed(hist):
        if fam is not None and c in CROPS and CROPS[c].family == fam:
            k += 1
        else:
            break
    return c0, max(k, 1)


# --- rotations and constraints ------------------------------------------------

def enumerate_rotations(crops: dict[str, Crop] = CROPS, n: int = N_SLOTS) -> list[tuple[str, ...]]:
    """All slot sequences: annual crops in any slot, two-slot crops as contiguous blocks."""
    annual = [k for k, c in crops.items() if c.slots == 1]
    multi = [k for k, c in crops.items() if c.slots > 1]
    out = list(itertools.product(annual, repeat=n))
    for m in multi:
        span = crops[m].slots
        for start in range(n - span + 1):
            rest = n - span
            for fill in itertools.product(annual, repeat=rest):
                seq = list(fill[:start]) + [m] * span + list(fill[start:])
                out.append(tuple(seq))
    return out


def _block_starts(rotation: tuple[str, ...]) -> list[bool]:
    """True where a crop starts (False for the 2nd slot of a two-slot crop)."""
    starts, i = [False] * len(rotation), 0
    while i < len(rotation):
        starts[i] = True
        i += CROPS[rotation[i]].slots
    return starts


def _msg(key: str, **params) -> dict:
    """A translatable message: rendered per language by render()."""
    return {"key": key, "params": params}


def render(lang: str, msg: dict, capitalize: bool = True) -> str:
    """Render a message in a language, localising crop, scenario, season and family codes."""
    p = dict(msg["params"])
    for name in ("crop", "prev"):
        if name in p:
            p[name] = i18n.t(lang, f"crop.{p[name]}").lower()
    if "scenario" in p:
        p["scenario"] = i18n.t(lang, f"scenario.{p['scenario']}")
    if "season" in p:
        code, year = p["season"]
        months = config.SEASONS[code]["months"]
        p["season"] = f"{i18n.t(lang, f'month.{months[0]}')}-{i18n.t(lang, f'month.{months[-1]}')} {year}"
    for name in ("family", "disease"):
        if name in p:
            p[name] = i18n.t(lang, f"{name}.{p[name]}")
    if "reasons" in p:
        p["reasons"] = i18n.t(lang, "reason.join").join(
            render(lang, r, capitalize=False) for r in p["reasons"])
    text = i18n.t(lang, msg["key"], **p)
    return text[:1].upper() + text[1:] if capitalize else text


def slot_feasibility(climates: list[SlotClimate], params: EngineParams,
                     crops: dict[str, Crop] = CROPS) -> dict[tuple[str, int], dict | None]:
    """Hard constraints under baseline climate. {(crop, slot): reason message or None}."""
    window = params.season_window_days + params.residual_moisture_days
    out = {}
    for c in crops.values():
        for sc in climates:
            reason = None
            if c.slots == 1 and c.days > window:
                reason = _msg("exclude.duration", crop=c.key, days=c.days, window=window)
            elif params.effective_rain_fraction * sc.P / water_need(c, sc, params)[0] < params.min_baseline_r:
                reason = _msg("exclude.water", crop=c.key, pct=round(100 * params.min_baseline_r),
                              season=[sc.season, sc.year])
            elif sc.T >= c.tmax_crit_c:
                reason = _msg("exclude.heat", crop=c.key, temp=f"{sc.T:.1f}",
                              crit=f"{c.tmax_crit_c:.0f}")
            out[(c.key, sc.slot)] = reason
    return out


# --- simulation and scoring ---------------------------------------------------

# Plain left-to-right arithmetic (no numpy reductions) so the browser port in
# frontend/engine.js reproduces every score bit for bit.
def _clip(x: float, lo: float, hi: float) -> float:
    return min(max(x, lo), hi)


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs)


def _f_T(T: float, crop: Crop) -> float:
    if T <= crop.tmax_opt_c:
        return 1.0
    if T >= crop.tmax_crit_c:
        return 0.0
    return (crop.tmax_crit_c - T) / (crop.tmax_crit_c - crop.tmax_opt_c)


def _transition_penalty(prev: str | None, crop: str, streak: int,
                        params: EngineParams) -> tuple[float, list[dict]]:
    p, why = 0.0, []
    if prev is None:
        return p, why
    a, b = CROPS.get(prev), CROPS[crop]
    if prev == crop:
        p += params.repeat_penalty
        why.append(_msg("reason.same_crop", crop=crop))
    elif a is not None and a.family == b.family:
        p += params.same_family_penalty
        why.append(_msg("reason.same_family", crop=crop, prev=prev, family=b.family))
    if streak >= params.streak_threshold:
        p += params.streak_penalty
        why.append(_msg("reason.streak", n=streak, family=b.family))
    disease = DISEASE_PAIRS.get(frozenset({prev, crop}))
    if disease and prev != crop and a is not None:
        p += params.disease_penalty
        why.append(_msg("reason.disease", crop=crop, prev=prev, disease=disease))
    return p, why


def simulate(rotation: tuple[str, ...], climates: list[SlotClimate], *, c0: str | None,
             k0: int, alpha: float, weights: dict[str, float],
             params: EngineParams = EngineParams()) -> dict:
    """Run the season update for slots 1..3 under one scenario and score the sequence."""
    m = params.residue_kept
    N = R = 0.0
    prev, k = c0, k0
    seasons, transitions = [], []
    for key, sc, starts in zip(rotation, climates, _block_starts(rotation)):
        crop = CROPS[key]
        wr, wr_src = water_need(crop, sc, params)
        r = min(1.0, params.effective_rain_fraction * sc.P / wr)
        d = crop.dry_spell_tol_days
        g = _clip(1 - max(0.0, sc.D - d) / d, 0, 1)
        fT = _f_T(sc.T, crop)
        fwet = (params.wet_penalty
                if crop.waterlog_sensitive and sc.P > params.wet_factor * wr
                else 1.0)
        C_t, W_t = fT * fwet, r * g
        F_t = r * (alpha * crop.food + (1 - alpha) * crop.market)
        N += r * crop.net_n_kg_ha
        R += r * crop.residue_index * m
        if starts:  # a new crop: transition c_{t-1} -> c_t
            prev_crop = CROPS.get(prev) if prev else None
            k = k + 1 if prev_crop is not None and prev_crop.family == crop.family else 1
            p, why = _transition_penalty(prev, key, k, params)
            transitions.append({"slot": sc.slot, "from": prev, "to": key,
                                "penalty": p, "reasons": why})
            prev = key
        seasons.append({"slot": sc.slot, "season": sc.label, "season_code": sc.season,
                        "year": sc.year, "crop": key,
                        "P_mm": sc.P, "D_days": sc.D, "T_c": sc.T, "WR_mm": wr, "WR_source": wr_src,
                        "r": r, "g": g, "f_T": fT, "f_wet": fwet,
                        "C": C_t, "W": W_t, "F": F_t})
    Cs = [s["C"] for s in seasons]
    Ws = [s["W"] for s in seasons]
    terms = {
        "C": 0.5 * _mean(Cs) + 0.5 * min(Cs),
        "W": 0.5 * _mean(Ws) + 0.5 * min(Ws),
        "H": (params.h_n_weight * _clip(0.5 + N / params.n_scale_kg_ha, 0, 1)
              + params.h_residue_weight * R / N_SLOTS),
        "K": _clip(1 - sum(t["penalty"] for t in transitions), 0, 1),
        "F": _mean([s["F"] for s in seasons]),
    }
    terms = {k_: float(v) for k_, v in terms.items()}
    S = sum(weights[t] * terms[t] for t in TERMS)
    return {"S": S, **terms, "N3": N, "R3": R, "seasons": seasons, "transitions": transitions}


# --- explanations -------------------------------------------------------------

def _issues(rotation: tuple[str, ...], runs: dict[str, dict], weights: dict[str, float],
            params: EngineParams) -> list[dict]:
    """Penalties as translatable messages with an approximate score cost (weight × shortfall)."""
    out = []
    for sc, run in runs.items():
        for s in run["seasons"]:
            c = CROPS[s["crop"]]
            where = {"scenario": sc, "crop": c.key, "slot": s["slot"],
                     "season": [s["season_code"], s["year"]]}
            if s["r"] < 1:
                out.append({"scenario": sc, "term": "W", "cost": weights["W"] * (1 - s["r"]) / N_SLOTS,
                            "msg": _msg("issue.water", pct=round(100 * s["r"]), **where)})
            if s["g"] < 1:
                out.append({"scenario": sc, "term": "W",
                            "cost": weights["W"] * s["r"] * (1 - s["g"]) / N_SLOTS,
                            "msg": _msg("issue.dry_spell", days=f"{s['D_days']:.0f}",
                                        tol=f"{c.dry_spell_tol_days:.0f}", **where)})
            if s["f_T"] < 1:
                out.append({"scenario": sc, "term": "C", "cost": weights["C"] * (1 - s["f_T"]) / N_SLOTS,
                            "msg": _msg("issue.heat", temp=f"{s['T_c']:.1f}",
                                        opt=f"{c.tmax_opt_c:.0f}", **where)})
            if s["f_wet"] < 1:
                out.append({"scenario": sc, "term": "C",
                            "cost": weights["C"] * (1 - s["f_wet"]) * s["f_T"] / N_SLOTS,
                            "msg": _msg("issue.waterlog", rain=f"{s['P_mm']:.0f}", **where)})
    base = runs["baseline"]
    for t in base["transitions"]:
        if t["penalty"] > 0:
            out.append({"scenario": "all", "term": "K", "cost": weights["K"] * t["penalty"],
                        "msg": _msg("issue.rotation", reasons=t["reasons"], slot=t["slot"])})
    if base["N3"] < 0:
        lost = min(0.5, -base["N3"] / params.n_scale_kg_ha)
        out.append({"scenario": "baseline", "term": "H",
                    "cost": weights["H"] * params.h_n_weight * lost,
                    "msg": _msg("issue.nitrogen", kg=f"{-base['N3']:.0f}")})
    out.sort(key=lambda i: -i["cost"])
    return out


def rotation_label(rotation: tuple[str, ...], lang: str = i18n.SOURCE) -> str:
    """'Maize → Beans → Groundnut' in the given language (the English label is the stable id)."""
    parts, starts = [], _block_starts(rotation)
    for key, s in zip(rotation, starts):
        if s:
            name = i18n.t(lang, f"crop.{key}")
            n = CROPS[key].slots
            parts.append(i18n.t(lang, "rot.multi_slot", crop=name, n=n) if n > 1 else name)
    return i18n.t(lang, "rot.arrow").join(parts)


# --- confidence ---------------------------------------------------------------

def confidence(annual_per_year: list[dict], farm: dict,
               crop_status: str = CROP_PARAMS_STATUS, lang: str = i18n.SOURCE) -> dict:
    """q = 0.4 q_clim + 0.3 q_soil + 0.3 q_crop, with the step that would raise it most."""
    years = len(annual_per_year)
    q_clim = (sum(1 for y in annual_per_year if y.get("rain_mm") is not None) / years) if years else 0.0
    src = ((farm.get("soil_profile") or {}).get("data_source") or "pending").lower()
    q_soil = 1.0 if src in {"lab", "lab_test"} else 0.5 if src in {"soilgrids", "isda"} else 0.0
    q_crop = 1.0 if crop_status == "reviewed" else 0.0
    q = 0.4 * q_clim + 0.3 * q_soil + 0.3 * q_crop
    level = "High" if q >= 0.75 else "Medium" if q >= 0.5 else "Low"
    steps = []
    if q_crop < 1:
        steps.append({"key": "confidence.step.agronomist", "gain": 0.3})
    if q_soil < 1:
        steps.append({"key": "confidence.step.lab" if q_soil else "confidence.step.soil_data",
                      "gain": 0.3 * (1 - q_soil)})
    if q_clim < 1:
        steps.append({"key": "confidence.step.climate", "gain": 0.4 * (1 - q_clim)})
    steps.sort(key=lambda s: -s["gain"])
    for st in steps:
        st["step"] = i18n.t(lang, st["key"])
    return {"q": round(q, 3), "level": level, "level_text": i18n.t(lang, f"confidence.{level}"), "q_clim": round(q_clim, 3), "q_soil": q_soil,
            "q_crop": q_crop, "raise_it": steps}


# --- top level ----------------------------------------------------------------

def _rounded(obj, nd=3):
    if isinstance(obj, float):
        return round(obj, nd)
    if isinstance(obj, dict):
        return {k: _rounded(v, nd) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_rounded(v, nd) for v in obj]
    return obj


@dataclass
class EngineResult:
    rows: list[dict] = field(default_factory=list)   # every feasible rotation, ranked
    meta: dict = field(default_factory=dict)


def rank_rotations(profile: dict, farm: dict = config.PILOT_FARM, *,
                   today: dt.date | None = None, weights: dict | None = None,
                   alpha: float | None = None, residues_removed: bool | None = None,
                   params: EngineParams | None = None, lang: str = i18n.SOURCE,
                   data_pack_version: str | None = None) -> EngineResult:
    """Score all feasible rotations under every scenario and rank by baseline S.

    profile: climate.climate_profile() output (needs "seasons" and "annual").
    weights: optional farmer_preferences overrides (same keys as the farm record).
    """
    params = params or EngineParams()
    if residues_removed is not None:
        m = params.residue_removed if residues_removed else params.residue_kept
        params = EngineParams(**{**params.__dict__, "residue_kept": m})
    w = farm_weights(farm, weights)
    if alpha is None:
        alpha = ALPHA_BY_FARM_TYPE.get((farm.get("farm") or {}).get("farm_type"), 0.6)
    if not 0 <= alpha <= 1:
        raise EngineError("alpha must be between 0 and 1")
    c0, k0 = history_state(farm)
    slots = next_slots(today)
    climates = scenario_climates(profile["seasons"], slots, params)
    feas = slot_feasibility(climates["baseline"], params)

    all_rotations = enumerate_rotations()
    feasible, excluded = [], {}
    for rot in all_rotations:
        reasons = [feas[(c, i + 1)] for i, c in enumerate(rot) if feas[(c, i + 1)]]
        if reasons:
            for r_ in reasons:
                text = render(lang, r_)
                entry = excluded.setdefault(text, {"reason": text, "code": r_["key"], "rotations": 0})
                entry["rotations"] += 1
        else:
            feasible.append(rot)
    if not feasible:
        raise EngineError("no rotation passes the hard constraints")

    rows = []
    for rot in feasible:
        runs = {sc: simulate(rot, climates[sc], c0=c0, k0=k0, alpha=alpha, weights=w,
                             params=params) for sc in SCENARIOS}
        rows.append({"rotation": list(rot), "label": rotation_label(rot),
                     "display": rotation_label(rot, lang), "runs": runs})

    for sc in SCENARIOS:  # rank 1 = best; ties broken by label for stable output
        for rank, row in enumerate(sorted(rows, key=lambda r_: (-r_["runs"][sc]["S"], r_["label"])), 1):
            row.setdefault("ranks", {})[sc] = rank
    rows.sort(key=lambda r_: r_["ranks"]["baseline"])

    for row in rows:
        runs = row.pop("runs")
        worst = min(SCENARIOS, key=lambda sc: runs[sc]["S"])
        issues = _issues(tuple(row["rotation"]), runs, w, params)
        row.update({
            "rank": row["ranks"]["baseline"],
            "scores": {sc: {k_: runs[sc][k_] for k_ in ("S",) + TERMS} for sc in SCENARIOS},
            "robustness": {"min_S": runs[worst]["S"], "scenario": worst},
            "dry_flag": row["ranks"]["dry"] - row["ranks"]["baseline"] > params.dry_rank_drop_flag,
            "state": {"N3_kg_ha": runs["baseline"]["N3"], "R3": runs["baseline"]["R3"]},
            "seasons": {sc: runs[sc]["seasons"] for sc in SCENARIOS},
            "transitions": runs["baseline"]["transitions"],
            "largest_penalty": render(lang, issues[0]["msg"]) if issues else None,
            "issues": [{"scenario": i["scenario"], "term": i["term"], "cost": i["cost"],
                        "code": i["msg"]["key"], "params": i["msg"]["params"],
                        "text": render(lang, i["msg"])} for i in issues[:3]],
        })

    meta = {
        "spec": "FARM TWIN Rotation Engine Spec v0.1.1",
        "versions": versions(data_pack_version),
        "farm_id": farm.get("farm_id"),
        "slots": [{"slot": i, "season": s, "year": y,
                   "months": config.SEASONS[s]["months"]} for i, (s, y) in enumerate(slots, 1)],
        "previous_crop": c0, "previous_crop_name": i18n.t(lang, f"crop.{c0}") if c0 else None,
        "previous_streak": k0,
        "weights": w, "alpha": alpha, "residue_factor_m": params.residue_kept,
        "scenarios": {sc: [{"slot": c.slot, "season": c.label, "P_mm": c.P, "D_days": c.D,
                            "T_c": c.T} for c in climates[sc]] for sc in SCENARIOS},
        "rotations_considered": len(all_rotations),
        "rotations_feasible": len(feasible),
        "evaluations": len(feasible) * len(SCENARIOS),
        "excluded": [excluded[k_] for k_ in sorted(excluded)],
        "confidence": confidence(profile.get("annual", {}).get("per_year", []), farm, lang=lang),
        "language": i18n.catalog(lang)["meta"],
        "labels": {
            "Observed": "season rain, dry spell and Tmax statistics from NASA POWER",
            "Derived": "r, g, f_T, f_wet, sub-scores C W H K F, S, ranks",
            "Assumed": "crop parameters, food/market values, Pe = 0.75 P, residue factor m",
        },
        "crop_params_status": CROP_PARAMS_STATUS,
        "params": params.__dict__,
    }
    return EngineResult(rows=_rounded(rows), meta=_rounded(meta))


def versions(data_pack: str | None = None) -> dict:
    return {"engine": ENGINE_VERSION, "rules": RULES_VERSION,
            "crop_catalogue": CROP_CATALOGUE_VERSION, "data_pack": data_pack}


def recommend(profile: dict, farm: dict = config.PILOT_FARM, *, top: int = 20,
              include_labels: tuple[str, ...] = ("Maize → Maize → Maize",), **kw) -> dict:
    """JSON-ready engine output: the top rotations plus any named control rotations."""
    res = rank_rotations(profile, farm, **kw)
    shown = res.rows[:top]
    controls = [r for r in res.rows if r["label"] in include_labels and r not in shown]
    return {"meta": res.meta, "rotations": shown, "controls": controls,
            "crops": {k: c.to_dict() for k, c in CROPS.items()}}

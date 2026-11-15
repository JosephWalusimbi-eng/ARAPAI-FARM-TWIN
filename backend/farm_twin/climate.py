"""Turn daily NASA POWER data into a farm climate profile.

Everything here is descriptive statistics over observed/reanalysis data. No values
are invented: incomplete years and seasons are excluded and reported as such.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from . import config

RAIN = "PRECTOTCORR"
IMERG_RAIN = "IMERG_PRECTOT"
SMAP_SM = "SMAP_SM"
PRECIP_COLUMNS = {RAIN, IMERG_RAIN}   # summed per period; everything else is averaged
COUNT_COLUMNS = {"SMAP_SM_N"}         # bookkeeping columns, not climate variables
COMPLETE_FRACTION = 0.95  # a year/season needs >= 95% non-missing rain days to count
SMAP_MIN_RETRIEVALS = 15  # a season needs >= 15 SMAP retrieval days to give a mean


def _round(x, nd=2):
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    return round(float(x), nd)


def _longest_dry_spell(rain: pd.Series, threshold: float) -> int:
    dry = (rain < threshold).astype(int).to_numpy()
    best = run = 0
    for d in dry:
        run = run + 1 if d else 0
        best = max(best, run)
    return best


def completeness(df: pd.DataFrame) -> dict[str, float]:
    """Share of non-missing days per column, over the span that column's source covers."""
    out = {}
    for c in df.columns:
        if c in COUNT_COLUMNS:
            continue
        s = df[c]
        valid = s.dropna()
        if valid.empty:
            out[c] = 0.0
            continue
        span = s.loc[valid.index.min():valid.index.max()]
        out[c] = _round(span.notna().mean(), 4)
    return out


def annual_rainfall(df: pd.DataFrame) -> list[dict]:
    out = []
    for year, g in df[RAIN].groupby(df.index.year):
        days_in_year = 366 if pd.Timestamp(year=year, month=12, day=31).dayofyear == 366 else 365
        frac = g.notna().sum() / days_in_year
        out.append({"year": int(year),
                     "rain_mm": _round(g.sum()) if frac >= COMPLETE_FRACTION else None,
                     "coverage": _round(frac, 3)})
    return out


def monthly_climatology(df: pd.DataFrame) -> list[dict]:
    """Mean monthly rain total and mean of the other variables, per calendar month."""
    cols = [c for c in df.columns if c not in COUNT_COLUMNS]
    monthly = df[cols].resample("MS").agg({c: ("sum" if c in PRECIP_COLUMNS else "mean") for c in cols})
    # A month's rain total is only valid if the month was mostly observed.
    for c in PRECIP_COLUMNS & set(cols):
        coverage = df[c].resample("MS").apply(lambda s: s.notna().mean())
        monthly.loc[coverage < COMPLETE_FRACTION, c] = np.nan
    clim = monthly.groupby(monthly.index.month).mean()
    rows = []
    for month, r in clim.iterrows():
        row = {"month": int(month)}
        row.update({c: _round(r[c]) for c in clim.columns})
        rows.append(row)
    return rows


def season_stats(df: pd.DataFrame, seasons: dict = config.SEASONS,
                 dry_day_mm: float = config.DRY_DAY_MM) -> dict:
    """Per-year and summary statistics for each growing season."""
    result = {}
    for key, spec in seasons.items():
        months = spec["months"]
        sub = df[df.index.month.isin(months)]
        per_year = []
        for year, g in sub.groupby(sub.index.year):
            expected = sum(pd.Period(f"{year}-{m:02d}").days_in_month for m in months)
            rain = g[RAIN]
            frac = rain.notna().sum() / expected
            row = {"year": int(year), "coverage": _round(frac, 3)}
            if frac >= COMPLETE_FRACTION:
                row.update({
                    "rain_mm": _round(rain.sum()),
                    "rainy_days": int((rain >= dry_day_mm).sum()),
                    "longest_dry_spell_days": _longest_dry_spell(rain.dropna(), dry_day_mm),
                    "mean_tmax_c": _round(g["T2M_MAX"].mean()) if "T2M_MAX" in g else None,
                    "mean_root_wetness": _round(g["GWETROOT"].mean(), 3) if "GWETROOT" in g else None,
                })
            per_year.append(row)

        valid = [r for r in per_year if r.get("rain_mm") is not None]
        totals = np.array([r["rain_mm"] for r in valid], dtype=float)
        summary = {"years_used": len(valid)}
        if len(totals):
            mean = totals.mean()
            std = totals.std(ddof=1) if len(totals) > 1 else 0.0
            summary.update({
                "mean_rain_mm": _round(mean),
                "std_rain_mm": _round(std),
                "cv": _round(std / mean, 3) if mean else None,
                "p20_rain_mm": _round(np.percentile(totals, 20)),  # "dry year" reference
                "p80_rain_mm": _round(np.percentile(totals, 80)),  # "wet year" reference
                "mean_longest_dry_spell_days": _round(np.mean([r["longest_dry_spell_days"] for r in valid]), 1),
                # Dry/Wet scenario references for the engine spec.
                "p20_longest_dry_spell_days": _round(np.percentile(
                    [r["longest_dry_spell_days"] for r in valid], 20), 1),
                "p80_longest_dry_spell_days": _round(np.percentile(
                    [r["longest_dry_spell_days"] for r in valid], 80), 1),
                "mean_tmax_c": _round(np.nanmean([r["mean_tmax_c"] for r in valid
                                                  if r["mean_tmax_c"] is not None]) if valid else None),
            })
        et0 = season_mean_et0(df, months)
        if et0 is not None:
            summary["mean_et0_mm_day"] = et0
        result[key] = {"label": spec["label"], "months": months,
                       "summary": summary, "per_year": per_year}
    return result


def season_mean_et0(df: pd.DataFrame, months: list[int]) -> float | None:
    """Mean daily ET0 (mm/day) over a season, averaged over years that are >= 95% observed."""
    if "ERA5_ET0" not in df or not df["ERA5_ET0"].notna().any():
        return None
    sub = df.loc[df.index.month.isin(months), "ERA5_ET0"]
    vals = []
    for year, g in sub.groupby(sub.index.year):
        expected = sum(pd.Period(f"{year}-{m:02d}").days_in_month for m in months)
        if g.notna().sum() / expected >= COMPLETE_FRACTION:
            vals.append(float(g.mean()))
    return round(float(np.mean(vals)), 3) if len(vals) >= 3 else None


def season_totals(df: pd.DataFrame, column: str, months: list[int]) -> dict[int, float]:
    """{year: season total} for years where the season is >= 95% observed in `column`."""
    sub = df.loc[df.index.month.isin(months), column]
    out = {}
    for year, g in sub.groupby(sub.index.year):
        expected = sum(pd.Period(f"{year}-{m:02d}").days_in_month for m in months)
        if g.notna().sum() / expected >= COMPLETE_FRACTION:
            out[int(year)] = float(g.sum())
    return out


def rain_crosscheck(df: pd.DataFrame, seasons: dict = config.SEASONS) -> dict:
    """Compare POWER (MERRA-2) and IMERG season rainfall over the years both cover.

    Two independent NASA estimates agreeing raises confidence; a large gap is a finding
    to report, not something to average away.
    """
    out = {}
    for key, spec in seasons.items():
        a = season_totals(df, RAIN, spec["months"])
        b = season_totals(df, IMERG_RAIN, spec["months"])
        years = sorted(set(a) & set(b))
        if len(years) < 3:
            out[key] = {"years_compared": len(years)}
            continue
        x = np.array([a[y] for y in years])
        y = np.array([b[y] for y in years])
        out[key] = {
            "years_compared": len(years),
            "power_mean_mm": _round(x.mean()),
            "imerg_mean_mm": _round(y.mean()),
            "imerg_minus_power_pct": _round(100 * (y.mean() - x.mean()) / x.mean(), 1) if x.mean() else None,
            "year_to_year_correlation": _round(np.corrcoef(x, y)[0, 1], 2),
            # Same dry/wet references as the scenarios, computed from IMERG instead.
            "imerg_p20_mm": _round(np.percentile(y, 20)),
            "imerg_p80_mm": _round(np.percentile(y, 80)),
        }
    return out


def soil_moisture_stats(df: pd.DataFrame, seasons: dict = config.SEASONS) -> dict:
    """SMAP surface soil moisture per season: mean, and dry/wet-year references."""
    out = {}
    sm = df[SMAP_SM]
    for key, spec in seasons.items():
        sub = sm[sm.index.month.isin(spec["months"])]
        per_year = []
        for year, g in sub.groupby(sub.index.year):
            n = int(g.notna().sum())
            per_year.append({"year": int(year), "retrieval_days": n,
                             "mean_m3m3": _round(g.mean(), 3) if n >= SMAP_MIN_RETRIEVALS else None})
        means = np.array([r["mean_m3m3"] for r in per_year if r["mean_m3m3"] is not None])
        summary = {"years_used": int(len(means))}
        if len(means):
            summary.update({"mean_m3m3": _round(means.mean(), 3),
                            "p20_m3m3": _round(np.percentile(means, 20), 3),
                            "p80_m3m3": _round(np.percentile(means, 80), 3)})
        out[key] = {"label": spec["label"], "summary": summary, "per_year": per_year}
    return out


def climate_profile(df: pd.DataFrame) -> dict:
    annual = annual_rainfall(df)
    valid_annual = [r["rain_mm"] for r in annual if r["rain_mm"] is not None]
    profile = {
        "period": {"start": df[RAIN].first_valid_index().strftime("%Y-%m-%d"),
                   "end": df[RAIN].last_valid_index().strftime("%Y-%m-%d"),
                   "days": int(df[RAIN].notna().sum())},
        "completeness": completeness(df),
        "annual": {
            "mean_rain_mm": _round(np.mean(valid_annual)) if valid_annual else None,
            "mean_t2m_c": _round(df["T2M"].mean()) if "T2M" in df else None,
            "per_year": annual,
        },
        "monthly_climatology": monthly_climatology(df),
        "seasons": season_stats(df),
    }
    if IMERG_RAIN in df and df[IMERG_RAIN].notna().any():
        profile["rain_crosscheck"] = rain_crosscheck(df)
    if SMAP_SM in df and df[SMAP_SM].notna().any():
        profile["soil_moisture"] = soil_moisture_stats(df)
    return profile

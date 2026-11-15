"""Rotation engine tests. Climate inputs are SYNTHETIC season summaries with round numbers
so scores can be checked by hand; they are not real climate data."""
from __future__ import annotations

import datetime as dt
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, engine  # noqa: E402
from farm_twin.crops import CROPS  # noqa: E402

TODAY = dt.date(2026, 11, 12)


def summary(mean=470, p20=370, p80=510, dmean=10, dp20=7, dp80=12, tmax=30.0, years=25):
    return {"summary": {"years_used": years, "mean_rain_mm": mean, "p20_rain_mm": p20,
                        "p80_rain_mm": p80, "mean_longest_dry_spell_days": dmean,
                        "p20_longest_dry_spell_days": dp20, "p80_longest_dry_spell_days": dp80,
                        "mean_tmax_c": tmax}}


def profile(a=None, b=None, years_ok=25, years_total=25):
    per_year = [{"year": 2000 + i, "rain_mm": 1100.0 if i < years_ok else None}
                for i in range(years_total)]
    return {"seasons": {"A": a or summary(), "B": b or summary()},
            "annual": {"per_year": per_year}}


def by_label(result):
    return {r["label"]: r for r in result.rows}


def test_enumeration_matches_spec_count():
    rots = engine.enumerate_rotations()
    assert len(rots) == 357 and len(set(rots)) == 357
    cassava = [r for r in rots if "cassava" in r]
    assert len(cassava) == 14
    # cassava is always a contiguous two-slot block starting in slot 1 or 2
    assert all(r.count("cassava") == 2 and r[1] == "cassava" for r in cassava)


@pytest.mark.parametrize("today,expected", [
    (dt.date(2026, 11, 12), [("A", 2027), ("B", 2027), ("A", 2028)]),
    (dt.date(2026, 5, 1), [("B", 2026), ("A", 2027), ("B", 2027)]),
    (dt.date(2026, 2, 28), [("A", 2026), ("B", 2026), ("A", 2027)]),
])
def test_next_slots(today, expected):
    assert engine.next_slots(today) == expected


def test_season_update_by_hand():
    c = [engine.SlotClimate(1, "A", 2027, P=400, D=15, T=35.5)] * 3
    w = {"C": 0.2, "W": 0.2, "H": 0.2, "K": 0.2, "F": 0.2}
    # Explicit penalties so the arithmetic is checked independently of the defaults.
    p = engine.EngineParams(repeat_penalty=0.35, streak_penalty=0.10)
    run = engine.simulate(("maize",) * 3, c, c0=None, k0=0, alpha=0.5, weights=w, params=p)
    s = run["seasons"][0]
    assert s["r"] == pytest.approx(0.75 * 400 / 500)        # 0.6
    assert s["g"] == pytest.approx(1 - (15 - 10) / 10)       # 0.5
    assert s["f_T"] == pytest.approx((38 - 35.5) / (38 - 33))  # 0.5
    assert s["F"] == pytest.approx(0.6 * (0.5 * 0.9 + 0.5 * 0.6))
    assert run["N3"] == pytest.approx(3 * 0.6 * -40)
    assert run["R3"] == pytest.approx(3 * 0.6 * 0.7)
    assert run["H"] == pytest.approx(0.6 * max(0, 0.5 - 72 / 120) + 0.4 * 1.26 / 3)
    # no previous crop: slot 1 free; slot 2 repeat 0.35; slot 3 repeat + streak 0.45
    assert run["K"] == pytest.approx(1 - 0.35 - 0.45)
    assert run["S"] == pytest.approx(sum(0.2 * run[t] for t in engine.TERMS))


def test_v011_defaults():
    p = engine.EngineParams()
    assert (p.repeat_penalty, p.streak_penalty) == (0.60, 0.30)
    w = engine.farm_weights(config.PILOT_FARM)
    assert w == pytest.approx({"C": 0.10, "W": 0.25, "H": 0.25, "K": 0.20, "F": 0.20})


def test_legume_monoculture_not_near_the_top():
    res = engine.rank_rotations(profile(), today=TODAY)
    assert by_label(res)["Cowpea → Cowpea → Cowpea"]["rank"] > 20
    for r in res.rows[:10]:
        assert sum(CROPS[c].family == "legume" for c in r["rotation"]) < 3, r["label"]


def test_waterlogging_factor():
    wet = [engine.SlotClimate(i, "A", 2027, P=600, D=5, T=28) for i in (1, 2, 3)]
    w = {t: 0.2 for t in engine.TERMS}
    beans = engine.simulate(("beans",) * 3, wet, c0=None, k0=0, alpha=0.6, weights=w)
    cowpea = engine.simulate(("cowpea",) * 3, wet, c0=None, k0=0, alpha=0.6, weights=w)
    assert beans["seasons"][0]["f_wet"] == 0.7   # 600 > 1.5 * 350, waterlog-sensitive
    assert cowpea["seasons"][0]["f_wet"] == 1.0  # not sensitive


def test_acceptance_monoculture_scores_below_legume_rotation():
    res = engine.rank_rotations(profile(), today=TODAY)
    rows = by_label(res)
    mono = rows["Maize → Maize → Maize"]
    rot = rows["Maize → Beans → Groundnut"]
    for sc in engine.SCENARIOS:
        assert mono["scores"][sc]["H"] < rot["scores"][sc]["H"]
        assert mono["scores"][sc]["K"] < rot["scores"][sc]["K"]
        assert mono["scores"][sc]["S"] < rot["scores"][sc]["S"]


def test_previous_crop_counts_in_first_transition():
    res = engine.rank_rotations(profile(), today=TODAY)
    t = by_label(res)["Maize → Beans → Groundnut"]["transitions"]
    assert t[0]["from"] == "maize" and t[0]["penalty"] == pytest.approx(0.60)
    assert t[1]["penalty"] == 0
    assert t[2]["penalty"] == pytest.approx(0.15)  # groundnut after beans: same family


def test_disease_pair_penalised():
    res = engine.rank_rotations(profile(), today=TODAY)
    t = by_label(res)["Sunflower → Beans → Maize"]["transitions"]
    assert t[1]["penalty"] == pytest.approx(0.20)
    assert t[1]["reasons"][0] == {"key": "reason.disease",
                                  "params": {"crop": "beans", "prev": "sunflower", "disease": "sclerotinia"}}


def test_cassava_block_has_no_internal_transition():
    res = engine.rank_rotations(profile(), today=TODAY)
    row = by_label(res)["Cassava (2 seasons) → Beans"]
    assert row["rotation"] == ["cassava", "cassava", "beans"]
    assert [t["slot"] for t in row["transitions"]] == [1, 3]
    assert len(row["seasons"]["baseline"]) == 3


def test_scenarios_pick_the_right_statistics():
    res = engine.rank_rotations(profile(), today=TODAY)
    sc = res.meta["scenarios"]
    assert sc["baseline"][0] == {"slot": 1, "season": "A 2027", "P_mm": 470, "D_days": 10, "T_c": 30.0}
    assert (sc["dry"][0]["P_mm"], sc["dry"][0]["D_days"]) == (370, 12)
    assert (sc["wet"][0]["P_mm"], sc["wet"][0]["D_days"]) == (510, 7)
    assert sc["hot"][0]["T_c"] == 32.0 and sc["hot"][0]["P_mm"] == 470


def test_hard_constraints_exclude_and_report():
    # Very dry A season: maize r = 0.75*300/500 = 0.45 < 0.5. Hot B: beans Tmax 35 >= 35.
    res = engine.rank_rotations(profile(a=summary(mean=300), b=summary(tmax=35.0)), today=TODAY)
    for r in res.rows:
        for slot, crop in enumerate(r["rotation"], 1):
            season = res.meta["slots"][slot - 1]["season"]
            assert not (crop == "maize" and season == "A")
            assert not (crop == "beans" and season == "B")
    reasons = " ".join(e["reason"] for e in res.meta["excluded"])
    assert "Maize would get less than 50%" in reasons
    assert {e["code"] for e in res.meta["excluded"]} == {"exclude.water", "exclude.heat"}
    assert res.meta["rotations_feasible"] < 357


def test_dry_flag_and_robustness_consistent():
    res = engine.rank_rotations(profile(), today=TODAY)
    for r in res.rows:
        assert r["dry_flag"] == (r["ranks"]["dry"] - r["ranks"]["baseline"] > 5)
        assert r["robustness"]["min_S"] == min(r["scores"][sc]["S"] for sc in engine.SCENARIOS)
    assert [r["rank"] for r in res.rows] == list(range(1, len(res.rows) + 1))


def test_confidence_for_pilot_is_low_and_says_how_to_raise_it():
    q = engine.confidence(profile()["annual"]["per_year"], config.PILOT_FARM)
    assert q["q"] == pytest.approx(0.4) and q["level"] == "Low"
    assert q["raise_it"]
    farm = {**config.PILOT_FARM, "soil_profile": {"data_source": "SoilGrids"}}
    q2 = engine.confidence(profile(years_ok=20)["annual"]["per_year"], farm)
    assert q2["q"] == pytest.approx(0.4 * 0.8 + 0.15)


def test_weights_normalised_and_validated():
    w = engine.farm_weights(config.PILOT_FARM, {"soil_health": 0.8})
    assert sum(w.values()) == pytest.approx(1.0)
    assert w["H"] == pytest.approx(0.8 / 1.55)
    with pytest.raises(engine.EngineError):
        engine.farm_weights({"farmer_preferences": {}})


def test_missing_climate_raises():
    bad = profile(a={"summary": {"years_used": 0}})
    with pytest.raises(engine.EngineError):
        engine.rank_rotations(bad, today=TODAY)


def test_recommend_includes_control_and_crop_table():
    out = engine.recommend(profile(), top=5, today=TODAY)
    assert len(out["rotations"]) == 5
    assert [c["label"] for c in out["controls"]] == ["Maize → Maize → Maize"]
    assert set(out["crops"]) == set(CROPS)
    assert out["meta"]["evaluations"] == 357 * 4
    assert out["rotations"][0]["largest_penalty"]


def test_water_need_from_et0_and_kc():
    """v0.2: need = ET0 x days x average Kc (FAO-56); falls back to the fixed v0.1.1 value without ET0."""
    from farm_twin.crops import CROPS
    params = engine.EngineParams()
    maize = CROPS["maize"]
    # (30*.3 + 50*.75 + 60*1.2 + 40*.85) / 180 = 152.5 / 180
    assert abs(engine.kc_average(maize) - 152.5 / 180) < 1e-9
    sc = engine.SlotClimate(1, "A", 2027, P=450, D=10, T=30, et0=4.5)
    need, src = engine.water_need(maize, sc, params)
    assert src == "fao56" and abs(need - 4.5 * 110 * 152.5 / 180) < 1e-9  # about 419 mm
    legacy, src0 = engine.water_need(maize, engine.SlotClimate(1, "A", 2027, 450, 10, 30), params)
    assert src0 == "assumed" and legacy == maize.water_need_mm
    cassava = CROPS["cassava"]                                            # fills the whole slot
    need_c, _ = engine.water_need(cassava, sc, params)
    assert abs(need_c - 4.5 * params.season_window_days * engine.kc_average(cassava)) < 1e-9


def test_et0_changes_maize_but_not_ranking_validity():
    from test_offline import profile, summary
    a0 = engine.rank_rotations(profile(), today=__import__("datetime").date(2026, 11, 12))
    a1 = engine.rank_rotations(profile(a=summary(et0=4.45), b=summary(475.46, 359.95, 526.74, 8.1, 5.0, 10.2, 29.66, et0=4.51)),
                               today=__import__("datetime").date(2026, 11, 12))
    s0 = a0.rows[0]["seasons"]["baseline"][0]
    s1 = a1.rows[0]["seasons"]["baseline"][0]
    assert s0["WR_source"] == "assumed" and s1["WR_source"] == "fao56"
    assert a1.meta["versions"]["engine"] == "0.2.0"


def test_concern_text_is_plain_language_in_every_language():
    """The table's 'Main concern' names months and plain risks, never season codes or slot numbers."""
    import re
    from test_offline import profile
    import datetime as dt
    for lang in ("en", "sw", "lg", "teo"):
        res = engine.rank_rotations(profile(), today=dt.date(2026, 11, 12), lang=lang)
        for row in res.rows[:40]:
            for text in [row["largest_penalty"] or ""] + [i["text"] for i in row["issues"]]:
                assert not re.search(r"\b[AB] 20\d\d\b", text), text        # no "B 2027" codes
                assert "(season" not in text.lower() and "msimu 2" not in text and "{" not in text, text
    top = engine.rank_rotations(profile(), today=dt.date(2026, 11, 12), lang="en").rows[0]
    assert re.search(r"may get only \d+% of the water it needs in (Mar-Jun|Aug-Nov) 20\d\d", top["largest_penalty"]), top["largest_penalty"]

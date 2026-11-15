"""NDVI summary, terrain slope maths, and that evidence rides in the pack without changing versions."""
import math
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, evidence, pack, terrain, vegetation  # noqa: E402


def fake_store(years=range(2010, 2014)):
    """Two green peaks (Apr-May and Oct), bare in Jan-Feb and Jul-Aug."""
    base = {1: .30, 2: .30, 3: .45, 4: .65, 5: .65, 6: .50, 7: .35, 8: .40, 9: .55, 10: .66, 11: .55, 12: .40}
    recs = {}
    for y in years:
        for m, v in base.items():
            recs[f"{y}-{m:02d}-10MOD"] = {"date": f"{y}-{m:02d}-10", "sensor": "MOD", "ndvi": v + 0.01 * (y - 2010),
                                          "pixel": [1.7, 33.6]}
    recs["2010-03-01MYD"] = {"date": "2010-03-01", "sensor": "MYD", "ndvi": None, "pixel": [1.7, 33.6]}
    return {"records": recs, "years_done": list(years)}


def test_ndvi_summary_finds_peaks_and_bare_months():
    s = vegetation.summarize(fake_store())
    assert s["greenness_peaks_months"] == [4, 10] or s["greenness_peaks_months"] == [5, 10]
    assert {1, 2} <= set(s["low_cover_months"])
    assert s["composites"]["valid"] < s["composites"]["total"]          # the cloudy composite stays missing
    assert s["seasons"]["A"]["years"] == 4 and s["seasons"]["A"]["greenest_year"] == 2013
    assert s["season_window_check"]["peaks_inside_windows"] is True


def test_ndvi_peaks_outside_windows_are_flagged():
    store = fake_store()
    shifted = {k: dict(v, ndvi=(0.8 if v["date"][5:7] in ("01", "12") else 0.3)) for k, v in store["records"].items()}
    s = vegetation.summarize({"records": shifted, "years_done": []})
    assert s["season_window_check"]["peaks_inside_windows"] is False


def test_slope_of_a_tilted_plane():
    # rises 5 m per 100 m toward the east: 5 % slope, water flows west (270 deg)
    dx = dy = 30.0
    z = np.tile(np.arange(7) * 1.5, (7, 1))
    slope, aspect = terrain._horn(z, dx, dy)
    assert math.isclose(float(slope.mean()), 5.0, rel_tol=1e-6)
    assert math.isclose(float(aspect.mean()), 270.0, abs_tol=1e-6)


def test_slope_classes():
    assert terrain.slope_class(1) == "flat" and terrain.slope_class(3.5) == "gently sloping"
    assert terrain.slope_class(20) == "steep"


def test_evidence_in_pack_does_not_change_data_pack_version(tmp_path, monkeypatch):
    from test_offline import build, profile  # same helpers the pack tests use
    base = build(profile())
    withev = pack.build_pack(profile(), {}, config.PILOT_FARM, evidence={"footprint_note": "x"})
    assert withev["evidence"] == {"footprint_note": "x"}
    assert "evidence" not in base
    assert withev["versions"]["data_pack"] == base["versions"]["data_pack"]


def test_evidence_absent_when_nothing_cached(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path)
    assert evidence.build(1.71, 33.61) is None


def test_fieldscale_summary_bare_share_and_thin_months():
    from farm_twin import fieldscale
    recs = {}
    for y in (2020, 2021):
        for m, nd, bare in ((1, 0.2, 0.9), (4, 0.6, 0.0), (10, 0.65, 0.0)):
            for d in (3, 12, 21):
                recs[f"{y}{m}{d}"] = {"date": f"{y}-{m:02d}-{d:02d}", "ndvi": nd, "bare_share": bare, "clear": 1.0}
    recs["cloudy"] = {"date": "2020-05-05", "ndvi": None, "clear": 0.1}
    s = fieldscale.summarize({"records": recs, "square_m": 140})
    assert s["scenes"] == {"total": len(recs), "clear": len(recs) - 1}
    assert s["monthly"][1]["bare_share"] == 0.9 and s["monthly"][1]["thin"] is True   # 6 looks < 8
    assert s["least_green_months"][0] == 1 and 5 not in s["monthly"]                   # cloudy month absent
    assert s["seasons"]["A"]["bare_share"] == 0.0

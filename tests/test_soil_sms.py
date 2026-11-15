"""SoilGrids parsing, texture classes, soil in the farm record and confidence; SMS building
and segmenting; the /api/sms endpoint. Offline: SoilGrids responses are SYNTHETIC, shaped like
the real API's JSON."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, engine, sms, soil  # noqa: E402
from test_engine import TODAY, profile  # noqa: E402


def layer(name, d_factor, means, unit="x"):
    labels = ["0-5cm", "5-15cm", "15-30cm"]
    return {"name": name, "unit_measure": {"d_factor": d_factor, "mapped_units": unit, "target_units": unit},
            "depths": [{"label": lab, "values": {"mean": m, "Q0.05": m // 2, "Q0.95": m * 2}}
                       for lab, m in zip(labels, means)]}


SOILGRIDS = {"type": "Feature", "properties": {"layers": [
    layer("phh2o", 10, [60, 60, 60]), layer("soc", 10, [250, 210, 200]), layer("nitrogen", 100, [190, 190, 185]),
    layer("clay", 10, [350, 370, 380]), layer("sand", 10, [400, 390, 390]), layer("silt", 10, [250, 240, 230]),
    layer("cec", 10, [170, 165, 165]), layer("bdod", 100, [130, 131, 133]),
]}}


def test_parse_depth_weighted_with_range():
    s = soil.parse(SOILGRIDS, 1.71, 33.61)
    soc = s["properties"]["organic_carbon"]
    assert soc["value"] == pytest.approx(round((25.0 * 5 + 21.0 * 10 + 20.0 * 15) / 30, 2))
    assert soc["low"] < soc["value"] < soc["high"] and soc["unit"] == "g/kg"
    assert s["properties"]["ph"]["value"] == 6.0
    assert s["texture_class"] == "clay loam" and s["kind"] == "modelled estimate"


@pytest.mark.parametrize("sand,silt,clay,cls", [
    (92, 5, 3, "sand"), (82, 12, 6, "loamy sand"), (65, 25, 10, "sandy loam"), (40, 40, 20, "loam"),
    (20, 65, 15, "silt loam"), (5, 88, 7, "silt"), (60, 12, 28, "sandy clay loam"), (35, 30, 35, "clay loam"),
    (10, 55, 35, "silty clay loam"), (50, 10, 40, "sandy clay"), (5, 45, 50, "silty clay"), (20, 20, 60, "clay"),
])
def test_usda_texture_classes(sand, silt, clay, cls):
    assert soil.texture_class(sand, silt, clay) == cls


def test_no_values_is_an_error():
    with pytest.raises(soil.SoilError):
        soil.parse({"properties": {"layers": [{"name": "phh2o", "unit_measure": {"d_factor": 10},
                                               "depths": [{"label": "0-5cm", "values": {"mean": None}}]}]}}, 0, 0)


def test_fetch_caches_and_retries_rate_limit(monkeypatch):
    monkeypatch.setattr(soil.time, "sleep", lambda s: None)
    calls = []

    def handler(request):
        calls.append(request.url)
        return httpx.Response(429) if len(calls) == 1 else httpx.Response(200, json=SOILGRIDS)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        s = soil.fetch(1.71, 33.61, client=client)
    assert len(calls) == 2 and "property=phh2o" in str(calls[0])
    assert soil.load_cached(1.71, 33.61)["texture_class"] == s["texture_class"]
    assert soil.fetch(1.71, 33.61)["fetched_at"] == s["fetched_at"]  # served from cache, no network


def test_soil_raises_confidence_to_medium_and_never_replaces_lab_data():
    farm = soil.farm_with_soil(config.PILOT_FARM, soil.parse(SOILGRIDS, 1.71, 33.61))
    assert farm["soil_profile"]["data_source"] == "SoilGrids" and farm["soil_profile"]["ph"] == 6.0
    assert config.PILOT_FARM["soil_profile"]["ph"] is None  # the original record is untouched
    q = engine.confidence(profile()["annual"]["per_year"], farm)
    assert q["level"] == "Medium" and q["q"] == pytest.approx(0.55)
    lab = {**config.PILOT_FARM, "soil_profile": {"data_source": "lab", "ph": 5.5}}
    assert soil.farm_with_soil(lab, soil.parse(SOILGRIDS, 1.71, 33.61)) is lab


# --- SMS -------------------------------------------------------------------------------

def test_segment_rules():
    assert sms.segment("a" * 160) == {"encoding": "GSM-7", "chars": 160, "segments": ["a" * 160]}
    words = " ".join(["word"] * 40)                      # 199 chars -> 2 parts of <= 153
    out = sms.segment(words)
    assert out["encoding"] == "GSM-7" and len(out["segments"]) == 2
    assert all(len(s) <= 153 for s in out["segments"]) and " ".join(out["segments"]) == words
    assert sms.segment("ok {}")["chars"] == 7             # o, k, space = 3; { and } cost 2 each
    uni = sms.segment("ŋ" * 71)                          # not GSM-7 -> UCS-2, 67 per part
    assert uni["encoding"] == "UCS-2" and [len(s) for s in uni["segments"]] == [67, 4]
    assert sms.sanitize("32.5 °C → ok") == "32.5 C - ok"


@pytest.mark.parametrize("lang", ["en", "lg", "sw", "teo"])
def test_plan_text_in_every_language(lang):
    res = engine.rank_rotations(profile(), today=TODAY, lang=lang)
    out = sms.plan_sms(res.rows[0], res.meta, config.PILOT_FARM, lang, "2025-12-31")
    assert out["encoding"] == "GSM-7", out["text"]       # our languages need no UCS-2
    assert "ARAPAI FARM TWIN" in out["text"] and "Dec 2025" in out["text"] or lang != "en"
    assert "2025-12-31" not in out["text"] and "Score" not in out["text"] and "rank" not in out["text"].lower()
    assert "FT-UG-0001" not in out["text"]                # no internal codes in a farmer's message
    assert out["text"].count(chr(10)) >= 4 and "-" in out["text"].split(chr(10))[1]   # one plain line per season
    # Luganda has no short month names, so its real month names make each season line longer: one more SMS part
    assert "{" not in out["text"] and len(out["segments"]) <= (4 if lang == "lg" else 3)
    first_crop = engine.i18n.t(lang, f"crop.{res.rows[0]['rotation'][0]}")
    assert first_crop in out["text"]


def test_api_sms_matches_device_settings(monkeypatch):
    from fastapi.testclient import TestClient
    from farm_twin import api, power
    from test_power_and_climate import synthetic_power_response

    monkeypatch.setattr(power, "fetch_daily", lambda *a, **k: synthetic_power_response(rain=4.0))
    client = TestClient(api.app)
    top = client.get("/api/sms?lang=sw").json()
    assert top["encoding"] == "GSM-7" and "Mahindi" not in top["text"].split(":")[0]
    mine = client.get("/api/sms?lang=en&rotation=maize,beans,groundnut").json()
    assert mine["rotation"] == ["maize", "beans", "groundnut"] and "Maize" in mine["text"]
    # changing last season's crop changes the plan's rank, as on the device
    a = client.get("/api/sms?rotation=maize,beans,groundnut").json()["text"]
    b = client.get("/api/sms?rotation=maize,beans,groundnut&previous_crop=beans").json()["text"]
    assert a != b and "after beans" in b
    assert client.get("/api/sms?rotation=rice,beans,maize").status_code == 422
    assert client.get("/api/sms?previous_crop=rice").status_code == 422
    assert client.get("/api/sms?lang=zz").status_code == 422


def test_sms_names_the_months_and_the_worry_in_plain_words():
    res = engine.rank_rotations(profile(), today=TODAY, lang="en")
    text = sms.plan_text(res.rows[0], res.meta, config.PILOT_FARM, "en", "2025-12-31")
    assert "Mar-Jun 2027: plant " in text and "Aug-Nov 2027: plant " in text
    assert "Careful" in text and "of the water it needs" in text and "season 2" not in text
    assert sms.season_label("en", "A", 2027) == "Mar-Jun 2027" and sms.month_year("en", "2025-12-31") == "Dec 2025"

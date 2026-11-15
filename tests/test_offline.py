"""Offline-first: the farm pack, plan sync, and that the browser engine (frontend/engine.js)
ranks exactly like the Python engine. Synthetic climate; no network."""
from __future__ import annotations

import datetime as dt
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from farm_twin import config, engine, pack, plans  # noqa: E402

NODE = shutil.which("node")
TODAY = dt.date(2026, 11, 12)


def summary(mean=471.46, p20=370.11, p80=511.21, dmean=9.9, dp20=7.0, dp80=12.2, tmax=30.46, years=25, et0=None):
    out = {"years_used": years, "mean_rain_mm": mean, "p20_rain_mm": p20, "p80_rain_mm": p80,
            "mean_longest_dry_spell_days": dmean, "p20_longest_dry_spell_days": dp20,
            "p80_longest_dry_spell_days": dp80, "mean_tmax_c": tmax}
    if et0 is not None:
        out["mean_et0_mm_day"] = et0
    return out


def profile(a=None, b=None, years_ok=25):
    per_year = [{"year": 2001 + i, "rain_mm": 1100.0 if i < years_ok else None, "coverage": 1.0}
                for i in range(25)]
    return {
        "period": {"start": "2001-01-01", "end": "2025-12-31", "days": 9131},
        "annual": {"mean_rain_mm": 1151.0, "mean_t2m_c": 24.1, "per_year": per_year},
        "monthly_climatology": [{"month": m, "PRECTOTCORR": 80.0 + m, "T2M_MAX": 30.0} for m in range(1, 13)],
        "seasons": {"A": {"label": "First rains", "months": [3, 4, 5, 6], "summary": a or summary()},
                    "B": {"label": "Second rains", "months": [8, 9, 10, 11],
                          "summary": b or summary(475.46, 359.95, 526.74, 8.1, 5.0, 10.2, 29.66)}},
    }


SOURCES = {"power": {"status": "ok", "label": "NASA POWER",
                     "provenance": {"dataset": "MERRA-2", "access": "test",
                                    "latitude": 1.71, "longitude": 33.61}}}
TIES = dict(a=summary(mean=300.0, dmean=12.5, dp80=15.5, tmax=30.25),
            b=summary(471.5, 350.0, 600.0, 10.5, 6.5, 13.5, 35.0))


def build(prof=None):
    return pack.build_pack(prof or profile(), SOURCES, config.PILOT_FARM,
                           generated_at=dt.datetime(2026, 11, 12, tzinfo=dt.timezone.utc))


def test_pack_is_small_and_complete():
    p = build()
    size = len(json.dumps(p, ensure_ascii=False).encode("utf-8"))
    assert size < 300_000, size
    assert set(p["i18n"]) >= {"en", "lg", "sw", "teo"}
    assert p["data_through"] == "2025-12-31"
    assert len(p["engine"]["crops"]) == 8
    assert p["engine"]["disease_pairs"] == [["beans", "sunflower", "sclerotinia"]]
    assert p["engine"]["params"]["repeat_penalty"] == 0.6


def run_js(p, cases, tmp_path):
    (tmp_path / "pack.json").write_text(json.dumps(p), encoding="utf-8")
    (tmp_path / "cases.json").write_text(json.dumps(cases), encoding="utf-8")
    mjs = tmp_path / "engine.mjs"  # .mjs: always an ES module, whatever the Node version
    shutil.copy(ROOT / "frontend" / "engine.js", mjs)
    out = subprocess.run([NODE, str(ROOT / "tests" / "engine_parity.mjs"), mjs.as_uri(),
                          str(tmp_path / "pack.json"), str(tmp_path / "cases.json")],
                         capture_output=True, text=True, encoding="utf-8", timeout=120)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


CASES = [
    {"lang": "en"},
    {"lang": "lg", "weights": {"soil_health": 0.0, "farmer_utility": 1.0}},
    {"lang": "sw", "alpha": 0.2, "residues_removed": True},
    {"lang": "teo", "farm": {**config.PILOT_FARM,
                             "farming_history": {"previous_crops": ["beans", "cowpea"]}}},
]


ET0 = dict(a=summary(et0=4.45), b=summary(475.46, 359.95, 526.74, 8.1, 5.0, 10.2, 29.66, et0=4.51))


@pytest.mark.skipif(NODE is None, reason="node not installed")
@pytest.mark.parametrize("prof", [profile(), profile(**TIES), profile(**ET0)],
                         ids=["soroti-like", "ties-and-exclusions", "fao56-water-need"])
def test_browser_engine_matches_python(prof, tmp_path):
    p = build(prof)
    js = run_js(p, [{**c, "today": [2026, 11, 12]} for c in CASES], tmp_path)
    for case, got in zip(CASES, js):
        res = engine.rank_rotations(p["climate"], case.get("farm", config.PILOT_FARM), today=TODAY,
                                    lang=case["lang"], weights=case.get("weights"),
                                    alpha=case.get("alpha"), residues_removed=case.get("residues_removed"),
                                    data_pack_version=p["versions"]["data_pack"])
        assert got["versions"] == res.meta["versions"] and got["version_mismatch"] is False
        assert got["feasible"] == res.meta["rotations_feasible"]
        assert [(e["reason"], e["rotations"]) for e in got["excluded"]] == \
               [(e["reason"], e["rotations"]) for e in res.meta["excluded"]]
        assert got["level"] == res.meta["confidence"]["level"]
        assert round(got["q"], 3) == res.meta["confidence"]["q"]
        assert got["steps"] == [s["step"] for s in res.meta["confidence"]["raise_it"]]
        assert len(got["rows"]) == len(res.rows)
        for g, r in zip(got["rows"], res.rows):
            assert (g["label"], g["display"], g["rank"], g["ranks"]) == \
                   (r["label"], r["display"], r["rank"], r["ranks"])
            for sc, s in g["S"].items():  # Python output is rounded to 3 places
                assert round(s, 3) == r["scores"][sc]["S"], (g["label"], sc)
            for term in engine.TERMS:
                assert round(g["terms"][term], 3) == r["scores"]["baseline"][term]
            assert g["dry_flag"] == r["dry_flag"] and g["worst"] == r["robustness"]["scenario"]
            assert g["largest_penalty"] == r["largest_penalty"]
            assert g["issues"] == [i["text"] for i in r["issues"]]


def test_versions_travel_with_pack_and_results():
    p = build()
    v = p["versions"]
    assert v["engine"] == engine.ENGINE_VERSION and v["rules"] == engine.RULES_VERSION
    assert v["data_pack"].startswith("2025-12-31+") and len(v["data_pack"]) == len("2025-12-31+") + 8
    assert build(profile(a=summary(mean=400.0)))["versions"]["data_pack"] != v["data_pack"]
    js_src = (ROOT / "frontend" / "engine.js").read_text(encoding="utf-8")
    assert f'export const ENGINE_VERSION = "{engine.ENGINE_VERSION}";' in js_src


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_flags_a_pack_made_for_another_engine(tmp_path):
    p = build()
    p["versions"]["engine"] = "9.9.9"
    got = run_js(p, [{"lang": "en", "today": [2026, 11, 12]}], tmp_path)[0]
    assert got["version_mismatch"] is True and got["feasible"] == 357


def test_ties_case_really_has_exclusions():
    res = engine.rank_rotations(build(profile(**TIES))["climate"], today=TODAY)
    assert res.meta["rotations_feasible"] < 357


@pytest.mark.skipif(NODE is None, reason="node not installed")
def test_js_number_formatting_matches_python(tmp_path):
    """Python rounds exact ties half to even (12.5 -> '12'); JS toFixed does not."""
    values = [12.5, 13.5, 0.5, 1.5, 30.25, 30.75, 30.35, 0.15, 2.675, 9.9, 471.5, 472.5, 54.5, 63.49999,
              7.0, 10.45, 29.66, 32.46, 85.0, 84.99, 1e-9, 99.95]
    mjs = tmp_path / "engine.mjs"
    shutil.copy(ROOT / "frontend" / "engine.js", mjs)
    script = (f"const E = await import({json.dumps(mjs.as_uri())});"
              f"const v = {json.dumps(values)};"
              "console.log(JSON.stringify(v.map(x => [E.pyFixed(x, 0), E.pyFixed(x, 1), E.pyRound(x)])));")
    out = subprocess.run([NODE, "--input-type=module", "-e", script], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout)
    want = [[f"{x:.0f}", f"{x:.1f}", round(x)] for x in values]
    assert got == want


@pytest.fixture
def plan_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(plans, "PLANS_DIR", tmp_path / "plans")
    return tmp_path / "plans"


def plan(pid="lx1abc-0011223344556677", rotation=("cowpea", "finger_millet", "cowpea")):
    return {"id": pid, "created_at": "2026-11-12T08:00:00Z", "rotation": list(rotation), "score": 0.81}


def test_plan_sync_is_idempotent_and_validated(plan_dir):
    farm = config.PILOT_FARM["farm_id"]
    out = plans.merge(farm, [plan(), plan("bad id!"), plan("lx1abc-ffff", ("maize", "rice", "beans"))])
    assert out["accepted"] == ["lx1abc-0011223344556677"]
    assert {r["id"] for r in out["rejected"]} == {"bad id!", "lx1abc-ffff"}
    again = plans.merge(farm, [plan()])  # a retry after a dropped connection
    assert again == {"accepted": [], "duplicates": ["lx1abc-0011223344556677"], "rejected": []}
    assert len(plans.load(farm)) == 1


def test_api_pack_plans_and_offline_files(plan_dir, monkeypatch):
    from fastapi.testclient import TestClient
    from farm_twin import api, power
    from farm_twin.sources import NotFetched
    from test_power_and_climate import synthetic_power_response

    def not_fetched(*a, **k):
        raise NotFetched("not fetched")

    monkeypatch.setattr(power, "fetch_daily", lambda *a, **k: synthetic_power_response(rain=4.0))
    monkeypatch.setattr(api.SOURCES["imerg"], "fetch", not_fetched)
    monkeypatch.setattr(api.SOURCES["smap"], "fetch", not_fetched)
    client = TestClient(api.app)
    body = client.get("/api/pack").json()
    assert body["pack_version"] == 1 and "engine" in body
    assert body["sources"]["smap"]["status"] == "not_fetched"
    farm = config.PILOT_FARM["farm_id"]
    r = client.post(f"/api/farms/{farm}/plans", json={"plans": [plan()]})
    assert r.status_code == 200 and r.json()["accepted"]
    assert len(client.get(f"/api/farms/{farm}/plans").json()["plans"]) == 1
    assert client.post("/api/farms/OTHER/plans", json={"plans": []}).status_code == 404
    assert client.post(f"/api/farms/{farm}/plans", json={"plans": "x"}).status_code == 422
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert "manifest" in client.get("/manifest.webmanifest").headers["content-type"]
    js = client.get("/static/engine.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]


def test_static_app_files_must_be_revalidated():
    """A new deploy's engine.js must not be hidden by a browser-cached copy (engine version mismatch)."""
    from fastapi.testclient import TestClient
    from farm_twin import api
    r = TestClient(api.app).get("/static/engine.js")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-cache"


def test_monitors_can_probe_with_head():
    """UptimeRobot-style monitors send HEAD; a 405 made a healthy site look down."""
    from fastapi.testclient import TestClient
    from farm_twin import api
    c = TestClient(api.app)
    for path in ("/api/health", "/"):
        assert c.head(path).status_code == 200, path
        assert c.get(path).status_code == 200, path
    assert c.get("/api/health").json() == {"status": "ok"}


def test_smartphone_page_qr_and_install_icons():
    """/phone shows the live app in a phone frame with a QR code; the manifest has the PNG icons Android needs."""
    import json as _json
    from fastapi.testclient import TestClient
    from farm_twin import api
    c = TestClient(api.app)
    page = c.get("/phone")
    assert page.status_code == 200 and c.head("/phone").status_code == 200
    assert 'src="/?embed=1"' in page.text and "/static/phone-qr.svg" in page.text and "Install app" in page.text
    qr = c.get("/static/phone-qr.svg")
    assert qr.status_code == 200 and "<svg" in qr.text
    manifest = _json.loads(c.get("/manifest.webmanifest").text)
    sizes = {(i["sizes"], i.get("purpose")) for i in manifest["icons"] if i["type"] == "image/png"}
    assert ("192x192", "any") in sizes and ("512x512", "any") in sizes and ("512x512", "maskable") in sizes
    for i in manifest["icons"]:
        assert c.get(i["src"]).status_code == 200, i["src"]
    assert c.get("/static/apple-touch-icon.png").status_code == 200
    home = c.get("/").text
    assert 'href="/phone"' in home and "apple-touch-icon" in home and "embed" in home


def test_page_is_answer_first_with_folded_details():
    """User-centred layout: the best plan card exists, long sections are collapsible, priorities use plain labels."""
    from fastapi.testclient import TestClient
    from farm_twin import api
    html = TestClient(api.app).get("/").text
    assert 'id="best"' in html and 'id="jump"' in html and 'id="rot-more"' in html
    for fold in ("fold-farm", "fold-compare", "fold-sms", "evidence", "fold-charts", "fold-seasons", "fold-sources"):
        assert f'<details class="card fold" id="{fold}"' in html, fold
    js = (ROOT / "frontend" / "app.js").read_text(encoding="utf-8")
    assert "renderBest" in js and "initFolds" in js and "prio.${term}" in js
    for code in ("en", "sw", "lg"):
        cat = api.i18n.catalog(code)["strings"]
        for key in ("best.heading", "best.why1", "best.why", "best.see_all", "prio.C", "prio.W", "prio.H", "prio.K", "prio.F",
                    "fold.priorities", "prio.hint", "how.heading", "more.show", "more.hide", "jump.best", "jump.all", "rot.detail"):
            assert key in cat and cat[key], (code, key)

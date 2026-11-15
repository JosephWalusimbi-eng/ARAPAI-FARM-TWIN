"""Translation catalogs and language-aware engine output. Offline; synthetic climate."""
from __future__ import annotations

import datetime as dt
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from farm_twin import engine, i18n  # noqa: E402
from farm_twin.crops import CROPS  # noqa: E402
from test_engine import TODAY, profile  # noqa: E402

SRC = i18n.catalog("en")["strings"]


@pytest.mark.parametrize("code", [c for c in i18n.codes() if c != "en"])
def test_catalog_keys_and_placeholders_match_english(code):
    cat = json.loads((i18n.LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))
    assert {"code", "name", "native_name", "status"} <= set(cat["_meta"])
    for key, text in cat["strings"].items():
        assert key in SRC, f"{code}: unknown key {key}"
        if text.strip():
            assert i18n.placeholders(text) == i18n.placeholders(SRC[key]), f"{code}: {key}"


@pytest.mark.parametrize("code", ["lg", "sw"])
def test_drafted_languages_cover_all_farmer_text(code):
    strings = i18n.catalog(code)["strings"]
    raw = json.loads((i18n.LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]
    missing = [k for k in i18n.translatable_keys() if not raw.get(k, "").strip()]
    assert not missing, missing
    assert strings["crop.maize"] != SRC["crop.maize"]


def test_every_engine_message_key_exists_in_english():
    needed = {f"crop.{k}" for k in CROPS} | {f"family.{c.family}" for c in CROPS.values()}
    needed |= {f"scenario.{s}" for s in engine.SCENARIOS + ("all",)}
    needed |= {"disease.sclerotinia", "season.A.short", "season.B.short"}
    assert needed <= set(SRC)


def test_technical_text_is_not_asked_for():
    keys = i18n.translatable_keys()
    assert "sources.heading" not in keys and "crosscheck.item" not in keys
    assert "rot.heading" in keys


def test_missing_translation_falls_back_to_english():
    # Ateso is reviewed but a few rows have no translation yet: they show the English text
    en, teo_cat = i18n.catalog("en")["strings"], i18n.catalog("teo")["strings"]
    missing = [k for k in i18n.translatable_keys() if teo_cat.get(k, "") in ("", en[k])]
    assert missing and i18n.t("teo", missing[0]) == en[missing[0]]
    teo = next(l for l in i18n.languages() if l["code"] == "teo")
    assert teo["status"] == "reviewed" and 0.5 < teo["coverage"] < 1
    with pytest.raises(i18n.UnknownLanguage):
        i18n.catalog("xx")
    with pytest.raises(i18n.UnknownLanguage):
        i18n.catalog("../en")


def test_engine_output_in_luganda_and_swahili():
    for code, maize, word in (("lg", "Kasooli", "enkola"), ("sw", "Mahindi", "mpangilio")):
        res = engine.rank_rotations(profile(), today=TODAY, lang=code)
        rows = {r["label"]: r for r in res.rows}
        mono = rows["Maize → Maize → Maize"]           # English label stays the stable id
        assert mono["display"] == f"{maize} → {maize} → {maize}"
        assert word in mono["largest_penalty"].lower()
        assert "{" not in mono["largest_penalty"]
        assert res.meta["confidence"]["level_text"] != "Low"


def test_english_messages_read_naturally():
    res = engine.rank_rotations(profile(a={"summary": {**profile()["seasons"]["A"]["summary"],
                                                       "mean_longest_dry_spell_days": 15}}),
                                today=TODAY)
    texts = [i["text"] for r in res.rows for i in r["issues"]]
    assert any("days without rain is too long for beans" in t for t in texts)
    assert not any("'s" in t and "beans's" in t for t in texts)


def test_translation_spreadsheet_round_trip(tmp_path, monkeypatch):
    import csv
    import translations

    locales = tmp_path / "locales"
    locales.mkdir()
    for code in ("en", "teo"):
        (locales / f"{code}.json").write_text(
            (i18n.LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", locales)
    monkeypatch.setattr(translations, "SHEETS", tmp_path / "sheets")
    i18n.reload()
    try:
        path = translations.export("teo")
        rows = list(csv.DictReader(path.open(encoding="utf-8-sig")))
        assert {r["key"] for r in rows} == set(i18n.translatable_keys())
        for r in rows:  # start from an empty sheet: only a test word, plus one row that drops its placeholder
            r["translation"] = ""
            if r["key"] == "crop.maize":
                r["translation"] = "TEST-MAIZE"
            if r["key"] == "issue.nitrogen":
                r["translation"] = "no placeholder"
        with path.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=translations.COLUMNS)
            w.writeheader()
            w.writerows(rows)
        n, problems = translations.import_("teo")
        assert n == 1 and len(problems) == 1 and "issue.nitrogen" in problems[0]
        assert i18n.t("teo", "crop.maize") == "TEST-MAIZE"
        assert i18n.catalog("teo")["meta"]["status"] == "reviewed"   # importing never changes the status
    finally:
        i18n.reload()


def test_brand_name_is_never_translated():
    """ARAPAI FARM TWIN is a brand: every language keeps it whole, and no partial form appears anywhere."""
    import re
    en = i18n.catalog("en")["strings"]
    brand_keys = [k for k, v in en.items() if "ARAPAI FARM TWIN" in v]
    assert brand_keys
    for code in ("lg", "sw", "teo"):
        strings = i18n.catalog(code)["strings"]
        for k in brand_keys:
            v = strings.get(k, "")
            assert not v or re.search(r"(?<![A-Za-z])ARAPAI FARM TWIN(?![A-Za-z])", v), (code, k, v)
        for k, v in strings.items():
            assert not re.search(r"ARAPAI|FARM TWIN|Farm Twin|Arapai", v.replace("ARAPAI FARM TWIN", "")), (code, k, v)


def test_each_language_records_its_reviewer_and_the_notice_shows_no_name():
    """The reviewer is kept in the language file, but the notice says only 'Reviewed'; marking a language reviewed needs no text edits."""
    from farm_twin import i18n
    names = {"lg": "Joshua Benjamin Ssentongo", "sw": "Juliana Akello", "teo": "Geresem Ogwapit"}
    for code, name in names.items():
        cat = i18n.catalog(code)
        assert cat["meta"]["reviewer"] == name, code
        assert cat["meta"]["status"] in ("draft", "reviewed")
        for key in ("lang.draft", "lang.reviewed"):
            assert "{reviewer}" not in cat["strings"][key] and name not in cat["strings"][key], (code, key)
    en = i18n.catalog("en")["strings"]
    assert "Sunbird AI" in en["lang.draft"] and "reviewing" in en["lang.draft"] and en["lang.reviewed"] == "Reviewed"


def test_approve_marks_a_language_reviewed_without_touching_its_text(tmp_path, monkeypatch):
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import translations
    from farm_twin import i18n
    locales = tmp_path / "locales"
    locales.mkdir()
    for code in ("en", "lg"):
        (locales / f"{code}.json").write_text((i18n.LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"), encoding="utf-8")
    lg_path = locales / "lg.json"
    lg_cat = json.loads(lg_path.read_text(encoding="utf-8"))
    lg_cat["_meta"]["status"] = "draft"          # start from a draft whatever the real status is today
    lg_path.write_text(json.dumps(lg_cat, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(i18n, "LOCALES_DIR", locales)
    i18n.reload()
    try:
        before = i18n.catalog("lg")["strings"]
        assert i18n.catalog("lg")["meta"]["status"] == "draft"
        assert translations.main(["approve", "lg"]) == 0
        i18n.reload()
        after = i18n.catalog("lg")
        assert after["meta"]["status"] == "reviewed" and after["meta"]["reviewer"] == "Joshua Benjamin Ssentongo"
        assert after["strings"] == before
    finally:
        monkeypatch.undo()
        i18n.reload()

"""Hand translations to a native speaker as a spreadsheet, and bring them back.

    python scripts/translations.py status
    python scripts/translations.py export teo            # -> translations/teo.csv (open in Excel)
    python scripts/translations.py import teo            # reads translations/teo.csv
    python scripts/translations.py import teo --reviewed # also marks the language as reviewed

The spreadsheet has one row per farmer-facing text: key, English, translation, notes.
Fill only the "translation" column. Leave {words in braces} exactly as they are: the app
puts numbers and crop names there. NASA, POWER, IMERG, SMAP, units and numbers stay as is.
Technical panels (data sources, cross-checks) are not in the sheet; they stay in English.
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from farm_twin import i18n  # noqa: E402
from farm_twin.storage import atomic_write_text  # noqa: E402

SHEETS = ROOT / "translations"
COLUMNS = ["key", "english", "translation", "notes"]

# Words that may have no local-language equivalent. Reviewers see a note on every row that uses
# one, and translations/glossary.csv explains each and asks how to handle it.
HARD_TERMS = {
    "NDVI": "NDVI: keep the letters NDVI; the sentence around it explains it",
    "ET0": "ET0: keep the letters ET0; the sentence explains it",
    "satellite": "satellite: use the local loan word if people say it, else 'a machine in space that takes pictures'",
    "rain gauge": "rain gauge: describe it if there is no word (a cup that catches rain)",
    "frost": "frost: if no word, 'night so cold that plants are damaged'",
    "slope": "slope: describe if needed (land that goes down in one direction)",
    "ridges": "ridges: describe if needed (raised lines of soil in a field)",
    "nitrogen": "nitrogen: keep the English word if there is none (plant food in the soil)",
    "legume": "legume: use 'beans, groundnuts, cowpea' if there is no one word",
    "soil moisture": "soil moisture: 'wetness of the soil'",
    "dry spell": "dry spell: 'days in a row without rain'",
    "digital twin": "digital twin: no local word; describe it (a computer copy of the farm)",
    "USSD": "USSD: keep as is (the *384*2026# phone menu)",
    "SMS": "SMS: keep as is, or the local word for text message",
    "MoMo": "MoMo: keep as is (MTN Mobile Money)",
}


def status() -> None:
    for lang in i18n.languages():
        print(f"{lang['code']:>4}  {lang['native_name']:<12} {lang['status']:<18} "
              f"{lang['coverage']:.0%} of farmer-facing text")


def export(code: str) -> Path:
    src = i18n.catalog(i18n.SOURCE)["strings"]
    have = json.loads((i18n.LOCALES_DIR / f"{code}.json").read_text(encoding="utf-8"))["strings"]
    SHEETS.mkdir(exist_ok=True)
    path = SHEETS / f"{code}.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as fh:  # utf-8-sig: Excel opens it right
        w = csv.writer(fh)
        w.writerow(COLUMNS)
        for key in i18n.translatable_keys():
            ph = i18n.placeholders(src[key])
            note = ("keep " + ", ".join("{" + p + "}" for p in sorted(ph))) if ph else ""
            found = [n for t, n in HARD_TERMS.items() if t.lower() in src[key].lower()]
            if found:
                note = "; ".join(filter(None, [note, "HARD WORD, see glossary.csv: " + " | ".join(found)]))
            w.writerow([key, src[key], have.get(key, ""), note])
    return path


GLOSSARY = [
    # term, plain explanation, how to handle, luganda, kiswahili (filled only where confident)
    ("satellite", "A machine in space that takes pictures and measurements of the Earth", "Use the loan word or describe it", "", "satelaiti"),
    ("NDVI", "A number for how green the land looks from space (0.1 bare, 0.6+ thick plants)", "Keep the letters NDVI; the sentence explains it", "", ""),
    ("ET0", "How much water sun and wind can dry up in a season from green grass with all the water it needs", "Keep the letters ET0; the sentence explains it", "", ""),
    ("rain gauge", "A cup or tube that catches rain so you can measure it", "Describe it if no word exists", "", "kipimo cha mvua"),
    ("rainfall estimate", "A best guess of how much rain fell, made from satellites or computer models", "Describe it", "", ""),
    ("frost", "A night so cold that plants are damaged. It does not happen at this farm", "Describe if no word", "", "barafu / baridi kali"),
    ("slope", "Land that goes down in one direction", "Use the local word for a hill side", "", "mteremko"),
    ("height above sea level", "How high the land is above the sea", "Use 'how high'", "", "kimo juu ya usawa wa bahari"),
    ("nitrogen", "Plant food in the soil. Beans, groundnuts and cowpea add it; maize uses it up", "Keep the English word if none", "", "naitrojeni"),
    ("legume", "A plant like beans, groundnut or cowpea that adds plant food to the soil", "List the crops if there is no one word", "", "jamii ya kunde"),
    ("crop rotation", "Growing different crops one after another on the same field", "Use the local phrase farmers use", "", "mzunguko wa mazao"),
    ("dry spell", "Days in a row without rain", "Use the local phrase", "", "kipindi cha ukame"),
    ("soil moisture", "How wet the soil is", "Use 'wetness of the soil'", "", "unyevu wa udongo"),
    ("digital twin", "A computer copy of the farm used to try things before real planting", "No local word: describe it", "", ""),
    ("confidence", "How sure the app is about its advice (Low, Medium, High)", "Use 'how sure we are'", "", "uhakika"),
    ("scenario (dry year, wet year, hot year)", "A 'what if': what if next year is dry", "Use 'if the year is ...'", "", ""),
    ("season A / season B", "The two rainy seasons: March-June and August-November", "Check which season names farmers really use", "", ""),
    ("maize", "", "Crop name: native speaker to confirm", "kasooli", "mahindi"),
    ("beans", "", "Crop name: native speaker to confirm", "", "maharagwe"),
    ("groundnut", "", "Crop name: native speaker to confirm", "binyeebwa", "karanga"),
    ("cowpea", "", "Crop name: native speaker to confirm. Machine translation got this wrong (tomatoes)", "", "kunde"),
    ("sorghum", "", "Crop name: native speaker to confirm", "", "mtama"),
    ("finger millet", "", "Crop name: native speaker to confirm", "obulo", "ulezi"),
    ("cassava", "", "Crop name: native speaker to confirm", "muwogo", "muhogo"),
    ("sunflower", "", "Crop name: native speaker to confirm", "", "alizeti"),
    ("USSD / SMS / MoMo", "Phone menu (*384*2026#), text message, MTN Mobile Money", "Keep as is", "", ""),
]


def glossary() -> Path:
    SHEETS.mkdir(exist_ok=True)
    path = SHEETS / "glossary.csv"
    cols = ["term", "plain_explanation", "how_to_handle", "luganda", "kiswahili", "ateso", "notes"]
    with path.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(cols)
        for term, what, how, lg, sw in GLOSSARY:
            w.writerow([term, what, how, lg, sw, "", "pre-filled words are suggestions only: the reviewer decides" if lg or sw else ""])
    return path


def import_(code: str, reviewed: bool = False) -> tuple[int, list[str]]:
    src = i18n.catalog(i18n.SOURCE)["strings"]
    path = SHEETS / f"{code}.csv"
    cat_path = i18n.LOCALES_DIR / f"{code}.json"
    cat = json.loads(cat_path.read_text(encoding="utf-8"))
    problems, n = [], 0
    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            key, text = row["key"].strip(), (row.get("translation") or "").strip()
            if not text:
                continue
            if key not in src:
                problems.append(f"{key}: not a known key, skipped")
                continue
            if i18n.placeholders(text) != i18n.placeholders(src[key]):
                problems.append(f"{key}: must keep exactly {sorted(i18n.placeholders(src[key]))}, skipped")
                continue
            cat["strings"][key] = text
            n += 1
    if n and cat["_meta"].get("status") == "needs_translation":
        cat["_meta"]["status"] = "draft"
    if reviewed:
        cat["_meta"]["status"] = "reviewed"
    atomic_write_text(cat_path, json.dumps(cat, ensure_ascii=False, indent=2) + "\n")
    i18n.reload()
    return n, problems


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    e = sub.add_parser("export"); e.add_argument("code")
    sub.add_parser("glossary")
    ap_ = sub.add_parser("approve", help="mark a language as reviewed by its reviewer (after they sign off)")
    ap_.add_argument("code")
    i = sub.add_parser("import"); i.add_argument("code"); i.add_argument("--reviewed", action="store_true")
    args = ap.parse_args(argv)
    if args.cmd == "status":
        status()
    elif args.cmd == "approve":
        cat_path = i18n.LOCALES_DIR / f"{args.code}.json"
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        if not cat["_meta"].get("reviewer"):
            print("This language has no reviewer name in its file; add _meta.reviewer first.")
            return 1
        cat["_meta"]["status"] = "reviewed"
        atomic_write_text(cat_path, json.dumps(cat, ensure_ascii=False, indent=2) + "\n")
        print(f"{args.code}: marked reviewed by {cat['_meta']['reviewer']}. The app now shows 'Reviewed'.")
    elif args.cmd == "glossary":
        print(f"Wrote {glossary()}. Reviewers: say how each word is said locally, or write 'no word'.")
    elif args.cmd == "export":
        print(f"Wrote {export(args.code)}. Fill the 'translation' column, save as CSV, then import.")
    else:
        n, problems = import_(args.code, args.reviewed)
        print(f"Imported {n} translations into locales/{args.code}.json")
        for p in problems:
            print("  !", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

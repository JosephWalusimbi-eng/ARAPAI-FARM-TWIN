"""Spot-check translations with Sunbird AI (Sunflower), within a small call budget.

    python scripts/sunbird_check.py                 # default budget: 25 API calls
    python scripts/sunbird_check.py --max-calls 10

Needs SUNBIRD_API_TOKEN in the git-ignored .env. For a SAMPLE of farmer-facing text it:
  - back-translates our Luganda / Kiswahili drafts into English (does the meaning survive?)
  - translates the English into Luganda (a second opinion to compare with our draft)
  - suggests Ateso candidates for native speakers to review (never written into the app)
Results go to translations/sunbird_review.csv. Every answer is cached in
translations/sunbird_cache.json, so a rerun after a rate limit or power cut continues where it
stopped and never pays twice for the same text. Machine output is a check, not a replacement
for a native speaker.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from farm_twin import config, engine, i18n  # noqa: E402,F401  (config loads .env)
from farm_twin.storage import atomic_write_text, read_json_safe  # noqa: E402

API_URL = "https://api.sunbird.ai/tasks/translate"  # token is only ever sent to this host
OUT = ROOT / "translations"
CACHE = OUT / "sunbird_cache.json"
ISO = {"en": "eng", "lg": "lug", "sw": "swa", "teo": "teo"}

# Sample: short labels plus rendered example sentences (placeholders filled, so the model
# sees real text). Crop names go as one list to save calls.
CROP_ORDER = ["maize", "sorghum", "finger_millet", "beans", "groundnut", "cowpea", "sunflower", "cassava"]
EXAMPLES = {
    "issue.water": engine._msg("issue.water", scenario="dry", crop="maize", pct=54, slot=2, season=["B", 2027]),
    "issue.nitrogen": engine._msg("issue.nitrogen", kg="85"),
    "issue.rotation": engine._msg("issue.rotation", slot=2,
                                  reasons=[engine._msg("reason.same_crop", crop="maize")]),
}
LABELS = ["rot.heading", "seasons.heading", "scenario.dry", "term.H", "confidence.step.agronomist"]
PLAN = [  # (language, mode, item) in priority order; the budget cuts from the end
    *[("lg", "back", k) for k in ["crops", *EXAMPLES, *LABELS[:3]]],
    *[("lg", "forward", k) for k in ["crops", "issue.water", "rot.heading", "scenario.dry"]],
    *[("sw", "back", k) for k in ["crops", "issue.water", "issue.nitrogen", "rot.heading"]],
    *[("teo", "forward", k) for k in ["crops", "rot.heading", "seasons.heading", "scenario.dry", "issue.water"]],
]


def text_for(lang: str, item: str) -> str:
    if item == "crops":
        return ", ".join(i18n.t(lang, f"crop.{c}") for c in CROP_ORDER)
    if item in EXAMPLES:
        return engine.render(lang, EXAMPLES[item])
    return i18n.t(lang, item)


class RateLimited(Exception):
    pass


def translate(client: httpx.Client, token: str, text: str, src: str, tgt: str) -> str:
    for attempt in range(3):
        r = client.post(API_URL, json={"source_language": src, "target_language": tgt, "text": text},
                        headers={"Authorization": f"Bearer {token}"})
        if r.status_code == 429:
            raise RateLimited(r.headers.get("retry-after") or "rate limit reached")
        if r.status_code in (502, 503) and attempt < 2:
            time.sleep(10 * (attempt + 1))
            continue
        if r.status_code in (401, 403):
            raise SystemExit("Sunbird refused the token (401/403). Check SUNBIRD_API_TOKEN in .env.")
        r.raise_for_status()
        body = r.json()
        out = (body.get("output") or {}).get("translated_text") or body.get("translated_text")
        if not out:
            raise RuntimeError(f"no translated_text in response: {list(body)}")
        return out.strip()
    raise RuntimeError(f"Sunbird unavailable (HTTP {r.status_code})")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--max-calls", type=int, default=25)
    ap.add_argument("--pause", type=float, default=2.0, help="seconds between calls")
    args = ap.parse_args(argv)
    token = os.environ.get("SUNBIRD_API_TOKEN", "").strip()
    if not token:
        print("Set SUNBIRD_API_TOKEN in .env first.")
        return 1
    OUT.mkdir(exist_ok=True)
    cache = read_json_safe(CACHE) or {}
    calls, stopped = 0, None
    rows = []
    with httpx.Client(timeout=httpx.Timeout(30, read=120)) as client:
        for lang, mode, item in PLAN:
            src, tgt = (lang, "en") if mode == "back" else ("en", lang)
            text = text_for(src, item)
            ck = f"{ISO[src]}>{ISO[tgt]}|{text}"
            if ck not in cache and stopped is None:
                if calls >= args.max_calls:
                    stopped = f"budget of {args.max_calls} calls used"
                else:
                    try:
                        if calls:
                            time.sleep(args.pause)
                        cache[ck] = translate(client, token, text, ISO[src], ISO[tgt])
                        calls += 1
                        atomic_write_text(CACHE, json.dumps(cache, ensure_ascii=False, indent=1))
                        print(f"  {lang} {mode:<7} {item}: ok")
                    except RateLimited as exc:
                        stopped = f"Sunbird rate limit ({exc})"
                    except (httpx.HTTPError, RuntimeError) as exc:
                        print(f"  {lang} {mode:<7} {item}: failed: {exc}")
            rows.append({"language": lang, "check": mode, "item": item,
                         "english": text_for("en", item),
                         "our_text": "" if lang == "teo" else text_for(lang, item),
                         "sunbird": cache.get(ck, "")})
    with (OUT / "sunbird_review.csv").open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    done = sum(1 for r in rows if r["sunbird"])
    print(f"{calls} API calls this run; {done}/{len(rows)} checks have a Sunbird answer.")
    if stopped:
        print(f"Stopped early: {stopped}. Rerun later to continue; finished checks are cached.")
    print(f"Review sheet: {OUT / 'sunbird_review.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

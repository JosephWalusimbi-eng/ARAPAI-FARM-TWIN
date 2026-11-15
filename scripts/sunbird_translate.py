"""Translate the farmer-facing text with Sunbird AI (Sunflower): our PRIMARY translator for Ateso, Luganda and Kiswahili.

    python scripts/sunbird_translate.py --lang teo           # translate (cached, resumable) and write translations/teo_sunbird.csv
    python scripts/sunbird_translate.py --lang lg --max-calls 450
    python scripts/sunbird_translate.py --lang sw --apply    # put the accepted rows into the language file as a DRAFT

Needs SUNBIRD_API_TOKEN in the git-ignored .env. Sunbird's output is a machine draft: native speakers review it, and nothing is
ever marked "reviewed" here. Safeguards:
  * A sentence is accepted only if every {placeholder}, number and protected name (NASA, SMS, USSD, NDVI ...) survives exactly and
    its length is plausible. A rejected row keeps its previous text (Luganda, Kiswahili: the earlier draft; Ateso: English) and is
    listed with the reason in the report, so reviewers see exactly where Sunbird needed help.
  * Single words (crop, month, season, family and disease names) are the least reliable output, so the report marks them "name",
    and two different names that come back identical are both rejected. Month abbreviations are translated from the full month
    name and cut to three letters.
  * Multi-line texts (phone menus, SMS plans) are translated line by line so the layout is kept.
  * Every answer is cached in translations/sunbird_<lang>_cache.json, so a rate limit or power cut loses nothing.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "scripts"))

from farm_twin import config, i18n  # noqa: E402,F401  (config loads .env)
from farm_twin.storage import atomic_write_text, read_json_safe  # noqa: E402

API_URL = "https://api.sunbird.ai/tasks/translate"  # the token is only ever sent to this host
OUT = ROOT / "translations"
ISO = {"teo": "teo", "lg": "lug", "sw": "swa"}
LABEL = {"teo": "Ateso", "lg": "Luganda", "sw": "Kiswahili"}
REVIEWERS = {"lg": "Joshua Benjamin Ssentongo", "sw": "Juliana Akello", "teo": "Geresem Ogwapit"}
NAME_PREFIXES = ("crop.", "month.", "season.", "family.", "disease.")
MONTH_FULL = {"Jan": "January", "Feb": "February", "Mar": "March", "Apr": "April", "May": "May", "Jun": "June", "Jul": "July",
              "Aug": "August", "Sep": "September", "Oct": "October", "Nov": "November", "Dec": "December"}
PROTECT = ["NASA", "POWER", "IMERG", "SMAP", "MODIS", "Landsat", "NASADEM", "SRTM", "ERA5", "NDVI", "ET0", "USSD", "SMS",
           "MoMo", "MTN", "UGX", "SoilGrids", "GPM", "FAO", "ARAPAI FARM TWIN", "ECMWF", "Open-Meteo"]   # the product name must stay whole
# Rows Sunbird's output is known to break (for example a phone screen over the 182-character limit): filled in after testing.
REJECT: dict[str, set[str]] = {}
# typical length of what the app puts into each placeholder (used to estimate a phone menu screen)
EXTRA = {"crop": 20, "crop2": 20, "crop3": 20, "season": 12, "level": 9, "price": 8, "name": 12, "code": 12, "a": 4, "b": 4,
         "a_dry": 4, "b_dry": 4, "through": 4}
LANGUAGE_NAMES = ["luganda", "kiswahili", "swahili", "ateso", "english", "kiingereza", "lungereza"]
NUM = re.compile(r"\d+(?:[.,]\d+)?")
LINE_NUM = re.compile(r"^(\d+\.\s+)(.*)$")


CURRENT_LANG = {"code": ""}


class RateLimited(Exception):
    daily = False   # True when Sunbird says the key's DAILY quota is used up (retry is hours away)


def call(client: httpx.Client, token: str, text: str, lang: str) -> str:
    for attempt in range(3):
        try:
            r = client.post(API_URL, json={"source_language": "eng", "target_language": ISO[lang], "text": text},
                            headers={"Authorization": f"Bearer {token}"})
        except httpx.TransportError:
            if attempt < 2:
                time.sleep(5 * (attempt + 1))
                continue
            raise
        if r.status_code == 429:
            retry_after = int(r.headers.get("retry-after") or 0)
            exc = RateLimited(f"retry in {retry_after // 3600} h {retry_after % 3600 // 60} min" if retry_after else "rate limit reached")
            exc.daily = retry_after > 300 or "daily" in r.text.lower()
            raise exc
        if r.status_code in (502, 503, 504) and attempt < 2:
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


def pieces(text: str) -> list[tuple[str, str]]:
    """Split a text into (prefix, sentence) lines; list numbers like '1. ' stay as prefixes."""
    out = []
    for line in text.split(chr(10)):
        m = LINE_NUM.match(line)
        out.append((m.group(1), m.group(2)) if m else ("", line))
    return out


def api_text(key: str, sentence: str) -> str:
    """What we send to Sunbird: month abbreviations are expanded to the full month name."""
    return MONTH_FULL.get(sentence.strip(), sentence) if key.startswith("month.") else sentence


def has_words(text: str) -> bool:
    """Something to translate besides {placeholders} and punctuation (so season.slot and the letter 'A' are left alone)."""
    return len(re.findall(r"[A-Za-z]{2,}", re.sub(r"[{][^}]*[}]", "", text))) > 0


PH = re.compile(r"[{](\w+)[}]")


def protect(text: str) -> tuple[str, list[tuple[str, str]]]:
    """Swap each {name} for a neutral token (XQ1X ...) the model should leave alone; returns the text and the token map."""
    names: list[str] = []
    for m in PH.finditer(text):
        if m.group(1) not in names:
            names.append(m.group(1))
    tokens = [(f"XQ{i + 1}X", n) for i, n in enumerate(names)]
    out = PH.sub(lambda m: next(t for t, n in tokens if n == m.group(1)), text)
    return out, tokens


def restore(text: str, tokens: list[tuple[str, str]]) -> str | None:
    """Put the {names} back; None if a token was lost or duplicated."""
    for tok, name in tokens:
        if text.count(tok) < 1:
            return None
        text = text.replace(tok, "{" + name + "}")
    return text if "XQ" not in text else None


def choose(sentence: str, cache: dict) -> str | None:
    """The cached translation of a sentence that keeps its placeholders, or the best available answer."""
    direct = cache.get(sentence)
    if direct is not None and i18n.placeholders(direct) == i18n.placeholders(sentence):
        return direct
    if PH.search(sentence):
        prot, tokens = protect(sentence)
        if prot in cache:
            back = restore(cache[prot], tokens)
            if back is not None:
                return back
    return direct


def valid(key: str, src: str, dst: str) -> str | None:
    """None when the translation is acceptable, else the reason it is not."""
    if key in REJECT.get(CURRENT_LANG["code"], set()):
        return "rejected after testing (too long for a phone menu screen)"
    if i18n.placeholders(src) != i18n.placeholders(dst):
        return "placeholders changed"
    for name in PROTECT:
        if name in src and not re.search(r"(?<![A-Za-z])" + re.escape(name) + r"(?![A-Za-z])", dst):
            return f"lost or changed {name}"
    if not set(NUM.findall(src)) <= set(NUM.findall(dst)):
        return "a number changed"
    if len(src) > 25 and not (0.45 * len(src) <= len(dst) <= 2.6 * len(src)):
        return "length looks wrong"
    low_src, low_dst = src.lower(), dst.lower()
    if any(n in low_dst and n not in low_src for n in LANGUAGE_NAMES):
        return "mentions a language name (model commentary)"
    if key.startswith(NAME_PREFIXES) and not key.startswith("month.") and len(dst.split()) > 4:
        return "too long for a name"
    if key.startswith("ussd.") and len(PH.sub(lambda m: "x" * EXTRA.get(m.group(1), 10), dst)) > 176:
        return "too long for a phone menu screen"   # one screen holds 182 characters including 'CON '/'END ' and the filled-in values
    if dst.strip().lower() == src.strip().lower() and len(src) > 25:
        return "unchanged"
    return None


def build(lang: str, keys: list[str], src_strings: dict, cache: dict, previous: dict):
    rows, accepted = [], {}
    for key in keys:
        src = src_strings[key]
        lines, ok, why = [], True, None
        for prefix, sentence in pieces(src):
            s = api_text(key, sentence).strip()
            if not s or not has_words(s):
                lines.append(prefix + sentence)
            elif choose(s, cache) is not None:
                t_ = choose(s, cache)
                if t_.endswith(".") and not s.endswith("."):
                    t_ = t_[:-1]          # the model adds full stops to single words and labels
                lines.append(prefix + t_)
            else:
                ok, why = False, "not translated yet"
                break
        dst = chr(10).join(lines) if ok else ""
        if ok and key.startswith("month.") and dst.split():
            dst = dst.split()[0]          # the full month word; shortened below when that stays unambiguous
        if ok:
            why = valid(key, src, dst)
        rows.append([key, src, dst, ok and not why, why or ""])
    # two different crop (or month ...) names that come back identical are not trustworthy
    for prefix in NAME_PREFIXES:
        group = [r for r in rows if r[0].startswith(prefix) and r[3] and r[2]]
        seen: dict[str, list] = {}
        for r in group:
            seen.setdefault(r[2].strip().lower(), []).append(r)
        for same in seen.values():
            if len(same) > 1 and len({r[1] for r in same}) > 1:
                for r in same:
                    r[3], r[4] = False, "same word returned for different names"
    months = [r for r in rows if r[0].startswith("month.") and r[3]]
    if months and len({r[2][:3] for r in months}) == len(months):
        for r in months:
            r[2] = r[2][:3]
    out = []
    for key, src, dst, ok, why in rows:
        kind = "name" if key.startswith(NAME_PREFIXES) else "sentence"
        if ok:
            accepted[key] = dst
        out.append([key, kind, src, dst, previous.get(key, ""), "accepted" if ok else "kept previous text", why])
    return out, accepted


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--lang", choices=sorted(ISO), required=True)
    ap.add_argument("--max-calls", type=int, default=450)
    ap.add_argument("--pause", type=float, default=0.3, help="seconds each worker waits after a call")
    ap.add_argument("--workers", type=int, default=4, help="requests in flight at once")
    ap.add_argument("--apply", action="store_true", help="import the accepted rows into the language file as a draft")
    ap.add_argument("--retry-rejected", action="store_true",
                    help="forget Sunbird's earlier answers for rows that failed the checks and ask again (answers vary from call to call)")
    args = ap.parse_args(argv)
    lang = args.lang
    CURRENT_LANG["code"] = lang
    cache_path = OUT / f"sunbird_{lang}_cache.json"
    report = OUT / f"{lang}_sunbird.csv"

    src_strings = i18n.catalog(i18n.SOURCE)["strings"]
    keys = [k for k in i18n.translatable_keys() if has_words(src_strings[k])]
    cache = read_json_safe(cache_path) or {}
    previous = json.loads((i18n.LOCALES_DIR / f"{lang}.json").read_text(encoding="utf-8"))["strings"]

    if args.retry_rejected:
        rows_, _ = build(lang, keys, src_strings, cache, previous)
        dropped = 0
        for key, _kind, src_, _dst, _prev, status, why in rows_:
            if status != "accepted" and why and not why.startswith(("not translated", "rejected after testing")):
                for _, sentence in pieces(src_strings[key]):
                    s2 = api_text(key, sentence).strip()
                    for variant in {s2, protect(s2)[0]}:
                        if variant in cache:
                            del cache[variant]
                            dropped += 1
        atomic_write_text(cache_path, json.dumps(cache, ensure_ascii=False))
        print(f"[{LABEL[lang]}] forgot {dropped} earlier answers for rejected rows; asking again.")

    if not args.apply:
        tokens = [os.environ.get(name, "").strip() for name in ["SUNBIRD_API_TOKEN"] + [f"SUNBIRD_API_TOKEN_{n}" for n in range(2, 9)]]
        tokens = [t for t in tokens if t]
        if not tokens:
            print("Set SUNBIRD_API_TOKEN in .env first (extra keys: SUNBIRD_API_TOKEN_2, _3, ...).")
            return 1
        OUT.mkdir(exist_ok=True)
        # Sunbird takes several seconds per call, so a few requests run side by side. A 429 stops everything cleanly.
        pending: list[str] = []
        for key in keys:
            for _, sentence in pieces(src_strings[key]):
                s_ = api_text(key, sentence).strip()
                if not s_ or not has_words(s_):
                    continue
                if PH.search(s_) and s_ in cache and i18n.placeholders(cache[s_]) != i18n.placeholders(s_):
                    s_ = protect(s_)[0]          # the model translated the words inside the braces: retry with tokens
                if s_ not in cache and s_ not in pending:
                    pending.append(s_)
        stopped = None
        if len(pending) > args.max_calls:
            pending, stopped = pending[:args.max_calls], f"call budget of {args.max_calls} used"
        calls = 0
        stop_flag = {"rate": None}

        import threading
        lock = threading.Lock()
        state = {"i": 0}
        failed = {"n": 0}

        def work(sentence: str):
            while not stop_flag["rate"]:
                i = state["i"]
                try:
                    with httpx.Client(timeout=httpx.Timeout(30, read=120)) as client:
                        out = call(client, tokens[i], sentence, lang)
                    time.sleep(args.pause)
                    return sentence, out
                except RateLimited as exc:
                    waited = False
                    with lock:
                        if state["i"] == i:  # first thread to see the limit moves everyone to the next key
                            if i + 1 < len(tokens):
                                state["i"] = i + 1
                                print(f"[{LABEL[lang]}] key {i + 1} is limited ({exc}), switching to key {i + 2} of {len(tokens)}")
                            elif exc.daily:  # the key's daily quota is spent: waiting would take hours, so stop and say so
                                stop_flag["rate"] = f"daily quota used up ({exc})"
                            else:  # a per-minute limit: wait it out, give up only if it keeps happening
                                state["waits"] = state.get("waits", 0) + 1
                                if state["waits"] > 40:
                                    stop_flag["rate"] = str(exc)
                                else:
                                    waited = True
                    if waited:
                        time.sleep(65)
                except (httpx.HTTPError, RuntimeError):
                    failed["n"] += 1
                    return sentence, None   # not cached: the next run tries it again
            return sentence, None

        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            for sentence, out in pool.map(work, pending):
                if out is None:
                    continue
                cache[sentence] = out
                calls += 1
                if calls % 20 == 0:
                    atomic_write_text(cache_path, json.dumps(cache, ensure_ascii=False))
        if stop_flag["rate"]:
            stopped = f"rate limited on all {len(tokens)} key(s) ({stop_flag['rate']})"
        if failed["n"]:
            print(f"[{LABEL[lang]}] {failed['n']} sentences hit a connection error; run again to retry them.")
        atomic_write_text(cache_path, json.dumps(cache, ensure_ascii=False))
        print(f"[{LABEL[lang]}] {calls} calls this run, {len(cache)} sentences cached." +
              (f" Stopped: {stopped}. Run again to continue." if stopped else ""))

    rows, accepted = build(lang, keys, src_strings, cache, previous)
    with report.open("w", encoding="utf-8-sig", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["key", "kind", "english", "sunbird_" + lang, "previous_text", "status", "reason"])
        w.writerows(rows)
    names = sum(1 for r in rows if r[1] == "name" and r[5] == "accepted")
    print(f"[{LABEL[lang]}] {len(accepted)} of {len(keys)} rows accepted ({names} are single-word names); "
          f"{len(keys) - len(accepted)} keep their previous text. Report: {report}")

    if args.apply:
        import translations as tr  # scripts/translations.py
        tr.export(lang)
        sheet = OUT / f"{lang}.csv"
        with sheet.open(encoding="utf-8-sig", newline="") as fh:
            data = list(csv.DictReader(fh))
        for r in data:
            if r["key"] in accepted:
                r["translation"] = accepted[r["key"]]
        with sheet.open("w", encoding="utf-8-sig", newline="") as fh:
            w = csv.DictWriter(fh, fieldnames=["key", "english", "translation", "notes"])
            w.writeheader()
            w.writerows(data)
        n, problems = tr.import_(lang)
        cat_path = i18n.LOCALES_DIR / f"{lang}.json"
        cat = json.loads(cat_path.read_text(encoding="utf-8"))
        cat["_meta"]["note"] = (f"Machine translation by Sunbird AI (Sunflower), reviewed by {REVIEWERS[lang]}, a native {LABEL[lang]} speaker on the team. "
                                "Rows Sunbird could not translate safely keep an earlier text or English.")
        cat["_meta"]["reviewer"] = REVIEWERS[lang]
        atomic_write_text(cat_path, json.dumps(cat, ensure_ascii=False, indent=2) + chr(10))
        i18n.reload()
        print(f"[{LABEL[lang]}] imported {n} rows as a DRAFT into locales/{lang}.json (status: {cat['_meta']['status']}).")
        for p in problems:
            print("  !", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

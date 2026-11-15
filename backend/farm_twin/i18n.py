"""Translations for farmer-facing text (English, Luganda, Kiswahili, Ateso, ...).

Not everything is translated: NASA, dataset names, units and numbers stay as they are, and
the technical panels listed in en.json "no_translation_needed" may stay in English.

Catalogs live in farm_twin/locales/<code>.json:
    {"_meta": {"code", "name", "native_name", "status", "note"}, "strings": {key: text}}
English (en.json) is the source: every key exists there. Other catalogs may be partial;
a missing key falls back to English, so a half-translated language still works.
status: "source" | "reviewed" (checked by a native speaker) | "draft" | "needs_translation".

To add a language: copy the _meta block into locales/<code>.json with an empty "strings",
then use scripts/translations.py to export a spreadsheet for a translator and import it back.
"""
from __future__ import annotations

import json
import re
import string
from functools import lru_cache
from pathlib import Path

LOCALES_DIR = Path(__file__).resolve().parent / "locales"
SOURCE = "en"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


class UnknownLanguage(KeyError):
    pass


@lru_cache(maxsize=None)
def _load(code: str) -> dict:
    path = LOCALES_DIR / f"{code}.json"
    if not re.fullmatch(r"[a-z]{2,3}", code) or not path.exists():
        raise UnknownLanguage(code)
    return json.loads(path.read_text(encoding="utf-8"))


def reload() -> None:
    """Forget cached catalogs (after a translation import)."""
    _load.cache_clear()
    catalog.cache_clear()


def codes() -> list[str]:
    return sorted(p.stem for p in LOCALES_DIR.glob("*.json"))


def needs_translation(key: str) -> bool:
    """False for technical text (data-source panels, formatting) that may stay in English."""
    skip = _load(SOURCE)["_meta"].get("no_translation_needed", [])
    return not any(key.startswith(prefix) for prefix in skip)


def translatable_keys() -> list[str]:
    return [k for k in _load(SOURCE)["strings"] if needs_translation(k)]


def languages() -> list[dict]:
    """Every catalog with its status and how much of the farmer-facing text it covers."""
    keys = translatable_keys()
    out = []
    for code in codes():
        strings = _load(code)["strings"]
        done = sum(1 for k in keys if strings.get(k, "").strip())
        meta = {k: v for k, v in _load(code)["_meta"].items()
                if k not in ("no_translation_needed", "keep_as_is")}
        out.append({**meta, "code": code, "coverage": round(done / len(keys), 3)})
    order = {SOURCE: 0}
    return sorted(out, key=lambda m: (order.get(m["code"], 1), m["name"]))


@lru_cache(maxsize=None)
def catalog(code: str) -> dict:
    """Merged strings for a language (English fills any gaps) plus its _meta. Do not mutate."""
    src = _load(SOURCE)
    cat = _load(code)
    strings = dict(src["strings"])
    strings.update({k: v for k, v in cat["strings"].items() if k in strings and v.strip()})
    return {"meta": cat["_meta"], "strings": strings}


class _Keep(dict):
    def __missing__(self, key):  # leave unknown placeholders visible rather than crash
        return "{" + key + "}"


def t(code: str, key: str, /, **params) -> str:
    # positional-only, so a placeholder may itself be called {code} or {key}
    strings = catalog(code)["strings"]
    text = strings.get(key, key)
    return string.Formatter().vformat(text, (), _Keep(params))


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text))

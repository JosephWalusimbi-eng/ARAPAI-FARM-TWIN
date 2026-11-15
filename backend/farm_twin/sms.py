"""SMS version of a rotation plan, for farmers with basic phones.

In the full product a licensed SMS aggregator sends these (USSD and SMS
licensing); this module only builds the text and splits it into SMS segments.
frontend/sms.js mirrors it for the offline preview; tests/test_sms.py checks they match.

Segment rules (GSM 03.38): text using only the GSM-7 alphabet fits 160 characters in one
SMS, or 153 per part when split; anything else is sent as UCS-2: 70, or 67 per part.
Characters outside GSM-7 that commonly appear in our text are replaced first, so one
'°' does not more than double the cost of a message.
"""
from __future__ import annotations

import re

from . import config, i18n

GSM_BASIC = ("@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?¡"
             "ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà")
GSM_EXTENDED = "^{}\\[~]|€\f"  # each costs 2 septets
REPLACE = [("°C", "C"), ("°", ""), ("→", "-"), ("·", "-"), ("’", "'"), ("‘", "'"), ("“", '"'),
           ("”", '"'), ("–", "-"), ("—", "-"), ("³", "3"), ("²", "2"), ("…", "...")]
LIMITS = {"GSM-7": (160, 153), "UCS-2": (70, 67)}


def sanitize(text: str) -> str:
    for a, b in REPLACE:
        text = text.replace(a, b)
    return text


def encoding(text: str) -> str:
    return "GSM-7" if all(c in GSM_BASIC or c in GSM_EXTENDED for c in text) else "UCS-2"


def cost(text: str, enc: str) -> int:
    if enc == "GSM-7":
        return sum(2 if c in GSM_EXTENDED else 1 for c in text)
    return len(text.encode("utf-16-le")) // 2


def segment(text: str) -> dict:
    """Split into SMS parts on word boundaries (hard-splitting only over-long words)."""
    enc = encoding(text)
    single, multi = LIMITS[enc]
    total = cost(text, enc)
    if total <= single:
        return {"encoding": enc, "chars": total, "segments": [text]}
    segments, cur, cur_cost = [], "", 0
    for word in text.split(" "):
        wc = cost(word, enc)
        add = wc if cur == "" else wc + 1
        if cur != "" and cur_cost + add > multi:
            segments.append(cur)
            cur, cur_cost, add = "", 0, wc
        if wc > multi:
            for ch in word:
                cc = cost(ch, enc)
                if cur_cost + cc > multi:
                    segments.append(cur)
                    cur, cur_cost = "", 0
                cur += ch
                cur_cost += cc
        else:
            cur = word if cur == "" else cur + " " + word
            cur_cost += add
    if cur != "":
        segments.append(cur)
    return {"encoding": enc, "chars": total, "segments": segments}


def season_label(lang: str, season: str, year: int) -> str:
    """'Mar-Jun 2027': the months of the season and the year, which a farmer knows at once."""
    months = config.SEASONS[season]["months"]
    t = lambda key: i18n.t(lang, key)  # noqa: E731
    return f"{t(f'month.{months[0]}')}-{t(f'month.{months[-1]}')} {year}"


def month_year(lang: str, iso_date: str) -> str:
    """'Dec 2025' from '2025-12-31'."""
    year, month = iso_date[:4], int(iso_date[5:7])
    return f"{i18n.t(lang, f'month.{month}')} {year}"


_CONCERN_KEYS = {"issue.water": "sms.c.water", "issue.dry_spell": "sms.c.dry_spell", "issue.heat": "sms.c.heat",
                 "issue.waterlog": "sms.c.waterlog", "issue.nitrogen": "sms.c.nitrogen",
                 "issue.rotation": "sms.c.rotation"}


def concern_text(row: dict, lang: str) -> str | None:
    """The biggest worry about a plan, in the simple words an SMS needs."""
    issues = row.get("issues") or []
    if not issues:
        return None
    first = issues[0]
    key = _CONCERN_KEYS.get(first.get("code"))
    if key is None:
        return i18n.t(lang, "sms.concern", concern=first["text"]) if first.get("text") else None
    p = first.get("params") or {}
    season = p.get("season")
    scenario = i18n.t(lang, f"scenario.{p['scenario']}") if p.get("scenario") else ""
    return i18n.t(lang, key, scenario=re.sub(r"\s*\(.*?\)", "", scenario),
                  crop=i18n.t(lang, f"crop.{p['crop']}") if p.get("crop") else "",
                  pct=p.get("pct", ""), days=p.get("days", ""),
                  when=season_label(lang, season[0], season[1]) if season else "")


def plan_text(row: dict, meta: dict, farm: dict, lang: str, data_through: str) -> str:
    """One rotation (a ranked row from engine.rank_rotations) as SMS text in a language."""
    t = lambda key, **p: i18n.t(lang, key, **p)  # noqa: E731
    slots = [t("sms.slot", season=season_label(lang, slot["season"], slot["year"]), crop=t(f"crop.{crop}"))
             for crop, slot in zip(row["rotation"], meta["slots"])]
    previous = meta.get("previous_crop")
    lines = [t("sms.plan", previous=t(f"crop.{previous}").lower() if previous else "-",
               plan="\n".join(slots), n=meta["rotations_feasible"])]
    worry = concern_text(row, lang)
    if worry:
        lines.append(worry)
    lines.append(t("sms.footer", level=meta["confidence"]["level_text"], through=month_year(lang, data_through)))
    return sanitize("\n".join(lines))


def plan_sms(row: dict, meta: dict, farm: dict, lang: str, data_through: str) -> dict:
    text = plan_text(row, meta, farm, lang, data_through)
    return {"text": text, "rotation": row["rotation"], "lang": lang, **segment(text)}

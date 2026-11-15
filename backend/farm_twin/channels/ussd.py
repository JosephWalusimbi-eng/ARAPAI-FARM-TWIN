"""USSD menus in the Africa's Talking callback format.

Africa's Talking POSTs sessionId, serviceCode, phoneNumber and text, where text holds every
choice made so far in the session joined by '*' ('' on the first screen, '2*1' after choosing 2
then 1). We reply with 'CON <screen>' to wait for another choice or 'END <screen>' to close.
Screens stay within MAX_CHARS so every phone can show them.

Menu: 1 next-season advice (free) · 2 full 3-season plan by SMS (paid, MTN MoMo) · 3 rain
· 4 soil · 5 language · 6 help. Text comes from the language catalogs, in the phone's language.
"""
from __future__ import annotations

from .. import config, i18n, sms
from . import advice, momo, orders, store

MAX_CHARS = 182
LANG_CHOICES = ["en", "lg", "sw", "teo"]
LANG_NAMES = {"en": "English", "lg": "Luganda", "sw": "Kiswahili", "teo": "Ateso"}


def con(text: str) -> str:
    return "CON " + text


def end(text: str) -> str:
    return "END " + text


def _slot(lang, slot):
    return sms.season_label(lang, slot["season"], slot["year"])


def screen_main(lang):
    return con(i18n.t(lang, "ussd.main", price=orders.price_text()))


def screen_next(lang):
    res, _, _ = advice.advice(lang)
    top, slots = res.rows[0], res.meta["slots"]
    names = [i18n.t(lang, f"crop.{c}") for c in top["rotation"]]
    return end(i18n.t(lang, "ussd.next", season=_slot(lang, slots[0]), crop=names[0], crop2=names[1].lower(),
                      crop3=names[2].lower(), level=res.meta["confidence"]["level_text"]))


def screen_rain(lang):
    _, profile, _ = advice.advice(lang)
    a, b = profile["seasons"]["A"]["summary"], profile["seasons"]["B"]["summary"]
    r = lambda v: f"{v:.0f}"  # noqa: E731
    return end(i18n.t(lang, "ussd.rain", a=r(a["mean_rain_mm"]), a_dry=r(a["p20_rain_mm"]),
                      b=r(b["mean_rain_mm"]), b_dry=r(b["p20_rain_mm"]), through=profile["period"]["end"][:4]))


def screen_soil(lang):
    _, _, farm = advice.advice(lang)
    sp = farm.get("soil_profile") or {}
    if not sp.get("details"):
        return end(i18n.t(lang, "ussd.soil_none"))
    oc = sp.get("organic_carbon")
    return end(i18n.t(lang, "ussd.soil", texture=sp.get("soil_type") or "-",
                      ph=f"{sp['ph']:.1f}" if sp.get("ph") is not None else "-",
                      oc=f"{oc:.0f}" if oc is not None else "-"))


def screen_languages():
    return con("\n".join(f"{i}. {LANG_NAMES[c]}" for i, c in enumerate(LANG_CHOICES, 1)))


def handle(session_id: str, service_code: str, phone: str, text: str) -> str:
    """One USSD request -> 'CON ...' or 'END ...'."""
    profile = store.phone_profile(phone)
    lang = profile["lang"] if profile["lang"] in i18n.codes() else "en"
    t = lambda key, **p: i18n.t(lang, key, **p)  # noqa: E731
    parts = [p.strip() for p in (text or "").split("*")] if text else []
    if not parts:
        return screen_main(lang)
    first, rest = parts[0], parts[1:]
    try:
        if first == "1" and not rest:
            return screen_next(lang)
        if first == "2":
            if not rest:
                return con(t("ussd.pay_confirm", price=orders.price_text()))
            if rest == ["1"]:
                try:
                    orders.start(phone, lang)
                except momo.PaymentError as exc:
                    return end(t("ussd.pay_failed", reason=str(exc)[:60]))
                return end(t("ussd.pay_started", price=orders.price_text()))
            if rest == ["2"]:
                return end(t("ussd.cancelled"))
        if first == "3" and not rest:
            return screen_rain(lang)
        if first == "4" and not rest:
            return screen_soil(lang)
        if first == "5":
            if not rest:
                return screen_languages()
            if len(rest) == 1 and rest[0].isdigit() and 1 <= int(rest[0]) <= len(LANG_CHOICES):
                new = LANG_CHOICES[int(rest[0]) - 1]
                store.set_phone_lang(phone, new)
                return end(i18n.t(new, "ussd.lang_set", name=LANG_NAMES[new]))
        if first == "6" and not rest:
            return end(t("ussd.help", code=config.USSD_CODE))
    except Exception:  # a failed lookup must still close the session politely
        return end(t("ussd.error"))
    return end(t("ussd.invalid"))


def handle_sms(phone: str, text: str) -> str | None:
    """Inbound SMS keywords: HELP, PLAN (paid plan via MoMo), LANG EN|LG|SW|TEO.
    Returns the reply text (sent by the caller), or None when no reply is needed."""
    profile = store.phone_profile(phone)
    lang = profile["lang"]
    words = (text or "").strip().upper().split()
    if not words:
        return None
    if words[0] == "LANG" and len(words) > 1 and words[1].lower() in LANG_CHOICES:
        new = words[1].lower()
        store.set_phone_lang(phone, new)
        return i18n.t(new, "ussd.lang_set", name=LANG_NAMES[new])
    if words[0] == "PLAN":
        try:
            orders.start(phone, lang)
        except momo.PaymentError as exc:
            return i18n.t(lang, "ussd.pay_failed", reason=str(exc)[:60])
        return i18n.t(lang, "ussd.pay_started", price=orders.price_text())
    return i18n.t(lang, "sms.help", code=config.USSD_CODE)

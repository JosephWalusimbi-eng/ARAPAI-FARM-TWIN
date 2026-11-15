"""Paid services: the full 3-season plan by SMS, paid with MTN MoMo.

Flow: start() asks MoMo to request payment from the farmer's phone and records an order.
check() asks MoMo for the payment status; when it is SUCCESSFUL the plan is sent by SMS
(exactly once), and when it fails the farmer gets an SMS saying no money was taken.
check() is called by the simulator after the farmer decides, by GET /api/payments/<ref>,
and (in sandbox mode) by a background poller.
"""
from __future__ import annotations

import datetime as dt
import threading
import time
import uuid

from .. import config, i18n
from . import advice, momo, sms_gateway, store


def price_text() -> str:
    return f"{config.PLAN_PRICE_CURRENCY} {config.PLAN_PRICE_AMOUNT}"


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def start(phone: str, lang: str, provider=None) -> dict:
    provider = provider or momo.get_provider()
    phone = store.normalize_phone(phone)
    external_id = uuid.uuid4().hex[:16]
    ref = provider.request_to_pay(store.msisdn(phone), config.PLAN_PRICE_AMOUNT, config.PLAN_PRICE_CURRENCY,
                                  external_id, i18n.t(lang, "momo.payer_message", price=price_text()),
                                  "ARAPAI FARM TWIN 3-season plan by SMS")
    order = {"ref": ref, "phone": phone, "lang": lang, "product": "plan_sms", "provider": provider.name,
             "amount": config.PLAN_PRICE_AMOUNT, "currency": config.PLAN_PRICE_CURRENCY,
             "external_id": external_id, "status": "PENDING", "created_at": _now()}
    store.update("orders", {}, lambda d: d.__setitem__(ref, order))
    if provider.name == "sandbox":
        threading.Thread(target=poll_until_final, args=(ref,), daemon=True).start()
    return order


def check(ref: str, provider=None, gateway=None) -> dict:
    # One check at a time, so the simulator and the poller can never both send the plan.
    with store.LOCK:
        return _check(ref, provider, gateway)


def _check(ref: str, provider=None, gateway=None) -> dict:
    order = store.load("orders", {}).get(ref)
    if not order:
        raise momo.PaymentError(f"unknown order {ref}")
    if order.get("fulfilled_at") or order.get("notified_at"):
        return order  # already settled; never send twice
    provider = provider or momo.get_provider()
    st = provider.status(ref)
    status = st["status"]
    gateway = gateway or sms_gateway.get_gateway()
    lang = order["lang"]
    changes = {"status": status, "reason": st.get("reason"), "checked_at": _now()}
    if status in momo.FINAL_OK:
        plan = advice.plan_sms(lang)
        gateway.send(order["phone"], i18n.t(lang, "sms.receipt", price=price_text()) + " " + plan["text"])
        changes["fulfilled_at"] = _now()
    elif status in momo.FINAL_FAIL:
        gateway.send(order["phone"], i18n.t(lang, "sms.payment_failed", code=config.USSD_CODE))
        changes["notified_at"] = _now()

    def put(d):
        d[ref].update(changes)
        return dict(d[ref])
    return store.update("orders", {}, put)


def poll_until_final(ref: str, tries: int = 24, wait: float = 5.0) -> dict | None:
    """Sandbox mode: check every few seconds until the payment settles (about 2 minutes)."""
    order = None
    for _ in range(tries):
        try:
            order = check(ref)
        except Exception:  # network trouble: try again on the next round
            pass
        if order and (order.get("fulfilled_at") or order.get("notified_at")):
            break
        time.sleep(wait)
    return order

"""HTTP routes for the basic-phone channel.

Africa's Talking-compatible callbacks (form-encoded POSTs, as AT sends them):
  POST /ussd          sessionId, serviceCode, phoneNumber, text, networkCode -> text/plain CON/END
  POST /sms/incoming  from, to, text, date, id -> 200 (a reply SMS is sent through the gateway)
Payments:
  GET  /api/payments/{ref}   check a MoMo payment; delivers the plan SMS when paid
Phone simulator (FARM_TWIN_SIMULATOR=0 turns these off in production):
  GET  /simulator, /api/sim/status, /api/sim/sms?phone=, /api/sim/momo/pending?phone=
  POST /api/sim/momo/{ref}   {"approve": true|false}: the farmer's MoMo PIN prompt
"""
from __future__ import annotations

from urllib.parse import parse_qs

from fastapi import APIRouter, Body, HTTPException, Request
from fastapi.responses import FileResponse, PlainTextResponse

from .. import config
from . import momo, orders, sms_gateway, store, ussd

router = APIRouter()


async def _form(request: Request) -> dict:
    """Parse an application/x-www-form-urlencoded body (no extra dependency)."""
    body = (await request.body()).decode("utf-8", errors="replace")
    return {k: v[0] for k, v in parse_qs(body, keep_blank_values=True).items()}


@router.post("/ussd", response_class=PlainTextResponse)
async def ussd_callback(request: Request):
    f = await _form(request)
    if not f.get("phoneNumber"):
        raise HTTPException(400, "phoneNumber missing")
    return ussd.handle(f.get("sessionId", ""), f.get("serviceCode", ""), f["phoneNumber"], f.get("text", ""))


@router.post("/sms/incoming", response_class=PlainTextResponse)
async def sms_incoming(request: Request):
    f = await _form(request)
    phone = f.get("from")
    if not phone:
        raise HTTPException(400, "from missing")
    reply = ussd.handle_sms(phone, f.get("text", ""))
    if reply:
        sms_gateway.get_gateway().send(phone, reply)
    return ""


@router.get("/api/payments/{ref}")
def payment_status(ref: str):
    try:
        return orders.check(ref)
    except momo.PaymentError as exc:
        raise HTTPException(404, str(exc)) from exc


# --- simulator helpers --------------------------------------------------------------------

def _sim():
    if not config.SIMULATOR_ENABLED:
        raise HTTPException(404, "simulator disabled")


@router.get("/simulator")
def simulator_page():
    _sim()
    return FileResponse(config.FRONTEND_DIR / "simulator.html", headers={"Cache-Control": "no-cache"})


@router.get("/api/sim/status")
def sim_status():
    _sim()
    return {"ussd_code": config.USSD_CODE, "price": orders.price_text(), "sms_gateway": config.SMS_GATEWAY,
            "momo_mode": config.MOMO_MODE, "sender": config.SMS_SENDER,
            "momo_test_numbers": momo.TEST_NUMBERS}


@router.get("/api/sim/sms")
def sim_inbox(phone: str):
    _sim()
    try:
        return {"phone": store.normalize_phone(phone), "messages": sms_gateway.inbox(phone)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/api/sim/momo/pending")
def sim_momo_pending(phone: str):
    _sim()
    if config.MOMO_MODE != "simulated":
        return {"pending": [], "note": "MTN sandbox mode: approvals happen in the MTN sandbox"}
    return {"pending": momo.SimulatedMomo().pending(store.msisdn(phone))}


@router.post("/api/sim/momo/{ref}")
def sim_momo_decide(ref: str, body: dict = Body(...)):
    _sim()
    if config.MOMO_MODE != "simulated":
        raise HTTPException(409, "only simulated payments can be approved here")
    try:
        momo.SimulatedMomo().decide(ref, bool(body.get("approve")))
        return orders.check(ref)
    except momo.PaymentError as exc:
        raise HTTPException(404, str(exc)) from exc

"""USSD + SMS + MoMo channel, offline. Climate data is SYNTHETIC; the MTN sandbox and Africa's
Talking are replaced by mock transports that check the requests we would send them."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, i18n, power  # noqa: E402
from farm_twin.channels import advice, momo, orders, sms_gateway, store, ussd  # noqa: E402
from test_power_and_climate import synthetic_power_response  # noqa: E402

PHONE = "+256770000001"


@pytest.fixture(autouse=True)
def synthetic_climate(monkeypatch):
    monkeypatch.setattr(power, "fetch_daily", lambda *a, **k: synthetic_power_response(rain=4.0))
    monkeypatch.setattr(config, "MOMO_MODE", "simulated")
    monkeypatch.setattr(config, "SMS_GATEWAY", "simulated")
    advice.clear()
    yield
    advice.clear()


def dial(text="", phone=PHONE):
    return ussd.handle("sess-1", config.USSD_CODE, phone, text)


def test_main_menu_and_free_screens():
    main = dial("")
    assert main.startswith("CON ARAPAI FARM TWIN\n1.") and "UGX 500" in main
    nxt = dial("1")
    res, _, _ = advice.advice("en")
    assert nxt.startswith("END Plant ") and i18n.t("en", f"crop.{res.rows[0]['rotation'][0]}") in nxt
    assert "Mar-Jun" in nxt and len(nxt) <= 4 + 182          # plain season months; within the USSD screen limit
    assert dial("3").startswith("END Rain, season A:")
    assert dial("4") == "END " + i18n.t("en", "ussd.soil_none")  # no SoilGrids in the test cache
    assert dial("6").startswith("END ARAPAI FARM TWIN uses NASA")
    assert dial("9") == "END " + i18n.t("en", "ussd.invalid")
    assert dial("2*2").startswith("END Cancelled")


def test_language_choice_sticks_to_the_phone():
    assert dial("5").startswith("CON 1. English")
    assert dial("5*3").startswith("END ") and dial("5*3").endswith("Kiswahili.")
    assert dial("").startswith("CON ARAPAI FARM TWIN\n1. Ushauri wa msimu ujao")
    assert dial("", phone="+256770000002").startswith("CON ARAPAI FARM TWIN\n1. Next season")


def test_paid_plan_approved_sends_plan_once():
    assert dial("2").startswith("CON Full 3-season plan by SMS: UGX 500")
    assert dial("2*1").startswith("END Check your phone and approve")
    pend = momo.SimulatedMomo().pending(store.msisdn(PHONE))
    assert len(pend) == 1 and pend[0]["amount"] == "500" and pend[0]["currency"] == "UGX"
    ref = pend[0]["ref"]
    assert orders.check(ref)["status"] == "PENDING" and not sms_gateway.inbox(PHONE)
    momo.SimulatedMomo().decide(ref, approve=True)
    order = orders.check(ref)
    assert order["status"] == "SUCCESSFUL" and order["fulfilled_at"]
    inbox = sms_gateway.inbox(PHONE)
    assert len(inbox) == 1 and inbox[0]["text"].startswith("Payment received: UGX 500.")
    assert "ARAPAI FARM TWIN plan" in inbox[0]["text"] and inbox[0]["encoding"] == "GSM-7"
    orders.check(ref)  # checking again must not send again
    assert len(sms_gateway.inbox(PHONE)) == 1


def test_rejected_and_sandbox_test_numbers_get_a_no_charge_sms():
    dial("2*1")
    ref = momo.SimulatedMomo().pending(store.msisdn(PHONE))[0]["ref"]
    momo.SimulatedMomo().decide(ref, approve=False)
    assert orders.check(ref)["status"] == "REJECTED"
    assert "No money was taken" in sms_gateway.inbox(PHONE)[0]["text"]
    failed = "+46733123450"  # MTN sandbox test number: always FAILED
    dial("2*1", phone=failed)
    order = next(o for o in store.load("orders", {}).values() if o["phone"] == failed)
    assert orders.check(order["ref"])["status"] == "FAILED"
    assert momo.SimulatedMomo().pending("46733123450") == []


@pytest.mark.parametrize("lang", ["en", "lg", "sw", "teo"])
def test_every_screen_fits_ussd_limit(lang, monkeypatch):
    store.set_phone_lang(PHONE, lang)
    for text in ["", "1", "2", "2*1", "2*2", "3", "4", "5", "6", "9"]:
        out = dial(text)
        assert out[:4] in ("CON ", "END ") and len(out) - 4 <= ussd.MAX_CHARS, (lang, text, len(out), out)
        assert "{" not in out, out


def test_inbound_sms_keywords():
    assert ussd.handle_sms(PHONE, "lang lg").endswith(": Luganda.")      # confirmation is sent in the new language
    assert "PLAN" in ussd.handle_sms(PHONE, "hello")  # help text in Luganda still names the keywords
    assert ussd.handle_sms(PHONE, "PLAN").startswith("Kebera essimu yo")
    assert ussd.handle_sms(PHONE, "   ") is None


def test_africas_talking_callbacks_over_http():
    from fastapi.testclient import TestClient
    from farm_twin import api
    client = TestClient(api.app)
    r = client.post("/ussd", data={"sessionId": "ATUid_1", "serviceCode": config.USSD_CODE,
                                   "phoneNumber": PHONE, "networkCode": "64110", "text": ""})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/plain") and r.text.startswith("CON ")
    assert client.post("/ussd", data={"sessionId": "ATUid_1", "phoneNumber": PHONE, "text": "2*1"}).text.startswith("END ")
    ref = client.get(f"/api/sim/momo/pending?phone={PHONE}").json()["pending"][0]["ref"]
    assert client.post(f"/api/sim/momo/{ref}", json={"approve": True}).json()["status"] == "SUCCESSFUL"
    msgs = client.get(f"/api/sim/sms?phone={PHONE}").json()["messages"]
    assert msgs and msgs[0]["text"].startswith("Payment received")
    assert client.get(f"/api/payments/{ref}").json()["fulfilled_at"]
    assert client.post("/sms/incoming", data={"from": PHONE, "to": "FARMTWIN", "text": "HELP"}).status_code == 200
    assert client.get("/simulator").status_code == 200
    assert client.post("/ussd", data={"text": ""}).status_code == 400


def test_simulator_can_be_switched_off(monkeypatch):
    from fastapi.testclient import TestClient
    from farm_twin import api
    monkeypatch.setattr(config, "SIMULATOR_ENABLED", False)
    client = TestClient(api.app)
    assert client.get("/simulator").status_code == 404
    assert client.get(f"/api/sim/sms?phone={PHONE}").status_code == 404
    assert client.post("/ussd", data={"phoneNumber": PHONE, "text": ""}).status_code == 200  # the real channel stays


def test_mtn_sandbox_requests_are_well_formed(tmp_path):
    seen = []

    def handler(req: httpx.Request):
        seen.append(req)
        assert req.url.host == "sandbox.momodeveloper.mtn.com"
        assert req.headers["Ocp-Apim-Subscription-Key"] == "test-key"
        p = req.url.path
        if p == "/v1_0/apiuser":
            return httpx.Response(201)
        if p.endswith("/apikey"):
            return httpx.Response(201, json={"apiKey": "k123"})
        if p == "/collection/token/":
            assert req.headers["Authorization"].startswith("Basic ")
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        if p == "/collection/v1_0/requesttopay" and req.method == "POST":
            assert req.headers["Authorization"] == "Bearer tok"
            assert req.headers["X-Target-Environment"] == "sandbox" and req.headers["X-Reference-Id"]
            body = json.loads(req.content)
            assert body["currency"] == "EUR" and body["payer"] == {"partyIdType": "MSISDN", "partyId": "256770000001"}
            return httpx.Response(202)
        if p.startswith("/collection/v1_0/requesttopay/"):
            return httpx.Response(200, json={"status": "SUCCESSFUL", "financialTransactionId": "123"})
        return httpx.Response(404)

    client = httpx.Client(base_url=momo.SANDBOX_URL, transport=httpx.MockTransport(handler))
    m = momo.MomoSandbox("test-key", client=client)
    ref = m.request_to_pay("256770000001", "500", "UGX", "ext1", "pay", "note")
    assert m.status(ref)["status"] == "SUCCESSFUL"
    m.request_to_pay("256770000001", "500", "UGX", "ext2", "pay", "note")  # reuses user and token
    assert [r.url.path for r in seen].count("/v1_0/apiuser") == 1
    assert [r.url.path for r in seen].count("/collection/token/") == 1
    with pytest.raises(momo.PaymentError):
        momo.MomoSandbox("")


def test_africas_talking_sms_request_is_well_formed():
    def handler(req: httpx.Request):
        assert req.url.host == "api.sandbox.africastalking.com" and req.headers["apiKey"] == "at-key"
        form = dict(httpx.QueryParams(req.content.decode()))
        assert form["username"] == "sandbox" and form["to"] == PHONE and form["message"] == "hello"
        return httpx.Response(201, json={"SMSMessageData": {"Recipients": [{"status": "Success", "messageId": "ATX1"}]}})

    gw = sms_gateway.AfricasTalkingGateway("sandbox", "at-key", client=httpx.Client(transport=httpx.MockTransport(handler)))
    msg = gw.send(PHONE, "hello")
    assert msg["status"] == "Success" and msg["provider_id"] == "ATX1"
    assert sms_gateway.inbox(PHONE)[0]["gateway"] == "africastalking"
    with pytest.raises(sms_gateway.SmsError):
        sms_gateway.AfricasTalkingGateway("", "")

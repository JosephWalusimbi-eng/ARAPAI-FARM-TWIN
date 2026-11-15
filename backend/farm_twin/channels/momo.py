"""MTN MoMo Collections: ask a farmer's phone to approve a payment ("request to pay").

MOMO_MODE=simulated (default): payments live in data/channels/momo_simulated.json. Like the MTN
sandbox's test numbers, some MSISDNs give fixed outcomes; any other number stays PENDING until
the phone simulator approves or rejects it (standing in for the farmer's MoMo PIN prompt).

MOMO_MODE=sandbox: the real MTN MoMo sandbox (https://momodeveloper.mtn.com). Needs
MOMO_SUBSCRIPTION_KEY (the Collections product's primary key) in .env. The sandbox API user
and key are created on first use and kept in data/channels/momo_sandbox_user.json
(git-ignored). The sandbox accepts only EUR, so amounts are sent in EUR there.
The subscription key and tokens are only ever sent to sandbox.momodeveloper.mtn.com.
"""
from __future__ import annotations

import datetime as dt
import os
import time
import uuid

import httpx

from .. import config
from ..storage import atomic_write_text, read_json_safe
from . import store

SANDBOX_URL = "https://sandbox.momodeveloper.mtn.com"
# MTN sandbox test MSISDNs and their fixed request-to-pay outcomes (simulated the same way).
TEST_NUMBERS = {"46733123450": "FAILED", "46733123451": "REJECTED", "46733123452": "TIMEOUT",
                "46733123453": "ONGOING", "46733123454": "PENDING"}
FINAL_OK = {"SUCCESSFUL"}
FINAL_FAIL = {"FAILED", "REJECTED", "TIMEOUT"}


class PaymentError(Exception):
    pass


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class SimulatedMomo:
    name = "simulated"

    def request_to_pay(self, msisdn: str, amount: str, currency: str, external_id: str,
                       payer_message: str, payee_note: str) -> str:
        ref = str(uuid.uuid4())
        status = TEST_NUMBERS.get(msisdn, "PENDING")
        entry = {"msisdn": msisdn, "amount": amount, "currency": currency, "external_id": external_id,
                 "payer_message": payer_message, "status": status, "created_at": _now()}
        store.update("momo_simulated", {}, lambda d: d.__setitem__(ref, entry))
        return ref

    def status(self, ref: str) -> dict:
        entry = store.load("momo_simulated", {}).get(ref)
        if not entry:
            raise PaymentError(f"unknown payment {ref}")
        return {"status": entry["status"], "reason": entry.get("reason")}

    def decide(self, ref: str, approve: bool) -> dict:
        """The farmer approves (enters PIN) or rejects on the simulated phone."""
        def put(d):
            entry = d.get(ref)
            if not entry:
                raise PaymentError(f"unknown payment {ref}")
            if entry["status"] == "PENDING" and entry["msisdn"] not in TEST_NUMBERS:
                entry["status"] = "SUCCESSFUL" if approve else "REJECTED"
                entry["reason"] = None if approve else "APPROVAL_REJECTED"
                entry["decided_at"] = _now()
            return dict(entry)
        return store.update("momo_simulated", {}, put)

    def pending(self, msisdn: str) -> list[dict]:
        return [{"ref": ref, **e} for ref, e in store.load("momo_simulated", {}).items()
                if e["msisdn"] == msisdn and e["status"] == "PENDING" and msisdn not in TEST_NUMBERS]


class MomoSandbox:
    name = "sandbox"

    def __init__(self, subscription_key: str, client: httpx.Client | None = None):
        if not subscription_key:
            raise PaymentError("set MOMO_SUBSCRIPTION_KEY in .env (MoMo developer portal, Collections product)")
        self.key = subscription_key
        self.client = client or httpx.Client(base_url=SANDBOX_URL, timeout=30)
        self._token: tuple[str, float] | None = None

    @property
    def _user_path(self):
        return config.CHANNELS_DIR / "momo_sandbox_user.json"

    def _user(self) -> tuple[str, str]:
        """The sandbox API user and key, created once and kept (git-ignored)."""
        saved = read_json_safe(self._user_path)
        if saved:
            return saved["user"], saved["api_key"]
        user = str(uuid.uuid4())
        h = {"X-Reference-Id": user, "Ocp-Apim-Subscription-Key": self.key}
        r = self.client.post("/v1_0/apiuser", headers=h, json={"providerCallbackHost": "localhost"})
        if r.status_code not in (201, 409):
            raise PaymentError(f"MoMo sandbox: could not create API user (HTTP {r.status_code})")
        r = self.client.post(f"/v1_0/apiuser/{user}/apikey", headers={"Ocp-Apim-Subscription-Key": self.key})
        if r.status_code != 201:
            raise PaymentError(f"MoMo sandbox: could not create API key (HTTP {r.status_code})")
        api_key = r.json()["apiKey"]
        config.CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(self._user_path, f'{{"user": "{user}", "api_key": "{api_key}"}}')
        return user, api_key

    def _bearer(self) -> str:
        if self._token and self._token[1] > time.time() + 30:
            return self._token[0]
        user, api_key = self._user()
        r = self.client.post("/collection/token/", auth=(user, api_key),
                             headers={"Ocp-Apim-Subscription-Key": self.key})
        if r.status_code != 200:
            raise PaymentError(f"MoMo sandbox: token refused (HTTP {r.status_code})")
        body = r.json()
        self._token = (body["access_token"], time.time() + float(body.get("expires_in", 3600)))
        return self._token[0]

    def _headers(self, **extra) -> dict:
        return {"Authorization": f"Bearer {self._bearer()}", "X-Target-Environment": "sandbox",
                "Ocp-Apim-Subscription-Key": self.key, **extra}

    def request_to_pay(self, msisdn: str, amount: str, currency: str, external_id: str,
                       payer_message: str, payee_note: str) -> str:
        ref = str(uuid.uuid4())
        body = {"amount": str(amount), "currency": "EUR",  # the sandbox only accepts EUR
                "externalId": external_id, "payer": {"partyIdType": "MSISDN", "partyId": msisdn},
                "payerMessage": payer_message[:160], "payeeNote": payee_note[:160]}
        r = self.client.post("/collection/v1_0/requesttopay", json=body,
                             headers=self._headers(**{"X-Reference-Id": ref}))
        if r.status_code != 202:
            raise PaymentError(f"MoMo sandbox: request to pay refused (HTTP {r.status_code})")
        return ref

    def status(self, ref: str) -> dict:
        r = self.client.get(f"/collection/v1_0/requesttopay/{ref}", headers=self._headers())
        if r.status_code != 200:
            raise PaymentError(f"MoMo sandbox: status check failed (HTTP {r.status_code})")
        body = r.json()
        return {"status": body.get("status", "PENDING"), "reason": body.get("reason"),
                "financial_transaction_id": body.get("financialTransactionId")}


def get_provider():
    if config.MOMO_MODE == "sandbox":
        return MomoSandbox(os.environ.get("MOMO_SUBSCRIPTION_KEY", ""))
    return SimulatedMomo()

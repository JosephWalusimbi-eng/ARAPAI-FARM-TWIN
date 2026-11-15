"""Sending SMS: a simulated outbox, or the Africa's Talking SMS API.

SMS_GATEWAY=simulated (default): messages go to data/channels/sms_outbox.json and appear in the
phone simulator's inbox. SMS_GATEWAY=africastalking with AT_USERNAME and AT_API_KEY in .env:
messages go to Africa's Talking (username "sandbox" uses their sandbox and its simulator).
Every message is also kept in the local outbox so the team can see what was sent.
"""
from __future__ import annotations

import datetime as dt
import os
import uuid

import httpx

from .. import config, sms
from . import store

AT_LIVE_URL = "https://api.africastalking.com/version1/messaging"
AT_SANDBOX_URL = "https://api.sandbox.africastalking.com/version1/messaging"


class SmsError(Exception):
    pass


def _record(to: str, text: str, status: str, gateway: str, provider_id: str | None = None) -> dict:
    seg = sms.segment(text)
    msg = {"id": uuid.uuid4().hex[:12], "to": store.normalize_phone(to), "from": config.SMS_SENDER,
           "text": text, "segments": len(seg["segments"]), "encoding": seg["encoding"],
           "sent_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
           "status": status, "gateway": gateway, "provider_id": provider_id}
    store.update("sms_outbox", [], lambda box: box.append(msg))
    return msg


class SimulatedGateway:
    name = "simulated"

    def send(self, to: str, text: str) -> dict:
        return _record(to, text, "delivered (simulated)", self.name)


class AfricasTalkingGateway:
    """Africa's Talking bulk SMS API (form POST, apiKey header)."""
    name = "africastalking"

    def __init__(self, username: str, api_key: str, sender_id: str | None = None,
                 client: httpx.Client | None = None):
        if not (username and api_key):
            raise SmsError("set AT_USERNAME and AT_API_KEY in .env")
        self.username, self.api_key, self.sender_id = username, api_key, sender_id
        self.url = AT_SANDBOX_URL if username == "sandbox" else AT_LIVE_URL
        self.client = client or httpx.Client(timeout=30)

    def send(self, to: str, text: str) -> dict:
        data = {"username": self.username, "to": store.normalize_phone(to), "message": text}
        if self.sender_id:
            data["from"] = self.sender_id
        try:
            r = self.client.post(self.url, data=data,
                                 headers={"apiKey": self.api_key, "Accept": "application/json"})
            r.raise_for_status()
            rec = ((r.json().get("SMSMessageData") or {}).get("Recipients") or [{}])[0]
        except (httpx.HTTPError, ValueError) as exc:
            _record(to, text, f"failed: {exc}", self.name)
            raise SmsError(f"Africa's Talking send failed: {exc}") from exc
        return _record(to, text, rec.get("status", "sent"), self.name, rec.get("messageId"))


def get_gateway():
    if config.SMS_GATEWAY == "africastalking":
        return AfricasTalkingGateway(os.environ.get("AT_USERNAME", ""), os.environ.get("AT_API_KEY", ""),
                                     os.environ.get("AT_SENDER_ID") or None)
    return SimulatedGateway()


def inbox(phone: str) -> list[dict]:
    """Messages sent to a phone, newest first (simulator view)."""
    phone = store.normalize_phone(phone)
    return [m for m in reversed(store.load("sms_outbox", [])) if m["to"] == phone]

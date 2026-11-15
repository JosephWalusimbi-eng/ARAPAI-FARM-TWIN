"""Small JSON stores for the channel state (phones, SMS outbox, orders, simulated payments).

Each store is one file in config.CHANNELS_DIR, written atomically under a process lock, so a
crash or power cut never leaves a half-written file. Fine for a demo and a pilot; a real
deployment would use a database.
"""
from __future__ import annotations

import json
import re
import threading
from typing import Any, Callable

from .. import config
from ..storage import atomic_write_text, read_json_safe

LOCK = threading.RLock()


def _path(name: str):
    return config.CHANNELS_DIR / f"{name}.json"


def load(name: str, default: Any) -> Any:
    with LOCK:
        data = read_json_safe(_path(name))
        return default if data is None else data


def save(name: str, data: Any) -> None:
    with LOCK:
        config.CHANNELS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_path(name), json.dumps(data, ensure_ascii=False, indent=1))


def update(name: str, default: Any, fn: Callable[[Any], Any]) -> Any:
    """Read-modify-write under the lock; fn mutates the data and may return a result."""
    with LOCK:
        data = load(name, default)
        result = fn(data)
        save(name, data)
        return result


def normalize_phone(phone: str) -> str:
    """'+256 772-123456' -> '+256772123456' (Africa's Talking style, with the +)."""
    digits = re.sub(r"[^\d]", "", phone or "")
    if not digits:
        raise ValueError("phone number missing")
    return "+" + digits


def msisdn(phone: str) -> str:
    """MTN MoMo wants digits only: '+256772123456' -> '256772123456'."""
    return normalize_phone(phone)[1:]


# --- phones: language and linked farm ---------------------------------------------------

def phone_profile(phone: str) -> dict:
    """The phone's settings; a new phone is linked to the pilot farm in English (demo)."""
    phone = normalize_phone(phone)

    def get(phones):
        if phone not in phones:
            phones[phone] = {"lang": "en", "farm_id": config.PILOT_FARM["farm_id"]}
        return dict(phones[phone])
    return update("phones", {}, get)


def set_phone_lang(phone: str, lang: str) -> None:
    phone = normalize_phone(phone)

    def put(phones):
        phones.setdefault(phone, {"farm_id": config.PILOT_FARM["farm_id"]})["lang"] = lang
    update("phones", {}, put)

"""Farm plans saved on a phone (possibly offline) and synced to the server later.

Sync is append-only and idempotent: each plan carries an id made on the device, so sending
the same plan twice (a retry after a dropped connection) stores it once. Plans are never
edited after saving, so two devices cannot conflict.
Stored in data/plans/<farm_id>.json (git-ignored), written atomically.
"""
from __future__ import annotations

import datetime as dt
import json
import re
import threading

from . import config
from .crops import CROPS
from .storage import atomic_write_text, read_json_safe

PLANS_DIR = config.ROOT / "data" / "plans"
ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
MAX_PLANS_PER_REQUEST = 200
_LOCK = threading.Lock()  # merges are read-modify-write: one at a time, or a plan could be lost


class PlanError(ValueError):
    pass


def _path(farm_id: str):
    if not ID_RE.match(farm_id.replace("-", "_")):
        raise PlanError("bad farm id")
    return PLANS_DIR / f"{farm_id}.json"


def load(farm_id: str) -> list[dict]:
    return (read_json_safe(_path(farm_id)) or {}).get("plans", [])


def _validate(plan: dict) -> str | None:
    if not isinstance(plan, dict):
        return "not an object"
    if not ID_RE.match(str(plan.get("id", ""))):
        return "id must be 8-64 letters, digits, - or _"
    rot = plan.get("rotation")
    if not (isinstance(rot, list) and len(rot) == 3 and all(c in CROPS for c in rot)):
        return "rotation must list 3 known crop keys"
    if not isinstance(plan.get("created_at"), str):
        return "created_at missing"
    if len(json.dumps(plan)) > 4000:
        return "plan too large"
    return None


def merge(farm_id: str, incoming: list) -> dict:
    """Add new plans; ignore ones already stored. Returns accepted / duplicate / rejected ids."""
    if not isinstance(incoming, list) or len(incoming) > MAX_PLANS_PER_REQUEST:
        raise PlanError(f"send a list of at most {MAX_PLANS_PER_REQUEST} plans")
    with _LOCK:
        return _merge(farm_id, incoming)


def _merge(farm_id: str, incoming: list) -> dict:
    stored = load(farm_id)
    known = {p["id"] for p in stored}
    result = {"accepted": [], "duplicates": [], "rejected": []}
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    for plan in incoming:
        why = _validate(plan)
        if why:
            result["rejected"].append({"id": plan.get("id") if isinstance(plan, dict) else None,
                                       "reason": why})
        elif plan["id"] in known:
            result["duplicates"].append(plan["id"])
        else:
            stored.append({**plan, "farm_id": farm_id, "received_at": now})
            known.add(plan["id"])
            result["accepted"].append(plan["id"])
    if result["accepted"]:
        PLANS_DIR.mkdir(parents=True, exist_ok=True)
        atomic_write_text(_path(farm_id), json.dumps({"plans": stored}, ensure_ascii=False, indent=1))
    return result

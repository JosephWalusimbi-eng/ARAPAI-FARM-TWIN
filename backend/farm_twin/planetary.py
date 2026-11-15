"""Small client for Microsoft's Planetary Computer: a free, no-login STAC catalogue of public
NASA/USGS/ESA data held as cloud-optimised GeoTIFFs. We use it to read single pixels and tiny
windows (a 140 m square) without downloading whole scenes.

Notes learned the hard way (14 Nov 2026):
- The signing token must be requested for the storage CONTAINER (/token/<account>/<container>);
  the per-collection token returned 403 for MODIS.
- Tokens last about a day; we ask once per run and again on a 403.
"""
from __future__ import annotations

import threading
import time
from urllib.parse import urlparse

import httpx

STAC = "https://planetarycomputer.microsoft.com/api/stac/v1"
SAS = "https://planetarycomputer.microsoft.com/api/sas/v1/token"

_tokens: dict[str, tuple[float, str]] = {}
_lock = threading.Lock()


class PlanetaryError(RuntimeError):
    pass


def search(collection: str, lat: float, lon: float, start: str, end: str, *, limit: int = 200,
           query: dict | None = None) -> list[dict]:
    """All STAC items touching the point in [start, end] (ISO dates), following pages."""
    body = {"collections": [collection], "intersects": {"type": "Point", "coordinates": [lon, lat]},
            "datetime": f"{start}/{end}", "limit": limit}
    if query:
        body["query"] = query
    items: list[dict] = []
    url, first = f"{STAC}/search", True
    for _ in range(50):
        try:
            r = httpx.post(url, json=body, timeout=60) if first else httpx.get(url, timeout=60)
            r.raise_for_status()
        except httpx.HTTPError as exc:
            raise PlanetaryError(f"catalogue search failed: {exc}") from exc
        data = r.json()
        items += data.get("features", [])
        nxt = next((ln for ln in data.get("links", []) if ln.get("rel") == "next"), None)
        if not nxt or not data.get("features"):
            break
        url, first = nxt["href"], False
    return items


def signed(href: str, *, renew: bool = False) -> str:
    """href + a read token for its storage container."""
    u = urlparse(href)
    account = u.netloc.split(".")[0]
    container = u.path.lstrip("/").split("/")[0]
    key = f"{account}/{container}"
    with _lock:
        got = _tokens.get(key)
        if renew or got is None or got[0] < time.time():
            try:
                r = httpx.get(f"{SAS}/{key}", timeout=60)
                r.raise_for_status()
            except httpx.HTTPError as exc:
                raise PlanetaryError(f"could not get a read token for {key}: {exc}") from exc
            _tokens[key] = (time.time() + 3000, r.json()["token"])
        token = _tokens[key][1]
    return f"{href}?{token}"

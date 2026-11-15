"""NASA Earthdata Login: credential lookup and an authenticated HTTP session.

Credentials, first match wins:
  1. EARTHDATA_TOKEN                          -> "Authorization: Bearer" on Earthdata hosts
  2. EARTHDATA_USERNAME + EARTHDATA_PASSWORD  -> basic auth, sent ONLY to urs.earthdata.nasa.gov
  3. ~/.netrc or ~/_netrc entry for urs.earthdata.nasa.gov
Values may live in a git-ignored .env file at the project root (see config.py).
"""
from __future__ import annotations

import netrc
import os
from pathlib import Path
from urllib.parse import urljoin, urlparse

import httpx

from .. import config
from .base import NotConfigured, SourceError

# Hosts that may receive a bearer token. Never send credentials anywhere else.
_TOKEN_HOST_SUFFIXES = (".earthdata.nasa.gov", ".earthdatacloud.nasa.gov")


class EarthdataAuthError(SourceError):
    pass


def credentials() -> tuple[str, object] | None:
    token = os.environ.get("EARTHDATA_TOKEN", "").strip()
    if token:
        return ("token", token)
    user = os.environ.get("EARTHDATA_USERNAME", "").strip()
    pwd = os.environ.get("EARTHDATA_PASSWORD", "")
    if user and pwd:
        return ("basic", (user, pwd))
    for name in (".netrc", "_netrc"):
        path = Path.home() / name
        if path.exists():
            try:
                auth = netrc.netrc(str(path)).authenticators(config.EARTHDATA_URS_HOST)
            except (netrc.NetrcParseError, OSError):
                continue
            if auth and auth[0] and auth[2]:
                return ("basic", (auth[0], auth[2]))
    return None


def describe() -> str:
    c = credentials()
    if c is None:
        return "not configured"
    return "token" if c[0] == "token" else f"username/password ({c[1][0]})"


class EarthdataSession:
    """Follows Earthdata's OAuth redirects by hand so credentials go only where allowed."""

    def __init__(self, creds=None, *, timeout: float = 60.0, transport=None):
        self.creds = creds if creds is not None else credentials()
        if self.creds is None:
            raise NotConfigured(
                "Earthdata Login not configured: set EARTHDATA_TOKEN (or "
                "EARTHDATA_USERNAME/EARTHDATA_PASSWORD) in .env or the environment")
        self.client = httpx.Client(timeout=timeout, follow_redirects=False, transport=transport,
                                   headers={"User-Agent": "farm-twin-prototype"})

    def close(self):
        self.client.close()

    def _auth_for(self, url: str):
        host = urlparse(url).hostname or ""
        kind, value = self.creds
        if kind == "token" and host.endswith(_TOKEN_HOST_SUFFIXES):
            return {"Authorization": f"Bearer {value}"}, None
        if kind == "basic" and host == config.EARTHDATA_URS_HOST:
            return {}, value
        return {}, None

    def get(self, url: str, max_redirects: int = 10) -> httpx.Response:
        for _ in range(max_redirects):
            headers, auth = self._auth_for(url)
            resp = self.client.get(url, headers=headers, auth=auth)
            if resp.is_redirect:
                url = urljoin(url, resp.headers["location"])
                continue
            if resp.status_code in (401, 403):
                raise EarthdataAuthError(
                    f"Earthdata refused access (HTTP {resp.status_code}) for {urlparse(url).hostname}. "
                    "Check the credentials, and for some archives approve the data application "
                    "in your Earthdata profile (Applications > Authorized Apps).")
            return resp
        raise SourceError(f"too many redirects for {url}")

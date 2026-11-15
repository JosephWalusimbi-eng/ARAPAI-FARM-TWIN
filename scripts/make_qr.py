"""Write frontend/phone-qr.svg: a QR code for the live app, so a phone camera opens it.

    python scripts/make_qr.py                      # the production address below
    python scripts/make_qr.py https://example.org/ # another address

Needs `pip install segno` (a tiny pure-Python library). It is only used here, once: the page
serves the finished SVG file, so the running app needs no QR library and no outside service.
"""
from __future__ import annotations

import sys
from pathlib import Path

import segno

DEFAULT_URL = "https://farm-twin-5n9n.onrender.com/"
OUT = Path(__file__).resolve().parents[1] / "frontend" / "phone-qr.svg"


def main(argv=None) -> int:
    url = (argv or sys.argv[1:] or [DEFAULT_URL])[0]
    qr = segno.make(url, error="m")
    qr.save(str(OUT), scale=10, border=2, dark="#1b1f1a", light="#ffffff", xmldecl=False,
            title="QR code: open ARAPAI FARM TWIN on a smartphone", desc=url)
    print(f"wrote {OUT} for {url} ({qr.version} version, {len(qr.matrix)} modules wide)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

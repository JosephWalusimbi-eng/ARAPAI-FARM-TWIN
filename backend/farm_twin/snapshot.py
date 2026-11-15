"""Data snapshot: the downloaded data, committed to the repo, so any clone runs at once.

data/cache/ (downloads) is git-ignored and takes hours plus an Earthdata token to rebuild.
data/snapshot/ holds a copy of the files the app needs, with SHA-256 checksums in
MANIFEST.json. On startup the server copies any snapshot file that is missing from the cache
(never overwriting fresher downloads), so a new laptop, a teammate's clone or a cloud host
works with no NASA downloads and no network.

    python scripts/snapshot.py create   # after fetching new data: refresh the snapshot
    python scripts/snapshot.py seed     # copy snapshot files into an empty cache
    python scripts/snapshot.py status
"""
from __future__ import annotations

import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path

from . import config
from .storage import atomic_write_text, read_json_safe

SNAPSHOT_DIR = config.ROOT / "data" / "snapshot"
# Cache files the app reads. SMAP granule listings are only needed to download, so they stay out.
INCLUDE_PREFIXES = ("power_", "imerg_", "smap_raw_", "soilgrids_", "vegetation_", "terrain_", "era5_")


def _sha256(path: Path) -> str:
    # Hash with line endings normalised: git on Windows turns CRLF into LF when committing, and a
    # byte-exact hash would then reject the file on the host (this dropped the soil data on Render).
    return hashlib.sha256(open(path, "rb").read().replace(b"\r\n", b"\n")).hexdigest()


def _wanted(path: Path) -> bool:
    return path.suffix == ".json" and path.name.startswith(INCLUDE_PREFIXES)


def create(cache_dir: Path | None = None, snapshot_dir: Path | None = None) -> dict:
    cache_dir, snapshot_dir = Path(cache_dir or config.CACHE_DIR), Path(snapshot_dir or SNAPSHOT_DIR)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for src in sorted(p for p in cache_dir.glob("*.json") if _wanted(p)):
        if read_json_safe(src) is None:
            continue  # damaged download: never snapshot it
        dst = snapshot_dir / src.name
        shutil.copyfile(src, dst.with_name(dst.name + ".tmp"))
        dst.with_name(dst.name + ".tmp").replace(dst)
        files.append({"name": src.name, "bytes": dst.stat().st_size, "sha256": _sha256(dst)})
    for old in snapshot_dir.glob("*.json"):
        if old.name != "MANIFEST.json" and old.name not in {f["name"] for f in files}:
            old.unlink()
    manifest = {"created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                "note": "Copies of data/cache files the app needs. Refresh with scripts/snapshot.py create.",
                "files": files}
    atomic_write_text(snapshot_dir / "MANIFEST.json", json.dumps(manifest, indent=1))
    return manifest


def manifest(snapshot_dir: Path | None = None) -> dict | None:
    return read_json_safe(Path(snapshot_dir or SNAPSHOT_DIR) / "MANIFEST.json")


def seed(cache_dir: Path | None = None, snapshot_dir: Path | None = None) -> list[str]:
    """Copy snapshot files missing from the cache (checksum-verified). Returns copied names."""
    cache_dir, snapshot_dir = Path(cache_dir or config.CACHE_DIR), Path(snapshot_dir or SNAPSHOT_DIR)
    m = manifest(snapshot_dir)
    if not m:
        return []
    cache_dir.mkdir(parents=True, exist_ok=True)
    copied = []
    for f in m["files"]:
        dst = cache_dir / f["name"]
        if dst.exists() or dst.with_name(dst.name + ".bak").exists():
            continue  # a local download (or its backup) always wins
        src = snapshot_dir / f["name"]
        if not src.exists() or _sha256(src) != f["sha256"]:
            continue  # missing or damaged snapshot file: skip rather than install bad data
        tmp = dst.with_name(dst.name + ".seed.tmp")
        shutil.copyfile(src, tmp)
        tmp.replace(dst)
        copied.append(f["name"])
    return copied

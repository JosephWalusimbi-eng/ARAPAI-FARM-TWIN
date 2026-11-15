"""Power-loss-safe file writes (no project imports, so any module can use it).

Write to a temp file, fsync it to disk, keep the previous version as .bak, then swap.
If the laptop loses power at any point, either the new file or the .bak is complete.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path


def _replace(src: Path, dst: Path, tries: int = 5) -> None:
    """os.replace, retried briefly: on Windows it fails while another process reads dst."""
    for attempt in range(tries):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == tries - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve()).lower()
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def atomic_write_text(path: Path, text: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    # One writer per file at a time within this process (Windows refuses simultaneous
    # replaces of the same file); separate processes are covered by the retries in _replace.
    with _lock_for(path):
        _atomic_write(path, text)


def _atomic_write(path: Path, text: str) -> None:
    # A unique temp name per write: two writers (threads or processes) must never share one,
    # or one would move the other's file away and leave the target missing.
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex[:8]}.tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            _replace(path, path.with_name(path.name + ".bak"))
        _replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink(missing_ok=True)


def read_json_safe(path: Path):
    """Read JSON written by atomic_write_text; fall back to .bak if the main file is bad."""
    path = Path(path)
    for candidate in (path, path.with_name(path.name + ".bak")):
        if candidate.exists():
            try:
                return json.loads(candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError, OSError):
                continue
    return None

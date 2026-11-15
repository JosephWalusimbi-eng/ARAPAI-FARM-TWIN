"""Manage the committed data snapshot (data/snapshot/).

    python scripts/snapshot.py create   # copy current downloads into the snapshot (then commit it)
    python scripts/snapshot.py seed     # fill an empty data/cache from the snapshot
    python scripts/snapshot.py status   # what is in the snapshot and the cache
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, snapshot  # noqa: E402


def main(argv=None) -> int:
    cmd = (argv or sys.argv[1:] or ["status"])[0]
    if cmd == "create":
        m = snapshot.create()
        total = sum(f["bytes"] for f in m["files"])
        print(f"Snapshot: {len(m['files'])} files, {total / 1e6:.1f} MB in {snapshot.SNAPSHOT_DIR}")
        for f in m["files"]:
            print(f"  {f['name']}  {f['bytes'] / 1e3:.0f} KB")
        print("Commit data/snapshot/ so every clone and the cloud host get this data.")
    elif cmd == "seed":
        copied = snapshot.seed()
        print(f"Copied {len(copied)} file(s) into {config.CACHE_DIR}" + (":" if copied else " (cache already complete)."))
        for name in copied:
            print("  " + name)
    elif cmd == "status":
        m = snapshot.manifest()
        if not m:
            print("No snapshot yet: run  python scripts/snapshot.py create")
            return 0
        print(f"Snapshot created {m['created_at']}")
        for f in m["files"]:
            here = (config.CACHE_DIR / f["name"]).exists()
            print(f"  {'in cache ' if here else 'MISSING  '} {f['name']}")
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

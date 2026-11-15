"""Power-loss / interruption tests: downloads must resume, saved files must survive.
All data here is SYNTHETIC test input."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import httpx
import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, storage  # noqa: E402
from farm_twin.sources import smap, zarr_point  # noqa: E402

from test_sources import _pixel_nc4  # noqa: E402


def test_atomic_write_keeps_backup_and_survives_corruption(tmp_path):
    p = tmp_path / "x.json"
    storage.atomic_write_text(p, json.dumps({"v": 1}))
    storage.atomic_write_text(p, json.dumps({"v": 2}))
    assert storage.read_json_safe(p) == {"v": 2}
    p.write_text('{"v": 3, "trunc', encoding="utf-8")  # simulate a write cut off by power loss
    assert storage.read_json_safe(p) == {"v": 1}      # previous complete version
    assert not list(tmp_path.glob("*.tmp"))


def _synthetic_store(days=10, chunk=3):
    xr = pytest.importorskip("xarray")
    times = pd.date_range("2020-01-01", periods=days, freq="D")
    data = np.arange(days, dtype="float32").reshape(days, 1, 1)
    ds = xr.Dataset({"RAIN": (("time", "lat", "lon"), data, {"units": "mm/day"})},
                    coords={"time": times, "lat": [1.7], "lon": [33.6]})
    ds["RAIN"].encoding["chunks"] = (chunk, 1, 1)
    return xr, ds


def test_zarr_read_resumes_from_saved_blocks(monkeypatch, tmp_path):
    xr, ds = _synthetic_store()
    monkeypatch.setattr(xr, "open_zarr", lambda *a, **k: ds)
    monkeypatch.setattr(zarr_point.time, "sleep", lambda s: None)
    real_load = zarr_point._load_block
    loads = []

    def dies_on_third_block(block):
        if len(loads) == 2:
            raise ConnectionResetError("power cut")
        loads.append(1)
        return real_load(block)

    monkeypatch.setattr(zarr_point, "_load_block", dies_on_third_block)
    with pytest.raises(zarr_point.ZarrReadError, match="2 of 4 blocks saved"):
        zarr_point.read_point("s3://x", ["RAIN"], 1.7, 33.6, "20200101", "20200110",
                              retries=1, checkpoint_root=tmp_path)

    # "Laptop back on": rerun downloads only blocks 3 and 4.
    loads.clear()
    monkeypatch.setattr(zarr_point, "_load_block", lambda b: (loads.append(1), real_load(b))[1])
    pt = zarr_point.read_point("s3://x", ["RAIN"], 1.7, 33.6, "20200101", "20200110",
                               checkpoint_root=tmp_path)
    assert len(loads) == 2
    assert pt.df["RAIN"].tolist() == list(range(10))
    pt.discard_checkpoint()
    assert not any(tmp_path.rglob("*.csv"))


def test_zarr_half_written_block_is_redone(monkeypatch, tmp_path):
    xr, ds = _synthetic_store()
    monkeypatch.setattr(xr, "open_zarr", lambda *a, **k: ds)
    ck = zarr_point.checkpoint_dir("s3://x", ["RAIN"], 1.7, 33.6, "20200101", "20200110", tmp_path)
    ck.mkdir(parents=True)
    (ck / "block_000.csv").write_text("date,RA", encoding="utf-8")  # truncated by a crash
    pt = zarr_point.read_point("s3://x", ["RAIN"], 1.7, 33.6, "20200101", "20200110",
                               checkpoint_root=tmp_path)
    assert pt.df["RAIN"].tolist() == list(range(10))


def test_smap_interrupted_run_keeps_finished_days(monkeypatch, tmp_path):
    days = {f"202404{d:02d}": f"https://opendap.example/{d}.h5" for d in range(1, 11)}
    monkeypatch.setattr(smap, "list_granules", lambda *a, **k: days)
    monkeypatch.setattr(config, "SMAP_WORKERS", 1)
    monkeypatch.setattr(smap.SmapSource, "_check_geolocation", staticmethod(lambda *a: None))

    class DyingSession:
        def __init__(self, die_after):
            self.calls, self.die_after = 0, die_after

        def get(self, url):
            self.calls += 1
            if self.calls > self.die_after:
                raise KeyboardInterrupt  # stands in for the process being killed
            return httpx.Response(200, content=_pixel_nc4())

        def close(self):
            pass

    src = smap.SmapSource()
    with pytest.raises(KeyboardInterrupt):
        src.fetch(1.71, 33.61, "20240401", "20240410", cache_dir=tmp_path, session=DyingSession(4))
    row, col = smap.ease2_9km_rowcol(1.71, 33.61)
    saved = storage.read_json_safe(src.raw_cache_path(row, col, tmp_path))
    assert sum(1 for v in saved["days"].values() if v) == 4

    resumed = DyingSession(die_after=10**6)
    s = src.fetch(1.71, 33.61, "20240401", "20240410", cache_dir=tmp_path, session=resumed)
    assert resumed.calls == 6                       # only the 6 missing days
    assert s.df["SMAP_SM"].notna().sum() == 10


def test_parallel_writes_and_plan_syncs_lose_nothing(tmp_path, monkeypatch):
    """Two syncs arriving at once (the app retries when the connection returns) must not
    remove the file or drop a plan. Regression test for a shared temp-file name."""
    import threading

    from farm_twin import plans
    from farm_twin.storage import atomic_write_text, read_json_safe

    target = tmp_path / "x.json"
    errors = []

    def write(i):
        try:
            atomic_write_text(target, json.dumps({"i": i}))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
    threads = [threading.Thread(target=write, args=(i,)) for i in range(16)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert not errors and target.exists() and read_json_safe(target)["i"] in range(16)
    assert not list(tmp_path.glob("*.tmp"))

    monkeypatch.setattr(plans, "PLANS_DIR", tmp_path / "plans")
    farm = config.PILOT_FARM["farm_id"]

    def sync(i):
        plans.merge(farm, [{"id": f"plan-{i:04d}-abcdef", "created_at": "2026-11-12T08:00:00Z",
                            "rotation": ["cowpea", "finger_millet", "cowpea"]}])
    threads = [threading.Thread(target=sync, args=(i,)) for i in range(12)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert (tmp_path / "plans" / f"{farm}.json").exists()
    assert len(plans.load(farm)) == 12

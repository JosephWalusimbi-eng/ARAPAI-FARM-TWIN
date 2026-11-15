"""Generic dataset readers (for challenge-day NASA resources) and the data snapshot.
All files are SYNTHETIC, written by the tests with known values at known places."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from farm_twin import config, snapshot  # noqa: E402
from farm_twin.sources import generic  # noqa: E402

LAT, LON = 1.71, 33.61


def grid_nc(path, lon0=30.0, lon_step=0.25, descending_lat=False):
    import xarray as xr
    lats = np.arange(0.0, 4.0, 0.25)
    if descending_lat:
        lats = lats[::-1]
    lons = np.arange(lon0, lon0 + 6.0, lon_step)
    times = pd.date_range("2020-01-01", periods=10, freq="D")
    # value encodes the cell: 1000*lat + lon + day, so the nearest cell is checkable
    data = (1000 * lats[None, :, None] + (lons[None, None, :] % 360) + np.arange(10)[:, None, None])
    ds = xr.Dataset({"NDVI": (("time", "lat", "lon"), data.astype("float32"), {"units": "1"})},
                    coords={"time": times, "lat": lats, "lon": lons})
    ds.to_netcdf(path)
    return path


def test_netcdf_nearest_cell_time_series(tmp_path):
    df, info = generic.read_point(str(grid_nc(tmp_path / "x.nc")), LAT, LON)
    assert (info["cell_latitude"], info["cell_longitude"]) == (1.75, 33.5)
    assert info["distance_km"] < 15 and info["time"] and info["units"]["NDVI"] == "1"
    assert len(df) == 10 and df["NDVI"].iloc[0] == pytest.approx(1000 * 1.75 + 33.5)


def test_netcdf_0_360_longitudes_and_descending_lat(tmp_path):
    p = grid_nc(tmp_path / "w.nc", lon0=330.0, descending_lat=True)  # 330..336 = -30..-24
    df, info = generic.read_point(str(p), LAT, -26.0)
    assert info["cell_longitude"] == -26.0 and info["cell_latitude"] == 1.75
    assert df["NDVI"].iloc[0] == pytest.approx(1000 * 1.75 + 334.0)


def test_geotiff_point_in_projected_crs(tmp_path):
    rasterio = generic._rasterio()
    from rasterio.transform import from_origin
    from rasterio.warp import transform
    path = tmp_path / "t.tif"
    xs, ys = transform("EPSG:4326", "EPSG:32636", [LON], [LAT])  # UTM 36N, metres
    tf = from_origin(xs[0] - 500, ys[0] + 500, 100, 100)        # 10x10 cells of 100 m around the farm
    data = np.arange(100, dtype="float32").reshape(10, 10)
    with rasterio.open(path, "w", driver="GTiff", width=10, height=10, count=1, dtype="float32",
                       crs="EPSG:32636", transform=tf, nodata=-9999) as r:
        r.write(data, 1)
        r.set_band_description(1, "ph")
    df, info = generic.read_point(str(path), LAT, LON)
    assert list(df.columns) == ["ph"] and df["ph"].iloc[0] == 55.0  # row 5, col 5
    assert info["distance_km"] < 0.1 and "32636" in info["crs"]
    with pytest.raises(generic.DatasetError):
        generic.read_point(str(path), 0.0, 0.0)


def test_csv_with_dates(tmp_path):
    p = tmp_path / "s.csv"
    p.write_text("# a comment\ndate,rain_mm,ndvi\n2024-01-01,1.5,0.4\n2024-01-02,0,0.41\n", encoding="utf-8")
    df, info = generic.read_point(str(p), LAT, LON)
    assert isinstance(df.index, pd.DatetimeIndex) and list(df.columns) == ["rain_mm", "ndvi"]


def test_unknown_format_and_missing_variable(tmp_path):
    with pytest.raises(generic.DatasetError):
        generic.kind_of("data.xyz")
    with pytest.raises(generic.DatasetError):
        generic.read_point(str(grid_nc(tmp_path / "x.nc")), LAT, LON, ["EVI"])


def test_generic_source_plugs_into_the_farm_table(tmp_path, monkeypatch):
    from farm_twin.sources import SOURCES, farm_table
    src = generic.GenericSource("ndvi", "Test NDVI", str(grid_nc(tmp_path / "x.nc")), ["NDVI"], "NDVI")
    monkeypatch.setitem(SOURCES, "ndvi", src)
    s = src.fetch(LAT, LON, "20200101", "20200110", cache_dir=tmp_path / "cache")
    assert list(s.df.columns) == ["NDVI_NDVI"] and len(s.df) == 10
    again = src.fetch(LAT, LON, "20200101", "20200110", cache_dir=tmp_path / "cache", cache_only=True)
    assert again.df.equals(s.df) or np.allclose(again.df.values, s.df.values)


def test_inspect_script_runs(tmp_path, capsys):
    import inspect_dataset
    assert inspect_dataset.main([str(grid_nc(tmp_path / "x.nc")), "--var", "NDVI"]) == 0
    out = capsys.readouterr().out
    assert "cell_latitude" in out and 'GenericSource("new"' in out


# --- snapshot -------------------------------------------------------------------------------

def test_snapshot_create_and_seed(tmp_path):
    cache, snap, fresh = tmp_path / "cache", tmp_path / "snap", tmp_path / "fresh"
    cache.mkdir()
    (cache / "power_s3_x.json").write_text(json.dumps({"a": 1}))
    (cache / "imerg_x.json").write_text(json.dumps({"b": 2}))
    (cache / "smap_granules_x.json").write_text("{}")   # download-only file: not snapshotted
    (cache / "soilgrids_bad.json").write_text("{broken")  # damaged: never snapshotted
    m = snapshot.create(cache, snap)
    assert sorted(f["name"] for f in m["files"]) == ["imerg_x.json", "power_s3_x.json"]
    assert sorted(snapshot.seed(fresh, snap)) == ["imerg_x.json", "power_s3_x.json"]
    assert json.loads((fresh / "power_s3_x.json").read_text()) == {"a": 1}
    (fresh / "imerg_x.json").write_text(json.dumps({"b": "newer local download"}))
    assert snapshot.seed(fresh, snap) == []  # local files always win
    assert json.loads((fresh / "imerg_x.json").read_text())["b"] == "newer local download"
    (snap / "power_s3_x.json").write_text("tampered")
    (fresh / "power_s3_x.json").unlink()
    assert snapshot.seed(fresh, snap) == []  # checksum mismatch: not installed


def test_committed_snapshot_is_complete():
    m = snapshot.manifest()
    assert m, "data/snapshot/MANIFEST.json missing: run python scripts/snapshot.py create"
    names = {f["name"] for f in m["files"]}
    for prefix in ("power_", "imerg_", "smap_raw_", "soilgrids_"):
        assert any(n.startswith(prefix) for n in names), prefix
    for f in m["files"]:
        assert (snapshot.SNAPSHOT_DIR / f["name"]).exists()


def test_snapshot_seeds_a_file_whose_line_endings_changed(tmp_path):
    """Windows writes CRLF; git on Windows commits LF. The host's copy must still pass the check
    (this silently dropped the soil data on the first Render deploy)."""
    from farm_twin import snapshot
    cache, snap = tmp_path / "cache", tmp_path / "snap"
    cache.mkdir(); snap.mkdir()
    (cache / "soilgrids_1.0000_2.0000.json").write_bytes(b'{\r\n "a": 1\r\n}\r\n')
    snapshot.create(cache, snap)
    # what the host sees after GitHub normalised the line endings
    (snap / "soilgrids_1.0000_2.0000.json").write_bytes(b'{\n "a": 1\n}\n')
    fresh = tmp_path / "fresh"
    assert snapshot.seed(fresh, snap) == ["soilgrids_1.0000_2.0000.json"]
    assert (fresh / "soilgrids_1.0000_2.0000.json").read_bytes().replace(b"\r\n", b"\n") == b'{\n "a": 1\n}\n'

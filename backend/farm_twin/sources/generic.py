"""Read any gridded or tabular dataset at the farm: for NASA resources released on challenge day.

Formats:  NetCDF / HDF5 (.nc .nc4 .cdf .h5 .hdf5 .he5), Zarr (.zarr, local or https),
          GeoTIFF / Cloud-Optimized GeoTIFF (.tif .tiff), CSV (.csv .txt).
Sources:  a local path, or an https URL (downloaded once to data/incoming/; Earthdata hosts use
          the Earthdata login from .env; Zarr URLs are read in place).

Triage on the day:  python scripts/inspect_dataset.py <path-or-url> [--var NAME]
Plug into the app:  register a GenericSource in sources/__init__.py (see its docstring).

Nearest grid cell, never interpolated; the cell centre and its distance from the farm are
reported so nobody mistakes a grid value for a plot measurement.
"""
from __future__ import annotations

import math
import os
import re
from pathlib import Path
from urllib.parse import urlparse

import numpy as np
import pandas as pd

from .. import config
from .base import NotFetched, PointSeries, Source, SourceError

INCOMING_DIR = config.ROOT / "data" / "incoming"
NETCDF = (".nc", ".nc4", ".cdf", ".h5", ".hdf5", ".he5", ".hdf")
GEOTIFF = (".tif", ".tiff")
TABLE = (".csv", ".txt")
LAT_NAMES = ("lat", "latitude", "y", "nav_lat", "Latitude", "LAT")
LON_NAMES = ("lon", "longitude", "x", "nav_lon", "Longitude", "LON")
TIME_NAMES = ("time", "t", "date", "Time", "TIME")


class DatasetError(SourceError):
    pass


def kind_of(src: str) -> str:
    s = str(src).lower().split("?")[0].rstrip("/")
    if s.endswith(".zarr") or ".zarr/" in s:
        return "zarr"
    if s.endswith(NETCDF) or "dap.nc4" in s:
        return "netcdf"
    if s.endswith(GEOTIFF):
        return "geotiff"
    if s.endswith(TABLE):
        return "csv"
    raise DatasetError(f"unknown format for {src!r}: expected NetCDF/HDF5, Zarr, GeoTIFF or CSV")


def is_url(src: str) -> bool:
    return str(src).startswith(("http://", "https://"))


def local_copy(url: str, dest_dir: Path | None = None) -> Path:
    """Download a file once (to .part, then renamed) and return its path. Earthdata hosts get
    the Earthdata login; credentials go to no other host."""
    dest_dir = Path(dest_dir or INCOMING_DIR)
    dest_dir.mkdir(parents=True, exist_ok=True)
    name = re.sub(r"[^\w.\-]", "_", Path(urlparse(url).path).name or "download")
    dest = dest_dir / name
    if dest.exists():
        return dest
    part = dest.with_name(dest.name + ".part")
    host = urlparse(url).hostname or ""
    if host.endswith((".earthdata.nasa.gov", ".earthdatacloud.nasa.gov")):
        from .earthdata import EarthdataSession
        session = EarthdataSession(timeout=600)
        try:
            resp = session.get(url)  # handles the Earthdata login redirects
            resp.raise_for_status()
            part.write_bytes(resp.content)
        finally:
            session.close()
    else:
        import httpx
        with httpx.Client(timeout=httpx.Timeout(30, read=600), follow_redirects=True) as c:
            with c.stream("GET", url) as r:
                r.raise_for_status()
                with open(part, "wb") as f:
                    for chunk in r.iter_bytes(1 << 20):
                        f.write(chunk)
    part.replace(dest)
    return dest


# --- xarray formats (NetCDF / HDF5 / Zarr) ------------------------------------------------

def open_xarray(src: str):
    import xarray as xr
    kind = kind_of(src)
    if kind == "zarr":
        if is_url(src):
            import fsspec
            return xr.open_zarr(fsspec.get_mapper(src), consolidated=None)
        return xr.open_zarr(src, consolidated=None)
    path = local_copy(src) if is_url(src) else Path(src)
    try:
        return xr.open_dataset(path, engine="netcdf4")
    except Exception as exc:  # e.g. HDF-EOS with groups: say which groups exist
        groups = _hdf5_groups(path)
        raise DatasetError(f"could not open {path.name} as NetCDF ({exc}). HDF5 groups: {groups[:20]}") from exc


def _hdf5_groups(path: Path) -> list[str]:
    try:
        import h5py
        out = []
        with h5py.File(path, "r") as f:
            f.visit(lambda name: out.append(name))
        return out
    except Exception:
        return []


def _coord(ds, names) -> str | None:
    for n in names:
        if n in ds.coords or n in ds.variables:
            return n
    return None


def _wrap_lon(lon: float, lons: np.ndarray) -> float:
    """Match the dataset's longitude convention (-180..180 or 0..360)."""
    if np.nanmax(lons) > 180 and lon < 0:
        return lon + 360
    return lon


def _haversine_km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(a))


def point_from_xarray(ds, lat: float, lon: float, variables: list[str] | None = None):
    """Nearest-cell values at (lat, lon) -> (DataFrame, info)."""
    la, lo = _coord(ds, LAT_NAMES), _coord(ds, LON_NAMES)
    if not (la and lo):
        raise DatasetError(f"no latitude/longitude coordinates found; coords: {list(ds.coords)}")
    lats, lons = np.asarray(ds[la].values), np.asarray(ds[lo].values)
    lon_q = _wrap_lon(lon, lons)
    if lats.ndim == 1 and lons.ndim == 1:
        sel = {la: lats[np.abs(lats - lat).argmin()], lo: lons[np.abs(lons - lon_q).argmin()]}
        pt = ds.sel(sel)
        cell_lat, cell_lon = float(sel[la]), float(sel[lo])
    else:  # 2-D (curvilinear or swath) coordinates
        d = (lats - lat) ** 2 + ((lons - lon_q) * math.cos(math.radians(lat))) ** 2
        idx = np.unravel_index(np.nanargmin(d), d.shape)
        dims = ds[la].dims
        pt = ds.isel({dims[0]: idx[0], dims[1]: idx[1]})
        cell_lat, cell_lon = float(lats[idx]), float(lons[idx])
    names = variables or [v for v in ds.data_vars if ds[v].ndim >= 1 and np.issubdtype(ds[v].dtype, np.number)]
    missing = [v for v in names if v not in ds.data_vars]
    if missing:
        raise DatasetError(f"variables not found: {missing}; available: {list(ds.data_vars)}")
    t = _coord(ds, TIME_NAMES)
    cols, units = {}, {}
    for v in names:
        arr = pt[v]
        extra = [d for d in arr.dims if d != t]
        if extra:  # e.g. a band or depth dimension left: take the first level, say so
            arr = arr.isel({d: 0 for d in extra})
        cols[v] = np.atleast_1d(arr.values).astype(float)
        units[v] = str(ds[v].attrs.get("units", ""))
    index = pd.DatetimeIndex(pd.to_datetime(np.atleast_1d(pt[t].values)), name="date") if t in pt.coords else None
    df = pd.DataFrame(cols, index=index)
    info = {"cell_latitude": round(cell_lat, 5), "cell_longitude": round(((cell_lon + 180) % 360) - 180, 5),
            "distance_km": round(_haversine_km(lat, lon, cell_lat, ((cell_lon + 180) % 360) - 180), 2),
            "units": units, "time": t is not None and t in pt.coords}
    return df, info


# --- GeoTIFF / COG ----------------------------------------------------------------------

def _rasterio():
    """Import rasterio using ITS OWN projection data. Other software (e.g. PostgreSQL/PostGIS on
    Windows) may set PROJ_LIB / GDAL_DATA system-wide to incompatible copies; we override them
    for this process only, never system-wide."""
    import importlib.util
    spec = importlib.util.find_spec("rasterio")
    if spec and spec.origin:
        base = Path(spec.origin).parent
        if (base / "proj_data" / "proj.db").exists():
            os.environ["PROJ_DATA"] = os.environ["PROJ_LIB"] = str(base / "proj_data")
        if (base / "gdal_data").exists():
            os.environ["GDAL_DATA"] = str(base / "gdal_data")
    import rasterio
    return rasterio


def point_from_geotiff(src: str, lat: float, lon: float):
    rasterio = _rasterio()
    from rasterio.warp import transform
    path = local_copy(src) if is_url(src) else Path(src)
    with rasterio.open(path) as r:
        xs, ys = transform("EPSG:4326", r.crs, [lon], [lat]) if r.crs else ([lon], [lat])
        row, col = r.index(xs[0], ys[0])
        if not (0 <= row < r.height and 0 <= col < r.width):
            raise DatasetError(f"the farm ({lat}, {lon}) is outside this raster's extent {r.bounds}")
        vals = [float(v) for v in next(r.sample([(xs[0], ys[0])]))]
        nod = r.nodata
        names = [d or f"band_{i + 1}" for i, d in enumerate(r.descriptions)]
        cols = {n: [np.nan if (nod is not None and v == nod) else v] for n, v in zip(names, vals)}
        cx, cy = r.xy(row, col)
        clon, clat = transform(r.crs, "EPSG:4326", [cx], [cy]) if r.crs else ([cx], [cy])
        info = {"cell_latitude": round(clat[0], 5), "cell_longitude": round(clon[0], 5),
                "distance_km": round(_haversine_km(lat, lon, clat[0], clon[0]), 3),
                "crs": str(r.crs), "resolution": r.res, "units": {n: (r.units[i] or "") for i, n in enumerate(names)},
                "time": False}
    return pd.DataFrame(cols), info


# --- CSV ------------------------------------------------------------------------------------

def table(src: str) -> pd.DataFrame:
    path = local_copy(src) if is_url(src) else Path(src)
    df = pd.read_csv(path, comment="#")
    for c in df.columns:  # first column that parses as dates becomes the index
        if df[c].dtype == object or "date" in c.lower() or "time" in c.lower():
            parsed = pd.to_datetime(df[c], errors="coerce")
            if parsed.notna().mean() > 0.9:
                return df.drop(columns=[c]).set_index(pd.DatetimeIndex(parsed, name="date")).sort_index()
    return df


# --- one entry point -------------------------------------------------------------------------

def read_point(src: str, lat: float, lon: float, variables: list[str] | None = None):
    """(DataFrame, info) for the farm from any supported source."""
    kind = kind_of(src)
    if kind in ("netcdf", "zarr"):
        df, info = point_from_xarray(open_xarray(src), lat, lon, variables)
    elif kind == "geotiff":
        df, info = point_from_geotiff(src, lat, lon)
    else:
        df, info = table(src), {"note": "table: no grid, values as given"}
        if variables:
            df = df[variables]
    info.update({"source": str(src), "format": kind})
    return df, info


def describe(src: str) -> dict:
    """What is inside: variables, dimensions, units, time and space ranges."""
    kind = kind_of(src)
    if kind in ("netcdf", "zarr"):
        ds = open_xarray(src)
        t, la, lo = _coord(ds, TIME_NAMES), _coord(ds, LAT_NAMES), _coord(ds, LON_NAMES)
        out = {"format": kind, "dims": dict(ds.sizes),
               "variables": {v: {"dims": list(ds[v].dims), "units": ds[v].attrs.get("units", ""),
                                 "long_name": ds[v].attrs.get("long_name", "")} for v in ds.data_vars},
               "attrs": {k: str(v)[:120] for k, v in list(ds.attrs.items())[:12]}}
        if t:
            tv = pd.to_datetime(np.atleast_1d(ds[t].values))
            out["time"] = [str(tv.min()), str(tv.max()), int(tv.size)]
        if la and lo:
            out["lat_range"] = [float(np.nanmin(ds[la])), float(np.nanmax(ds[la]))]
            out["lon_range"] = [float(np.nanmin(ds[lo])), float(np.nanmax(ds[lo]))]
        return out
    if kind == "geotiff":
        rasterio = _rasterio()
        path = local_copy(src) if is_url(src) else Path(src)
        with rasterio.open(path) as r:
            return {"format": kind, "size": [r.width, r.height], "bands": r.count, "crs": str(r.crs),
                    "bounds": list(r.bounds), "resolution": list(r.res), "dtype": r.dtypes[0],
                    "nodata": r.nodata, "descriptions": list(r.descriptions), "tags": dict(list(r.tags().items())[:12])}
    df = table(src)
    return {"format": kind, "columns": list(df.columns), "rows": len(df),
            "index": [str(df.index.min()), str(df.index.max())] if isinstance(df.index, pd.DatetimeIndex) else None}


class GenericSource(Source):
    """Plug an incoming dataset into the app as a daily source.

        SOURCES["ndvi"] = GenericSource("ndvi", "MODIS NDVI (16-day)", "https://.../file.nc",
                                        variables=["NDVI"], prefix="NDVI")

    Columns become <PREFIX>_<VAR>; values are read once and cached like every other source.
    Time series only (data with a time axis); single-date rasters belong in the farm profile.
    """

    def __init__(self, key: str, label: str, src: str, variables: list[str] | None = None,
                 prefix: str | None = None):
        self.key, self.label, self.src, self.variables = key, label, src, variables
        self.prefix = (prefix or key).upper()
        self.columns = [f"{self.prefix}_{v}" for v in (variables or [])]

    def fetch(self, lat, lon, start, end, *, refresh=False, cache_only=False,
              cache_dir: Path | None = None, log=None) -> PointSeries:
        cache_dir = cache_dir or config.CACHE_DIR
        if not refresh:
            cached = self.load_cached(lat, lon, start, end, cache_dir)
            if cached:
                return cached
        if cache_only:
            raise NotFetched(f"{self.key}: not fetched yet (python scripts/fetch_sources.py {self.key})")
        df, info = read_point(self.src, lat, lon, self.variables)
        if not isinstance(df.index, pd.DatetimeIndex):
            raise DatasetError(f"{self.key}: no time axis; put single-date values in the farm profile instead")
        df = df.loc[pd.Timestamp(start):pd.Timestamp(end) + pd.Timedelta(days=1)]
        df = df.rename(columns={c: f"{self.prefix}_{c}" for c in df.columns})
        df.index = df.index.normalize()
        df = df.groupby(level=0).mean()
        units = {f"{self.prefix}_{k}": v for k, v in (info.get("units") or {}).items()}
        series = PointSeries(self.key, df, units, {**info, "label": self.label})
        cache_dir.mkdir(parents=True, exist_ok=True)
        self.save(series, lat, lon, start, end, cache_dir)
        return series

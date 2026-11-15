"""Read one grid cell's daily time series from a public Zarr store over HTTPS.

Reads in time blocks aligned to the store's chunks, with a short stall timeout and
retries per block, because some networks drop long transfers. Each finished block is
checkpointed to disk, so after a crash or power cut a rerun skips completed blocks.
"""
from __future__ import annotations

import hashlib
import io
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import pandas as pd

from .. import config
from ..storage import atomic_write_text


class ZarrReadError(RuntimeError):
    pass


@dataclass
class ZarrPoint:
    df: pd.DataFrame              # daily, DatetimeIndex "date", one column per variable
    units: dict[str, str]
    attrs: dict[str, dict]
    cell_lat: float
    cell_lon: float
    checkpoint: Path | None = None

    def discard_checkpoint(self) -> None:
        """Call after the final result is safely saved; removes per-block files."""
        if self.checkpoint is not None:
            shutil.rmtree(self.checkpoint, ignore_errors=True)


def _open(store: str):
    import xarray as xr
    try:
        import aiohttp
        opts = {"client_kwargs": {"timeout": aiohttp.ClientTimeout(
            total=None, sock_connect=30, sock_read=90)}}
    except ImportError:  # pragma: no cover
        opts = {}
    try:
        return xr.open_zarr(store, consolidated=True, storage_options=opts)
    except TypeError:  # test doubles / older xarray without storage_options
        return xr.open_zarr(store, consolidated=True)


def _load_block(block) -> pd.DataFrame:
    """Download one time block (the only network-heavy step)."""
    loaded = block.load()
    return pd.DataFrame({v: np.asarray(loaded[v].values, dtype=float) for v in loaded.data_vars},
                        index=pd.DatetimeIndex(pd.to_datetime(loaded["time"].values), name="date"))


def checkpoint_dir(store: str, variables: list[str], lat: float, lon: float,
                   start: str, end: str, root: Path | None = None) -> Path:
    key = f"{store}|{','.join(sorted(variables))}|{lat:.4f}|{lon:.4f}|{start}|{end}"
    return (root or config.CACHE_DIR / "partial") / hashlib.sha1(key.encode()).hexdigest()[:16]


def read_point(store: str, variables: list[str], lat: float, lon: float,
               start: str, end: str, *, retries: int = 4, log=None,
               checkpoint_root: Path | None = None) -> ZarrPoint:
    """Nearest-cell daily values for `variables` between YYYYMMDD `start` and `end`."""
    try:
        ds = _open(store)
    except Exception as exc:
        raise ZarrReadError(f"cannot open {store}: {exc}") from exc

    available = [v for v in variables if v in ds.data_vars]
    if not available:
        raise ZarrReadError(f"none of {variables} in {store}")
    point = ds[available].sel(lat=lat, lon=lon, method="nearest")
    t0, t1 = pd.to_datetime(start, format="%Y%m%d"), pd.to_datetime(end, format="%Y%m%d")
    point = point.sel(time=slice(t0, t1))

    # Block size = the store's time chunk length, so each block is one chunk download.
    n = point.sizes["time"]
    chunk = (ds[available[0]].encoding.get("chunks") or (n,))[0] or n
    nblocks = -(-n // chunk) if n else 0
    ckpt = checkpoint_dir(store, available, lat, lon, start, end, checkpoint_root)

    parts = []
    for b in range(nblocks):
        part_file = ckpt / f"block_{b:03d}.csv"
        expected_rows = min((b + 1) * chunk, n) - b * chunk
        if part_file.exists():
            try:
                saved = pd.read_csv(part_file, index_col="date", parse_dates=["date"])
                if len(saved) == expected_rows and set(available) <= set(saved.columns):
                    parts.append(saved)
                    if log:
                        log(f"  block {b + 1}/{nblocks} already downloaded (resumed)")
                    continue
            except (ValueError, pd.errors.ParserError, pd.errors.EmptyDataError, OSError):
                pass
            part_file.unlink(missing_ok=True)  # incomplete (e.g. cut off by a crash): redo it
        block = point.isel(time=slice(b * chunk, min((b + 1) * chunk, n)))
        for attempt in range(retries):
            try:
                frame = _load_block(block)
                break
            except Exception as exc:  # network stalls surface as many exception types
                if attempt == retries - 1:
                    raise ZarrReadError(f"read failed after {retries} tries "
                                        f"({b} of {nblocks} blocks saved; rerun to resume): {exc!r}") from exc
                if log:
                    log(f"  retrying block {b + 1} ({exc.__class__.__name__})")
                time.sleep(2 ** attempt)
        buf = io.StringIO()
        frame.to_csv(buf)
        atomic_write_text(part_file, buf.getvalue())
        parts.append(frame)
        if log:
            log(f"  block {b + 1}/{nblocks} done")

    df = pd.concat(parts).sort_index() if parts else pd.DataFrame(columns=available)
    df.index.name = "date"
    df = df[available].astype(float)
    for v in available:
        vmin = ds[v].attrs.get("valid_min")
        if vmin is not None:
            df.loc[df[v] < float(vmin), v] = np.nan  # fill values and impossible negatives
    df = df.mask(df <= -999.0)
    return ZarrPoint(
        df=df,
        units={v: ds[v].attrs.get("units", "") for v in available},
        attrs={v: dict(ds[v].attrs) for v in available},
        cell_lat=float(point["lat"]),
        cell_lon=float(point["lon"]),
        checkpoint=ckpt,
    )

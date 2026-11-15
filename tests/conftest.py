"""Shared test setup: no test may read or write the real data folders.

Downloaded NASA/SoilGrids data in data/cache/ and synced plans in data/plans/ differ from
machine to machine; tests use empty temporary folders instead, so results never depend on
what has been downloaded.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from farm_twin import config, plans  # noqa: E402


@pytest.fixture(autouse=True)
def isolated_data_dirs(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    monkeypatch.setattr(plans, "PLANS_DIR", tmp_path / "plans")
    monkeypatch.setattr(config, "CHANNELS_DIR", tmp_path / "channels")
    yield

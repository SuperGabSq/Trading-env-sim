import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import pytest

import sim.data.store as store_mod
from sim.data.store import BarStore


@pytest.fixture()
def tmp_store(tmp_path, monkeypatch):
    s = BarStore(tmp_path / "bars")
    monkeypatch.setattr(store_mod, "_store", s)
    return s

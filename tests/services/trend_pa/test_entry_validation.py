"""Executable entry regressions. All writes and order collaborators are fakes."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from backend.src.services.trend_pa import service
from backend.src.services.trend_pa.strategy import Setup


@pytest.mark.parametrize("direction,entry,level,stop,paid", [
    ("BUY", 100, 99, 95, 120), ("SELL", 100, 101, 105, 80)])
def test_executable_fill_cannot_bypass_the_chase_limit(monkeypatch, direction, entry, level, stop, paid):
    setup = Setup(direction, "pin", entry, stop, 110, 5, level,
                  "swing_low", "london", 10, 40, {"risk_atr": .5})
    e, inserted, refused = engine(monkeypatch, paid)
    asyncio.run(e._open(setup, 1))
    assert inserted == []
    assert "chasing" in refused[-1]


def engine(monkeypatch, paid):
    inserted, refused = [], []
    e = service.TrendPAEngine.__new__(service.TrendPAEngine)
    e._bridge = SimpleNamespace(get_tick=AsyncMock(return_value=SimpleNamespace(ask=paid, bid=paid)))
    e.model = SimpleNamespace(predict=lambda f: None)
    monkeypatch.setattr(service.repo, "insert_signal", lambda s: inserted.append(s) or 1)
    monkeypatch.setattr(service.repo, "log_analysis", refused.append)
    monkeypatch.setattr(service, "_live_settings", lambda: {"tpa_live_execution": 0})
    return e, inserted, refused


def test_accepted_fill_records_actual_risk_and_level_distance(monkeypatch):
    setup = Setup("BUY", "pin", 100, 95, 110, 5, 99,
                  "swing_low", "london", 10, 40, {"risk_atr": .5, "level_dist_atr": .1})
    e, inserted, _ = engine(monkeypatch, 101)
    asyncio.run(e._open(setup, 1))
    assert inserted[0]["features"]["risk_atr"] == pytest.approx(.6)
    assert inserted[0]["features"]["level_dist_atr"] == pytest.approx(.2)
    assert setup.features == {"risk_atr": .5, "level_dist_atr": .1}


@pytest.mark.parametrize("paid", [94, float("nan"), float("inf")])
def test_invalid_fill_never_creates_a_virtual_or_real_trade(monkeypatch, paid):
    setup = Setup("BUY", "pin", 100, 95, 110, 5, 99, "swing_low", "london", 10, 40)
    e, inserted, _ = engine(monkeypatch, paid)
    asyncio.run(e._open(setup, 1))
    assert inserted == []

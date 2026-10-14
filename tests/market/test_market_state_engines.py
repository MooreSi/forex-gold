"""docs/todo/017 item 4 through each engine's real repo (temp databases): the
sweep writes `market_state_json` on that engine's own rows, and a measured row
is never selected again.

A fake bridge; nothing reaches a broker.
"""
from __future__ import annotations

import asyncio
import json
import os
import tempfile

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo as bo_repo
from backend.src.services.breakout_signal import market_state_repo as bo_state
from backend.src.services.breakout_signal import market_state_sweep as bo_sweep
from backend.src.services.reversal_engine import market_state_repo as re_state
from backend.src.services.reversal_engine import market_state_sweep as re_sweep
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file

H = 3600.0


class _Bridge:
    def __init__(self):
        self.calls = 0

    async def get_candles_range_for_symbol(self, symbol, lo, hi, tf):
        self.calls += 1
        start = lo - (lo % H)
        n = int((hi - start) // H) + 1
        return [{"ts": start + i * H, "open": 100.0 + i, "close": 101.0 + i,
                 "high": 101.5 + i, "low": 99.5 + i} for i in range(n)]


@pytest.fixture
def re_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    yield re_repo
    re_repo.close_db()
    remove_db_file(path)


@pytest.fixture
def bo_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    bo_repo.init(path)
    yield bo_repo
    bo_repo.close_db()
    remove_db_file(path)


def _state(db, table, sid):
    row = db.get_db().get(f"SELECT market_state_json FROM {table} WHERE id=?", sid)
    return json.loads(row[0]) if row and row[0] else None


class TestReversal:
    def test_a_signal_is_measured_once(self, re_db):
        sid = re_db.create_signal({"signal_ref": "RE-1", "direction": "BUY",
                                   "sl_dist": 5.0, "entry_low": 1.0,
                                   "entry_high": 1.0, "stop_loss": 0.5})
        assert [r["id"] for r in re_state.signals_missing_state()] == [sid]
        b = _Bridge()
        assert asyncio.run(re_sweep.market_state_sweep(None, bridge=b)) == 1
        s = _state(re_db, "re_signals", sid)
        assert s["h1_regime"] == "trend" and s["h1_side"] == "with"
        assert re_state.signals_missing_state() == []
        assert asyncio.run(re_sweep.market_state_sweep(None, bridge=b)) == 0
        assert b.calls == 1

    def test_no_running_engine_means_no_read(self, re_db):
        re_db.create_signal({"signal_ref": "RE-2", "direction": "SELL",
                             "sl_dist": 5.0, "entry_low": 1.0,
                             "entry_high": 1.0, "stop_loss": 1.5})
        n = asyncio.run(re_sweep.market_state_sweep(None, bridge_getter=lambda: None))
        assert n == 0 and len(re_state.signals_missing_state()) == 1


class TestBreakout:
    def test_a_signal_is_measured_once(self, bo_db):
        sid = bo_db.create_signal({"signal_ref": "BO-1", "direction": "SELL",
                                   "breakout_type": "go", "entry_mid": 100.0,
                                   "stop_loss": 101.0})
        assert [r["id"] for r in bo_state.signals_missing_state()] == [sid]
        assert asyncio.run(bo_sweep.market_state_sweep(None, bridge=_Bridge())) == 1
        s = _state(bo_db, "bo_signals", sid)
        assert s["h1_side"] == "against"
        assert bo_state.signals_missing_state() == []

    def test_without_the_engine_running_there_is_no_bridge(self, bo_db, monkeypatch):
        from backend.src.services.breakout_signal import breakout_signal_service as svc
        monkeypatch.setattr(svc, "get_instance", lambda: None)
        assert bo_sweep._engine_bridge() is None

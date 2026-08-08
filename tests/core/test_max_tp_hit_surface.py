"""Proves backend.src.services.positions.max_tp's extracted functions behave
identically to SimulationEngine's originals, characterized in
test_max_tp_hit_characterization.py -- see
docs/todo/refactor/core-max-tp-hit-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class.
No real or demo MT5 order is ever placed, closed, or modified -- this
module's functions never call an order-placing collaborator at all.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.positions import max_tp as maxtp


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, candles=None):
        self._candles = candles if candles is not None else []
        self.calls = []

    async def get_candles_range(self, start, end, tf):
        self.calls.append((start, end, tf))
        return self._candles


def _insert_trade(trade_id, direction="BUY", open_time=None, close_time=None,
                   tp1=2410.0, tp2=None, sig_tp1=None, sig_tp2=None,
                   max_tp_hit=None, net_pnl=10.0, strategy="scale_out",
                   tg_source="chan1", mt5_ticket=555):
    open_time = time.time() - 5000 if open_time is None else open_time
    close_time = time.time() - 2000 if close_time is None else close_time
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at, tp1, tp2) VALUES (?,?,?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, 2400.0, 2400.0, 2390.0, "active",
             time.time(), sig_tp1, sig_tp2),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time, tp1, tp2, max_tp_hit, net_pnl, strategy, "
            "tg_source, mt5_ticket) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", direction, 2400.0, 2400.0, 2400.0, 0.10, 0.0,
             2390.0, "closed", open_time, close_time, tp1, tp2, max_tp_hit, net_pnl,
             strategy, tg_source, mt5_ticket),
        )


def _max_tp_hit(trade_id):
    with db.db() as conn:
        row = conn.execute(
            "SELECT max_tp_hit FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()
    return row[0]


# ── _tp_level_from_extreme (pure helper) ────────────────────────────────

def test_tp_level_buy_highest_reached():
    tps = {1: 2405.0, 2: 2410.0, 3: 2415.0}
    assert maxtp._tp_level_from_extreme("BUY", 2412.0, lambda i: tps.get(i)) == "TP2"


def test_tp_level_sell_highest_reached():
    tps = {1: 2395.0, 2: 2390.0, 3: 2385.0}
    assert maxtp._tp_level_from_extreme("SELL", 2388.0, lambda i: tps.get(i)) == "TP2"


def test_tp_level_none_mid_ladder_not_treated_as_stop():
    tps = {1: 2405.0, 2: None, 3: 2415.0}
    assert maxtp._tp_level_from_extreme("BUY", 2420.0, lambda i: tps.get(i)) == "TP3"


def test_tp_level_none_reached():
    tps = {1: 2405.0}
    assert maxtp._tp_level_from_extreme("BUY", 2400.0, lambda i: tps.get(i)) == "none"


# ── max_tp_checker_sweep ─────────────────────────────────────────────────

def test_missing_open_time_saves_none_directly_no_candle_fetch(fresh_db):
    _insert_trade("t-missing", open_time=0.0, close_time=time.time() - 2000)
    bridge = _FakeBridge()
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed") as mock_push:
        asyncio.run(maxtp.max_tp_checker_sweep(bridge))
    assert _max_tp_hit("t-missing") == "none"
    assert bridge.calls == []
    mock_push.assert_not_called()


def test_normal_buy_prefers_sig_tp_over_tp_and_pushes_ledger(fresh_db):
    _insert_trade("t-normal", direction="BUY", tp1=2405.0, sig_tp1=2408.0,
                  sig_tp2=2420.0, net_pnl=15.0, tg_source="chan1", mt5_ticket=777)
    bridge = _FakeBridge(candles=[{"high": 2415.0, "low": 2398.0}, {"high": 2422.0, "low": 2400.0}])
    pushed = []
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed", side_effect=lambda d: pushed.append(dict(d))):
        asyncio.run(maxtp.max_tp_checker_sweep(bridge))
    assert _max_tp_hit("t-normal") == "TP2"
    assert len(pushed) == 1
    assert pushed[0]["max_tp_hit"] == "TP2"
    assert pushed[0]["pnl_dollars"] == 15.0
    assert pushed[0]["outcome"] == "win"
    assert pushed[0]["mt5_ticket"] == 777
    assert pushed[0]["tg_source"] == "chan1"


def test_no_candles_skips_no_save_no_push(fresh_db):
    _insert_trade("t-nocandles", tp1=2405.0)
    bridge = _FakeBridge(candles=[])
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed") as mock_push:
        asyncio.run(maxtp.max_tp_checker_sweep(bridge))
    assert _max_tp_hit("t-nocandles") is None
    mock_push.assert_not_called()


def test_per_trade_exception_does_not_stop_loop(fresh_db):
    _insert_trade("t-bad", tp1=2405.0)
    _insert_trade("t-good", tp1=2405.0)

    class _RaisingBridge(_FakeBridge):
        async def get_candles_range(self, start, end, tf):
            self.calls.append((start, end, tf))
            if len(self.calls) == 1:
                raise RuntimeError("boom")
            return [{"high": 2410.0, "low": 2398.0}]

    bridge = _RaisingBridge()
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed"):
        asyncio.run(maxtp.max_tp_checker_sweep(bridge))
    assert _max_tp_hit("t-bad") is None
    assert _max_tp_hit("t-good") == "TP1"


def test_ledger_push_exception_swallowed_save_still_happens(fresh_db):
    _insert_trade("t-e", direction="SELL", tp1=2395.0)
    bridge = _FakeBridge(candles=[{"high": 2402.0, "low": 2390.0}])
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed", side_effect=RuntimeError("ledger down")):
        asyncio.run(maxtp.max_tp_checker_sweep(bridge))
    assert _max_tp_hit("t-e") == "TP1"


def test_net_pnl_outcome_thresholds(fresh_db):
    _insert_trade("t-be", tp1=2405.0, net_pnl=0.0)
    _insert_trade("t-loss", tp1=2405.0, net_pnl=-3.0)
    bridge = _FakeBridge(candles=[{"high": 2410.0, "low": 2398.0}])
    pushed = []
    with mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed", side_effect=lambda d: pushed.append(dict(d))):
        asyncio.run(maxtp.max_tp_checker_sweep(bridge))
    by_id = {p["trade_id"]: p["outcome"] for p in pushed}
    assert by_id["t-be"] == "be"
    assert by_id["t-loss"] == "loss"


# ── backfill_max_tp_hit_corrected ───────────────────────────────────────

def test_backfill_skips_when_recomputed_matches_stored(fresh_db):
    _insert_trade("t-same", tp1=2405.0, max_tp_hit="TP1", net_pnl=5.0)
    bridge = _FakeBridge(candles=[{"high": 2408.0, "low": 2398.0}])
    with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
         mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed") as mock_push:
        asyncio.run(maxtp.backfill_max_tp_hit_corrected(bridge))
    assert _max_tp_hit("t-same") == "TP1"
    mock_push.assert_not_called()


def test_backfill_saves_and_pushes_when_recomputed_differs(fresh_db):
    _insert_trade("t-diff", tp1=2405.0, sig_tp2=2415.0, max_tp_hit="TP1", net_pnl=5.0)
    bridge = _FakeBridge(candles=[{"high": 2418.0, "low": 2398.0}])
    pushed = []
    with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
         mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed", side_effect=lambda d: pushed.append(dict(d))):
        asyncio.run(maxtp.backfill_max_tp_hit_corrected(bridge))
    assert _max_tp_hit("t-diff") == "TP2"
    assert len(pushed) == 1
    assert pushed[0]["max_tp_hit"] == "TP2"


def test_backfill_fetch_failure_returns_cleanly(fresh_db):
    _insert_trade("t-x", tp1=2405.0, max_tp_hit="TP1")
    bridge = _FakeBridge()
    with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
         mock.patch.object(db, "get_trades_with_max_tp_set", side_effect=RuntimeError("db down")):
        asyncio.run(maxtp.backfill_max_tp_hit_corrected(bridge))  # must not raise
    assert _max_tp_hit("t-x") == "TP1"  # untouched


def test_backfill_per_trade_exception_does_not_stop_loop(fresh_db):
    _insert_trade("t-bad", tp1=2405.0, max_tp_hit="TP1")
    _insert_trade("t-good", tp1=2405.0, sig_tp2=2415.0, max_tp_hit="TP1")

    class _RaisingBridge(_FakeBridge):
        async def get_candles_range(self, start, end, tf):
            self.calls.append((start, end, tf))
            if len(self.calls) == 1:
                raise RuntimeError("boom")
            return [{"high": 2418.0, "low": 2398.0}]

    bridge = _RaisingBridge()
    with mock.patch("asyncio.sleep", new=mock.AsyncMock()), \
         mock.patch("backend.src.services.cluster.sync.ledger.push_trade_closed"):
        asyncio.run(maxtp.backfill_max_tp_hit_corrected(bridge))
    assert _max_tp_hit("t-bad") == "TP1"  # untouched, exception before save
    assert _max_tp_hit("t-good") == "TP2"

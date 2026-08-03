"""Characterizes _get_triggered_tps/_last_closed_tp/_log_tp_wait_diagnostic/
_check_sl/_check_tp_hits/_get_remaining_lots on SimulationEngine
(core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-tp-trigger-tracking-migration/010-*.md.

_get_triggered_tps and _log_tp_wait_diagnostic read/write self._tp_cache/
self._tp_wait_log_ts. _check_tp_hits also calls self.get_triggered_tps
internally, so a bare stand-in object won't do -- these need a real
SimulationEngine instance (SimulationEngine.__new__(SimulationEngine), same
as pack 1's Risk Governor tests) with _tp_cache/_tp_wait_log_ts set manually
since __init__ never runs.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.services.positions.monitor_loop import check_sl

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    """to_db_thread() (used by _get_triggered_tps) runs on db._db_executor, a
    PERSISTENT single-worker ThreadPoolExecutor with its own threading.local()
    storage -- separate from the test's own thread. Resetting
    db._thread_local from the test thread has zero effect on the worker
    thread's cached connection, so a "fresh" temp DB would silently keep
    serving an earlier test's (deleted) file to any to_db_thread() call.
    Submit the reset function to run ON that worker thread instead."""
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


@pytest.fixture
def engine(fresh_db):
    """A real SimulationEngine instance (correct method binding for
    self.get_triggered_tps calls inside _check_tp_hits) without running
    __init__ (which would construct a live MT5 bridge) -- same pattern as
    pack 1's Risk Governor tests. __init__ never runs, so instance state
    normally set there (_tp_trigger_cache, a core_tp_trigger_tracking.TPCache
    bundling the old separate _tp_cache/_tp_wait_log_ts dicts since engine.py
    was wired to delegate to the extracted module) is set manually."""
    from backend.src.services.positions.tp_tracking import TPCache
    e = SimulationEngine.__new__(SimulationEngine)
    e._tp_trigger_cache = TPCache()
    return e


def _tick(bid: float, ask: float):
    return SimpleNamespace(bid=bid, ask=ask)


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", remaining_lots=0.10, **tps):
    with db.db() as conn:
        cols = "trade_id, signal_id, direction, entry_low, entry_high, entry_price, " \
               "lot_size, remaining_lots, stop_loss, status, open_time"
        vals = [trade_id, sig_id, direction, 2399.0, 2401.0, 2400.0, 0.10,
                remaining_lots, 2390.0, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _insert_partial_close(trade_id, reason, lots_closed=0.05, ts=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id, ts, lots_closed, close_price, pnl, reason) "
            "VALUES (?,?,?,?,?,?)",
            (trade_id, ts or time.time(), lots_closed, 2405.0, 25.0, reason),
        )


# ── _get_triggered_tps ────────────────────────────────────────────────────────

def test_get_triggered_tps_parses_reason_strings(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")
    _insert_partial_close("t-1", "TP3")

    triggered = asyncio.run(SimulationEngine.get_triggered_tps(engine, "t-1"))
    assert triggered == {1, 3}


def test_get_triggered_tps_ignores_non_matching_reasons(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "manual_close")

    triggered = asyncio.run(SimulationEngine.get_triggered_tps(engine, "t-1"))
    assert triggered == set()


def test_get_triggered_tps_ttl_cache_returns_stale_within_window(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")

    first = asyncio.run(SimulationEngine.get_triggered_tps(engine, "t-1"))
    assert first == {1}

    _insert_partial_close("t-1", "TP2")
    second = asyncio.run(SimulationEngine.get_triggered_tps(engine, "t-1"))
    assert second == {1}  # still cached, TP2 not yet visible


def test_get_triggered_tps_reloads_after_ttl_expiry(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1")
    _insert_partial_close("t-1", "TP1")

    asyncio.run(SimulationEngine.get_triggered_tps(engine, "t-1"))

    _insert_partial_close("t-1", "TP2")
    cached_set, _ = engine._tp_trigger_cache.triggered["t-1"]
    engine._tp_trigger_cache.triggered["t-1"] = (cached_set, time.time() - 10)  # force TTL expiry (TTL is 2.5s)
    reloaded = asyncio.run(SimulationEngine.get_triggered_tps(engine, "t-1"))
    assert reloaded == {1, 2}


# ── _last_closed_tp ────────────────────────────────────────────────────────────

# ── _log_tp_wait_diagnostic ────────────────────────────────────────────────────

# ── _check_sl ──────────────────────────────────────────────────────────────────

def test_check_sl_buy_hit(fresh_db):
    trade = {"trade_id": "t-1", "direction": "BUY", "stop_loss": 2390.0}
    result = check_sl(trade, _tick(bid=2389.0, ask=2389.5))
    assert result == ("t-1", 2390.0, "SL")


def test_check_sl_buy_not_hit(fresh_db):
    trade = {"trade_id": "t-1", "direction": "BUY", "stop_loss": 2390.0}
    result = check_sl(trade, _tick(bid=2395.0, ask=2395.5))
    assert result is None


def test_check_sl_sell_hit(fresh_db):
    trade = {"trade_id": "t-1", "direction": "SELL", "stop_loss": 2410.0}
    result = check_sl(trade, _tick(bid=2410.5, ask=2411.0))
    assert result == ("t-1", 2410.0, "SL")


def test_check_sl_no_stop_set(fresh_db):
    trade = {"trade_id": "t-1", "direction": "BUY", "stop_loss": None}
    result = check_sl(trade, _tick(bid=2300.0, ask=2300.5))
    assert result is None


# ── _check_tp_hits ─────────────────────────────────────────────────────────────

# ── _get_remaining_lots ────────────────────────────────────────────────────────


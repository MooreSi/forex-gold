"""Characterizes _handle_trail_stop on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-trail-stop-handler-migration/010-*.md.

Uses a fake bridge (modify_order). NO real or demo MT5 order is ever
modified -- verified via the fake's own call log.
"""
import asyncio
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.positions.tp_tracking import TPCache as _TPCache
from backend.src.runtime import SimulationEngine


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
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self):
        self.modify_order_calls = []

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._tp_trigger_cache = _TPCache()
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


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", mt5_ticket=None,
                  stop_loss=2390.0, entry_price=2400.0, sl_moved_to_be=0, **tps):
    with db.db() as conn:
        cols = ("trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
                "entry_price, lot_size, remaining_lots, stop_loss, sl_moved_to_be, status, open_time")
        vals = [trade_id, sig_id, mt5_ticket, direction, 2399.0, 2401.0, entry_price,
                0.10, 0.10, stop_loss, sl_moved_to_be, "open", time.time()]
        for k, v in tps.items():
            cols += f", {k}"
            vals.append(v)
        placeholders = ",".join("?" for _ in vals)
        conn.execute(f"INSERT INTO vantage_simulated_trades ({cols}) VALUES ({placeholders})", vals)


def _trade_dict(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_tp1_not_cleared_not_trailing_is_noop(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, tp1=2410.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2405.0, ask=2405.5)))
    assert engine._bridge.modify_order_calls == []


def test_tp1_cleared_activates_locks_sl_to_entry(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2390.0, tp1=2410.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2412.0, ask=2412.5)))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2400.0, "tp": None}]
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0
    assert trade_after["sl_moved_to_be"] == 1
    with db.db() as conn:
        marker = conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=?", ("t-1",)
        ).fetchone()
    assert marker[0] == "TP1_TRAIL_START"


def test_stale_trade_dict_still_detects_active_trailing_via_db(fresh_db, engine):
    # trade dict passed in claims sl_moved_to_be=0 (stale), but the DB row
    # itself already has it set (a prior cycle activated it) -- the handler
    # re-checks the DB directly rather than trusting the stale dict.
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    trade["sl_moved_to_be"] = 0  # simulate staleness
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2408.0, ask=2408.5)))
    # Active trailing (not re-activation) -- SL should trail to bid-5, not re-lock to entry.
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2403.0, "tp": None}]


def test_active_trailing_moves_sl_forward_with_price(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2400.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2420.0, ask=2420.5)))
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2415.0, "tp": None}]  # bid - 5


def test_active_trailing_never_moves_sl_backward(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2415.0, sl_moved_to_be=1)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2410.0, ask=2410.5)))
    assert engine._bridge.modify_order_calls == []  # bid-5=2405 < current_sl(2415) -- no update


def test_tp2_marker_recorded_once_while_trailing(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=555, entry_price=2400.0, stop_loss=2415.0, sl_moved_to_be=1,
                  tp2=2420.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2425.0, ask=2425.5)))

    with db.db() as conn:
        markers = conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=? AND reason=?",
            ("t-1", "TP2_TRAIL_MARKER"),
        ).fetchall()
    assert len(markers) == 1
    # remaining_lots untouched -- no actual close happened
    trade_after = _trade_dict("t-1")
    assert trade_after["remaining_lots"] == 0.10

    # De-dup relies on _get_triggered_tps' 2.5s TTL cache reflecting the
    # marker row just written -- force the cache to expire (as a real tick
    # arriving after the TTL window would) before checking a second call
    # doesn't duplicate the marker. Within the TTL window itself a
    # duplicate marker CAN occur (harmless for the UI's "at least one row"
    # chip logic) -- a real, documented quirk, not something masked here.
    cached_set, _ = engine._tp_trigger_cache.triggered["t-1"]
    engine._tp_trigger_cache.triggered["t-1"] = (cached_set, time.time() - 10)
    trade2 = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade2, _tick(bid=2426.0, ask=2426.5)))
    with db.db() as conn:
        markers_after = conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=? AND reason=?",
            ("t-1", "TP2_TRAIL_MARKER"),
        ).fetchall()
    assert len(markers_after) == 1


def test_no_mt5_ticket_still_updates_db_skips_bridge(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", mt5_ticket=None, entry_price=2400.0, stop_loss=2390.0, tp1=2410.0)
    trade = _trade_dict("t-1")
    asyncio.run(SimulationEngine._handle_trail_stop(engine, trade, _tick(bid=2412.0, ask=2412.5)))
    assert engine._bridge.modify_order_calls == []
    trade_after = _trade_dict("t-1")
    assert trade_after["stop_loss"] == 2400.0

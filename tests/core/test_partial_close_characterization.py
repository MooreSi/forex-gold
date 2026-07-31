"""Characterizes partial_close_trade on SimulationEngine (core/engine.py)
before task 020 extracts it -- see
docs/todo/refactor/core-partial-close-migration/010-*.md.

partial_close_trade never calls the MT5 bridge -- pure DB accounting, no
test-double needed. It IS async and routes writes through
db_module.to_db_thread(), so it needs the db-worker-thread reset helper
discovered in pack 5's 010 (test_tp_trigger_tracking_characterization.py).
"""
import asyncio
import os
import tempfile
import time

import pytest

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
    return SimulationEngine.__new__(SimulationEngine)


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id, sig_id="sig-1", direction="BUY", status="open",
                  remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, sl_moved_to_be) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, direction, 2399.0, 2401.0, 2400.0, 0.10,
             remaining_lots, stop_loss, status, time.time(), sl_moved_to_be),
        )


def _get_trade(trade_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def test_raises_on_unknown_trade(fresh_db, engine):
    with pytest.raises(ValueError):
        asyncio.run(SimulationEngine.partial_close_trade(engine, "does-not-exist", 0.05, 2410.0))


def test_raises_when_trade_not_open(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", status="closed")
    with pytest.raises(ValueError):
        asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.05, 2410.0))


def test_normal_partial_close_updates_trade_and_inserts_row(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10)
    result = asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.04, 2410.0, reason="TP1"))

    assert result["lots_closed"] == 0.04
    assert result["remaining_lots"] == 0.06
    assert result["auto_closed"] is False

    trade = _get_trade("t-1")
    assert trade["remaining_lots"] == 0.06
    assert trade["realised_pnl"] == result["partial_pnl"]

    with db.db() as conn:
        pc = conn.execute("SELECT * FROM vantage_partial_closes WHERE trade_id=?", ("t-1",)).fetchall()
    assert len(pc) == 1


def test_updates_sim_balance(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10)
    result = asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.04, 2410.0, reason="TP1"))

    with db.db() as conn:
        balance = conn.execute("SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    assert balance == round(1000.0 + result["partial_pnl"], 10)


def test_clamps_lots_to_close_to_remaining(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.05)
    result = asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 10.0, 2410.0, reason="TP1"))
    assert result["lots_closed"] == 0.05
    assert result["remaining_lots"] == 0.0
    assert result["auto_closed"] is True


def test_moves_sl_to_be_on_tp1_when_enabled_and_not_already_moved(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0)
    asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.04, 2410.0, reason="TP1"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2400.0  # moved to entry_price
    assert trade["sl_moved_to_be"] == 1


def test_does_not_move_sl_twice(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2405.0, sl_moved_to_be=1)
    asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.04, 2410.0, reason="TP1"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2405.0  # untouched -- already at BE


def test_does_not_move_sl_for_non_tp1_reason(fresh_db, engine):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0)
    asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.04, 2410.0, reason="TP2"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2390.0
    assert trade["sl_moved_to_be"] == 0


def test_does_not_move_sl_when_setting_disabled(fresh_db, engine):
    db.update_risk_settings({"move_sl_to_be_after_tp1": 0})
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0)
    asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.04, 2410.0, reason="TP1"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2390.0


def test_auto_closes_and_cascades_signal_status(fresh_db, engine):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", remaining_lots=0.05)
    result = asyncio.run(SimulationEngine.partial_close_trade(engine, "t-1", 0.05, 2410.0, reason="TP8"))

    assert result["auto_closed"] is True
    trade = _get_trade("t-1")
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "all_tps_hit"

    with db.db() as conn:
        sig_status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-1",)).fetchone()[0]
    assert sig_status == "closed"

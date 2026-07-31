"""Proves backend.src.services.trading.partial_close's extracted function behaves
identically to SimulationEngine.partial_close_trade, characterized in
test_partial_close_characterization.py -- see
docs/todo/refactor/core-partial-close-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class.
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.services.trading import partial_close as pc


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


def test_raises_on_unknown_trade(fresh_db):
    with pytest.raises(ValueError):
        asyncio.run(pc.partial_close_trade("does-not-exist", 0.05, 2410.0))


def test_raises_when_trade_not_open(fresh_db):
    _insert_signal()
    _insert_trade("t-1", status="closed")
    with pytest.raises(ValueError):
        asyncio.run(pc.partial_close_trade("t-1", 0.05, 2410.0))


def test_normal_partial_close_updates_trade_and_inserts_row(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10)
    result = asyncio.run(pc.partial_close_trade("t-1", 0.04, 2410.0, reason="TP1"))

    assert result["lots_closed"] == 0.04
    assert result["remaining_lots"] == 0.06
    assert result["auto_closed"] is False

    trade = _get_trade("t-1")
    assert trade["remaining_lots"] == 0.06
    assert trade["realised_pnl"] == result["partial_pnl"]

    with db.db() as conn:
        rows = conn.execute("SELECT * FROM vantage_partial_closes WHERE trade_id=?", ("t-1",)).fetchall()
    assert len(rows) == 1


def test_updates_sim_balance(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10)
    result = asyncio.run(pc.partial_close_trade("t-1", 0.04, 2410.0, reason="TP1"))

    with db.db() as conn:
        balance = conn.execute("SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    assert balance == round(1000.0 + result["partial_pnl"], 10)


def test_clamps_lots_to_close_to_remaining(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.05)
    result = asyncio.run(pc.partial_close_trade("t-1", 10.0, 2410.0, reason="TP1"))
    assert result["lots_closed"] == 0.05
    assert result["remaining_lots"] == 0.0
    assert result["auto_closed"] is True


def test_moves_sl_to_be_on_tp1_when_enabled_and_not_already_moved(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0)
    asyncio.run(pc.partial_close_trade("t-1", 0.04, 2410.0, reason="TP1"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2400.0
    assert trade["sl_moved_to_be"] == 1


def test_does_not_move_sl_twice(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2405.0, sl_moved_to_be=1)
    asyncio.run(pc.partial_close_trade("t-1", 0.04, 2410.0, reason="TP1"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2405.0


def test_does_not_move_sl_for_non_tp1_reason(fresh_db):
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0)
    asyncio.run(pc.partial_close_trade("t-1", 0.04, 2410.0, reason="TP2"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2390.0
    assert trade["sl_moved_to_be"] == 0


def test_does_not_move_sl_when_setting_disabled(fresh_db):
    db.update_risk_settings({"move_sl_to_be_after_tp1": 0})
    _insert_signal()
    _insert_trade("t-1", remaining_lots=0.10, stop_loss=2390.0, sl_moved_to_be=0)
    asyncio.run(pc.partial_close_trade("t-1", 0.04, 2410.0, reason="TP1"))

    trade = _get_trade("t-1")
    assert trade["stop_loss"] == 2390.0


def test_auto_closes_and_cascades_signal_status(fresh_db):
    _insert_signal("sig-1")
    _insert_trade("t-1", "sig-1", remaining_lots=0.05)
    result = asyncio.run(pc.partial_close_trade("t-1", 0.05, 2410.0, reason="TP8"))

    assert result["auto_closed"] is True
    trade = _get_trade("t-1")
    assert trade["status"] == "closed"
    assert trade["exit_reason"] == "all_tps_hit"

    with db.db() as conn:
        sig_status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-1",)).fetchone()[0]
    assert sig_status == "closed"

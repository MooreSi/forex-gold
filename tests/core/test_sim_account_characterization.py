"""Characterizes get_sim_account/update_sim_balance/reset_simulation on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-fees-risk-governor-migration/010-*.md.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine


def _reset_thread_local_connection():
    """db_module caches a thread-local sqlite3 connection -- db.init(path)
    alone does NOT close/replace it, so a connection opened by a previous
    test would silently keep serving the OLD (now-deleted) file instead of
    the new one. Must be explicitly torn down for tests to actually be
    isolated from each other."""
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


class _FakeEngine:
    """Minimal stand-in exposing only what reset_simulation() reads --
    real SimulationEngine construction needs a live bridge/config."""
    def __init__(self, starting_balance: float):
        self._cfg = {"starting_balance": starting_balance}


def _insert_signal(sig_id="sig-1", direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (sig_id, direction, 2399.0, 2401.0, 2390.0, "active", time.time()),
        )


def _insert_trade(trade_id="t-1", sig_id="sig-1", direction="BUY", status="open",
                  close_time=None, net_pnl=0.0, sl_moved_to_be=0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time, net_pnl, sl_moved_to_be) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, sig_id, direction, 2399.0, 2401.0, 2400.0, 0.10, 0.10, 2390.0,
             status, time.time(), close_time, net_pnl, sl_moved_to_be),
        )


def test_get_sim_account_returns_seeded_defaults(fresh_db):
    account = SimulationEngine.get_sim_account(None)
    assert account["balance"] == 1000.0  # seeded by _apply_schema
    assert account["currency"] == "USD"


def test_reset_simulation_cancels_pending_and_active_signals(fresh_db):
    _insert_signal(sig_id="sig-pending")
    with db.db() as conn:
        conn.execute("UPDATE vantage_signals SET status='pending' WHERE signal_id=?", ("sig-pending",))

    engine = _FakeEngine(starting_balance=1000.0)
    SimulationEngine.reset_simulation(engine)

    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-pending",)
        ).fetchone()[0]
    assert status == "cancelled"



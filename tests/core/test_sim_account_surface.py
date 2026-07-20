"""Proves forex_trader.core.core_sim_account's extracted functions behave
identically to the SimulationEngine methods characterized in
test_sim_account_characterization.py -- see
docs/todo/refactor/core-fees-risk-governor-migration/020-*.md.
"""
import os
import tempfile
import time

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_sim_account as sa


def _reset_thread_local_connection():
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
    account = sa.get_sim_account()
    assert account["balance"] == 1000.0
    assert account["currency"] == "USD"


def test_update_sim_balance_applies_delta(fresh_db):
    sa.update_sim_balance(50.0)
    assert sa.get_sim_account()["balance"] == 1050.0
    sa.update_sim_balance(-20.0)
    assert sa.get_sim_account()["balance"] == 1030.0


def test_reset_simulation_resets_balance_and_wipes_trades(fresh_db):
    _insert_signal()
    _insert_trade()
    sa.update_sim_balance(500.0)
    assert sa.get_sim_account()["balance"] == 1500.0

    sa.reset_simulation(starting_balance=1000.0)

    assert sa.get_sim_account()["balance"] == 1000.0
    with db.db() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
    assert remaining == 0


def test_reset_simulation_cancels_pending_and_active_signals(fresh_db):
    _insert_signal(sig_id="sig-pending")
    with db.db() as conn:
        conn.execute("UPDATE vantage_signals SET status='pending' WHERE signal_id=?", ("sig-pending",))

    sa.reset_simulation(starting_balance=1000.0)

    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", ("sig-pending",)
        ).fetchone()[0]
    assert status == "cancelled"


def test_reset_simulation_is_atomic_via_existing_single_with_block(fresh_db):
    """Same forced-failure proof as 010 -- reset_simulation's body was
    already a single `with db_module.db():` block before extraction and
    remains one here verbatim, so this must still pass unchanged."""
    _insert_signal()
    _insert_trade()
    sa.update_sim_balance(500.0)

    from unittest.mock import patch

    class _FailingConn:
        def __init__(self, real_conn):
            self._real = real_conn
            self._calls = 0
        def execute(self, sql, *args):
            self._calls += 1
            if self._calls == 2:
                raise RuntimeError("simulated crash mid-reset")
            return self._real.execute(sql, *args)

    import contextlib

    @contextlib.contextmanager
    def _wrapped_db():
        with db.db() as real_conn:
            yield _FailingConn(real_conn)

    with patch.object(sa.reset_simulation.__globals__["db_module"], "db", _wrapped_db):
        with pytest.raises(RuntimeError):
            sa.reset_simulation(starting_balance=1000.0)

    assert sa.get_sim_account()["balance"] == 1500.0
    with db.db() as conn:
        remaining = conn.execute("SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
    assert remaining == 1

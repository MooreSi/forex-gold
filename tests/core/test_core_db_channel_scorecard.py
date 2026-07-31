"""Regression test for a real bug found 2026-07-21: get_channel_scorecard()
(core/core_db_channel.py) calls _trade_pts()/_session_for_hour(), both
defined in core/core_db_analytics.py, but core_db_channel.py never imported
them -- a NameError on every call, missed by the full suite because nothing
exercised this function. Crashed the app for real on a demo->live account
switch (History page re-render calls it via db_module.get_channel_scorecard).
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db


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
    yield db
    _reset_thread_local_connection()
    os.remove(path)


def _insert_closed_trade(conn, trade_id, tg_source, direction, entry, close, pnl, close_time):
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"sig-{trade_id}", direction, entry - 1, entry + 1, entry - 5, "filled", close_time - 60),
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, close_time, close_price, "
        " net_pnl, tg_source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, f"sig-{trade_id}", 1000, direction, entry - 1, entry + 1, entry,
         0.1, 0.1, entry - 5, "closed", close_time - 60, close_time, close,
         pnl, tg_source),
    )


def test_get_channel_scorecard_computes_points_and_session(fresh_db):
    """The exact crash path: a closed trade with a real close_time (drives
    _session_for_hour) and entry/close prices (drives _trade_pts)."""
    now = time.time()
    with fresh_db.db() as conn:
        _insert_closed_trade(conn, "t1", "Gold Diggers VIP", "BUY", 2400.0, 2410.0, 50.0, now)
        _insert_closed_trade(conn, "t2", "Gold Diggers VIP", "SELL", 2410.0, 2405.0, -20.0, now)

    scorecard = fresh_db.get_channel_scorecard(days=30)

    assert len(scorecard) == 1
    row = scorecard[0]
    assert row["source"] == "Gold Diggers VIP"
    assert row["trades"] == 2
    assert row["wins"] == 1
    assert row["losses"] == 1
    # BUY 2400->2410 = +10 pts, SELL 2410->2405 = +5 pts (favorable move for a short)
    assert row["avg_pts"] == pytest.approx(7.5)
    assert sum(row["sessions"].values()) == pytest.approx(30.0)

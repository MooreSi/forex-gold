"""The reversal panel's realised P&L, read from the core database.

`panel_data.get_realised_pnl` carries the last SQL statement in that module,
and it is being moved into `analytics/read_repo` so the SQL sits in the data
layer. Its docstring records a distinction that is easy to lose in a move, so
it is pinned first:

    Deliberately not read from reversal_engine.db: that database records every
    signal the generator produced and prices them all at the virtual lot,
    whether or not the trade was ever placed. Only rows here in
    vantage_simulated_trades correspond to orders that really went to MT5.

So: closed rows only, this channel only, and the per-trade average derived
rather than stored.
"""
from __future__ import annotations

import asyncio
import uuid

import pytest

from backend.src.services.reversal_engine import panel_data


def _closed_trade(conn, *, pnl, source="Reversal Engine", status="closed"):
    tid = uuid.uuid4().hex[:16]
    sid = f"sig-{tid}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, "BUY", 2399.0, 2401.0, 2390.0))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, net_pnl, tg_source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?)",
        (tid, sid, "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.1, 2390.0,
         status, pnl, source))
    return tid


def test_an_empty_book_reports_zero_without_dividing_by_it(fresh_db):
    out = asyncio.run(panel_data.get_realised_pnl())
    assert out == {"n": 0, "total": 0.0, "per_trade": 0.0}


def test_closed_reversal_trades_are_summed(fresh_db):
    with fresh_db.db() as conn:
        _closed_trade(conn, pnl=30.0)
        _closed_trade(conn, pnl=-10.0)

    out = asyncio.run(panel_data.get_realised_pnl())

    assert out["n"] == 2
    assert out["total"] == pytest.approx(20.0)
    assert out["per_trade"] == pytest.approx(10.0)


def test_open_trades_are_not_counted(fresh_db):
    """Realised means realised -- an open position's floating P&L is not it."""
    with fresh_db.db() as conn:
        _closed_trade(conn, pnl=30.0)
        _closed_trade(conn, pnl=999.0, status="open")

    out = asyncio.run(panel_data.get_realised_pnl())

    assert out["n"] == 1 and out["total"] == pytest.approx(30.0)


def test_another_channels_trades_are_not_counted(fresh_db):
    """This is the Reversal Engine's own panel."""
    with fresh_db.db() as conn:
        _closed_trade(conn, pnl=30.0)
        _closed_trade(conn, pnl=500.0, source="GD VIP")

    out = asyncio.run(panel_data.get_realised_pnl())

    assert out["n"] == 1 and out["total"] == pytest.approx(30.0)

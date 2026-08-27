"""The two daily-P&L reads that feed risk decisions.

Both are being moved out of their services and into the analytics read repo so
the SQL sits in the data layer. Neither decides anything on its own, but what
they return does:

  risk/governor.day_pnl_and_peak   feeds the daily loss limit -- the
                                         guard that halts trading for the day
  risk/schedule._day_realized_pnl        feeds the schedule's daily cumulative
                                         profit target

They look like the same query and are not. **The governor keys on `close_time`;
the schedule keys on `open_time`.** A trade opened yesterday and closed today
counts for the governor and not for the schedule; one opened today and still
running counts for neither. Conflating those two columns in a move is silent
and would move a risk threshold, so it is pinned explicitly.
"""
from __future__ import annotations

import uuid

import pytest

from backend.src.services.risk import governor, schedule


def _trade(conn, *, pnl, open_time, close_time=None, status="closed"):
    tid = uuid.uuid4().hex[:16]
    sid = f"sig-{tid}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, "BUY", 2399.0, 2401.0, 2390.0))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, close_time, net_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (tid, sid, "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.1, 2390.0,
         status, open_time, close_time, pnl))
    return tid


DAY = 1_000_000.0          # an arbitrary day boundary


# ── governor: realised and peak, keyed on close_time ──────────────────────────

def test_the_governor_sums_todays_closes_in_order(fresh_db):
    with fresh_db.db() as conn:
        _trade(conn, pnl=50.0, open_time=DAY - 5000, close_time=DAY + 10)
        _trade(conn, pnl=-20.0, open_time=DAY + 20, close_time=DAY + 30)

    running, peak = governor.day_pnl_and_peak(DAY)

    assert running == pytest.approx(30.0)
    assert peak == pytest.approx(50.0), "peak is the high-water mark, not the close"


def test_the_governor_counts_a_trade_opened_yesterday_and_closed_today(fresh_db):
    """It keys on close_time. This is the case that separates it from the
    schedule's read."""
    with fresh_db.db() as conn:
        _trade(conn, pnl=40.0, open_time=DAY - 90_000, close_time=DAY + 10)

    running, _ = governor.day_pnl_and_peak(DAY)
    assert running == pytest.approx(40.0)


def test_the_governor_ignores_trades_closed_before_the_day_started(fresh_db):
    with fresh_db.db() as conn:
        _trade(conn, pnl=999.0, open_time=DAY - 90_000, close_time=DAY - 10)

    running, peak = governor.day_pnl_and_peak(DAY)
    assert (running, peak) == (0.0, 0.0)


def test_the_peak_never_goes_negative(fresh_db):
    """A losing day has a peak of zero, not the least-bad point -- the
    give-back guard measures from a high-water mark that starts at flat."""
    with fresh_db.db() as conn:
        _trade(conn, pnl=-10.0, open_time=DAY, close_time=DAY + 10)
        _trade(conn, pnl=-5.0, open_time=DAY, close_time=DAY + 20)

    running, peak = governor.day_pnl_and_peak(DAY)
    assert running == pytest.approx(-15.0)
    assert peak == 0.0


# ── schedule: today's total, keyed on open_time ───────────────────────────────

def _at(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, tz=timezone.utc).replace(tzinfo=None)


def test_the_schedule_sums_trades_opened_today(fresh_db):
    now = _at(DAY + 40_000)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with fresh_db.db() as conn:
        _trade(conn, pnl=25.0, open_time=start + 10, close_time=start + 20)
        _trade(conn, pnl=15.0, open_time=start + 30, close_time=start + 40)

    assert schedule._day_realized_pnl(now) == pytest.approx(40.0)


def test_the_schedule_ignores_a_trade_opened_yesterday(fresh_db):
    """It keys on open_time -- the mirror of the governor case above. A move
    that swapped the two columns would pass every other test here."""
    now = _at(DAY + 40_000)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with fresh_db.db() as conn:
        _trade(conn, pnl=500.0, open_time=start - 3600, close_time=start + 100)

    assert schedule._day_realized_pnl(now) == pytest.approx(0.0)


def test_the_schedule_ignores_open_positions(fresh_db):
    now = _at(DAY + 40_000)
    start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with fresh_db.db() as conn:
        _trade(conn, pnl=500.0, open_time=start + 10, status="open", close_time=None)

    assert schedule._day_realized_pnl(now) == pytest.approx(0.0)


def test_an_empty_day_is_zero_not_none(fresh_db):
    assert schedule._day_realized_pnl(_at(DAY)) == 0.0
    assert governor.day_pnl_and_peak(DAY) == (0.0, 0.0)

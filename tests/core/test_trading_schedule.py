"""Trading Schedule: per-day/per-window profit-target discipline gate.

Covers: disabled-by-default, outside-all-windows blocking, in-window
allowed with no target, target reached blocking, trades outside the
window not counting toward it, and manual orders bypassing the gate
by construction (core_manual_market_order.py never calls
resolve_open_trade_params, the only caller of check_trading_schedule).
"""
import os
import tempfile
import time
from datetime import datetime

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_trading_schedule as sched


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


def _insert_closed_trade(conn, trade_id, direction, pnl, open_time):
    conn.execute(
        "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
        "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
        (f"sig-{trade_id}", direction, 2399.0, 2401.0, 2390.0, "filled", open_time),
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, close_time, "
        " net_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (trade_id, f"sig-{trade_id}", direction, 2399.0, 2401.0, 2400.0,
         0.1, 0.1, 2390.0, "closed", open_time, open_time + 60, pnl),
    )


# A fixed Wednesday, so weekday() is deterministic regardless of when tests run.
_WEDNESDAY = datetime(2026, 7, 22, 10, 30, 0)
assert _WEDNESDAY.weekday() == 2  # Wednesday


def _schedule_with_one_block(start="09:00", end="12:00", target=0.0, enabled=True):
    schedule = sched._default_schedule()
    schedule["wednesday"][0] = {
        "enabled": enabled, "start": start, "end": end, "target": target,
    }
    return schedule


def test_disabled_by_default_always_allows(fresh_db):
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is True
    assert reason == ""


def test_enabled_outside_every_window_blocks(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="14:00", end="16:00"))
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)  # 10:30, before the window
    assert allowed is False
    assert "outside today's trading schedule" in reason
    assert "Wednesday" in reason


def test_enabled_inside_window_no_target_allows(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="09:00", end="12:00", target=0.0))
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)  # 10:30, inside
    assert allowed is True
    assert reason == ""


def test_target_reached_by_trades_opened_in_window_blocks(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="09:00", end="12:00", target=50.0))

    day_start = _WEDNESDAY.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    in_window_open = day_start + 10 * 3600  # 10:00, inside 09:00-12:00

    with fresh_db.db() as conn:
        _insert_closed_trade(conn, "t1", "BUY", 30.0, in_window_open)
        _insert_closed_trade(conn, "t2", "BUY", 25.0, in_window_open + 60)

    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "profit target reached" in reason
    assert "$55.00 of $50.00" in reason


def test_trades_outside_the_window_dont_count_toward_its_target(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="09:00", end="12:00", target=50.0))

    day_start = _WEDNESDAY.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    outside_window_open = day_start + 20 * 3600  # 20:00, outside 09:00-12:00

    with fresh_db.db() as conn:
        _insert_closed_trade(conn, "t1", "BUY", 500.0, outside_window_open)

    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)  # 10:30, inside the window
    assert allowed is True
    assert reason == ""


def test_below_target_still_allows(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="09:00", end="12:00", target=50.0))

    day_start = _WEDNESDAY.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    in_window_open = day_start + 10 * 3600

    with fresh_db.db() as conn:
        _insert_closed_trade(conn, "t1", "BUY", 20.0, in_window_open)

    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is True


def test_disabled_block_within_the_day_is_ignored(fresh_db):
    sched.set_trading_schedule_enabled(True)
    schedule = _schedule_with_one_block(start="09:00", end="12:00", enabled=False)
    sched.set_trading_schedule(schedule)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False  # no enabled block covers 10:30 at all
    assert "outside today's trading schedule" in reason


def test_get_trading_schedule_fills_in_defaults_for_malformed_data(fresh_db):
    sched.set_trading_schedule({"monday": [{"enabled": True}]})  # malformed: wrong block count
    schedule = sched.get_trading_schedule()
    assert len(schedule["monday"]) == sched.BLOCKS_PER_DAY
    assert len(schedule["wednesday"]) == sched.BLOCKS_PER_DAY
    assert schedule["monday"][0]["enabled"] is False  # fell back to default, not the malformed value


def test_manual_market_order_never_reaches_the_gate():
    """Structural guarantee: core_manual_market_order.py must never call
    resolve_open_trade_params (the only caller of check_trading_schedule),
    or manual orders would stop being exempt from the schedule."""
    import inspect
    from forex_trader.core import core_manual_market_order as manual_mod
    source = inspect.getsource(manual_mod)
    assert "resolve_open_trade_params" not in source

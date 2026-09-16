"""The daily profit target: what the header badge shows, and resuming past it.

The daily target (`trading_schedule_daily_target`) is the one gate in
`check_trading_schedule` that is neither a window nor a source toggle: once the
day's realised P&L clears it, every automated entry is refused for the rest of
the day. Until now the only thing that lifted it was the clock.

Two things were missing and are added here (owner, 2026-09-16):

  * The header said "Circuit Breaker OK" while this gate was holding every
    order -- the same false all-clear the news blackout box was added to fix
    on 2026-09-04, for a gate that lasts the whole day rather than minutes.
  * There was no way to say "I know, carry on". A target that cannot be
    overridden is a target the operator turns off in Settings instead, and
    then forgets to turn back on.

The override is recorded as a DAY, not a flag, so it expires by construction at
midnight on the trading clock rather than needing anything to clear it. That is
the same reasoning `_block_realized_pnl` uses for computing P&L on demand: no
reset-at-midnight bookkeeping, nothing to drift.
"""
from datetime import datetime

import pytest

from backend.src.db import database as db_module
from backend.src.services.risk import schedule as sched

from tests.core.test_trading_schedule import (
    _insert_closed_trade, _schedule_with_one_block, _WEDNESDAY,
)

_THURSDAY = datetime(2026, 7, 23, 10, 30, 0)
assert _THURSDAY.weekday() == 3


def _book(fresh_db, pnl, now=_WEDNESDAY, hour=10):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with fresh_db.db() as conn:
        _insert_closed_trade(conn, f"t{hour}-{pnl}", "BUY", pnl, day_start + hour * 3600)


def _enable(daily_target):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="09:00", end="12:00"))
    sched.set_daily_profit_target(daily_target)


# ── The state the header badge renders ──────────────────────────────────────

def test_no_target_configured_is_never_reached(fresh_db):
    _enable(0.0)
    state = sched.daily_profit_target_state(now=_WEDNESDAY)
    assert state["reached"] is False
    assert state["target"] == 0.0


def test_below_the_target_is_not_reached_and_carries_both_figures(fresh_db):
    _enable(100.0)
    _book(fresh_db, 40.0)
    state = sched.daily_profit_target_state(now=_WEDNESDAY)
    assert state["reached"] is False
    assert state["pnl"] == 40.0
    assert state["target"] == 100.0


def test_clearing_the_target_is_reached(fresh_db):
    _enable(100.0)
    _book(fresh_db, 120.0)
    state = sched.daily_profit_target_state(now=_WEDNESDAY)
    assert state["reached"] is True
    assert state["overridden"] is False


def test_the_target_is_not_reached_while_the_schedule_is_off(fresh_db):
    """The gate only exists inside check_trading_schedule, which returns early
    when the schedule is disabled. A badge that announced a halt the entry path
    is not applying would be the same false positive the news box fixed."""
    sched.set_trading_schedule_enabled(False)
    sched.set_daily_profit_target(100.0)
    _book(fresh_db, 120.0)
    assert sched.daily_profit_target_state(now=_WEDNESDAY)["reached"] is False


def test_the_state_answers_the_full_shape_when_the_read_fails(fresh_db, monkeypatch):
    """This runs on the header's 5s timer, same as news_pause_state. A failed
    read must cost the badge, not the header."""
    def _boom(*a, **k):
        raise RuntimeError("db exploded")

    monkeypatch.setattr(sched, "_day_realized_pnl", _boom)
    state = sched.daily_profit_target_state(now=_WEDNESDAY)
    assert state["reached"] is False
    assert set(state) >= {"reached", "overridden", "pnl", "target"}


# ── The gate itself ─────────────────────────────────────────────────────────

def test_reaching_the_daily_target_blocks_entries(fresh_db):
    _enable(100.0)
    _book(fresh_db, 120.0)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "daily profit target reached" in reason


def test_resuming_lets_entries_through_again(fresh_db):
    _enable(100.0)
    _book(fresh_db, 120.0)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is True
    assert reason == ""


def test_resuming_says_so_in_the_state(fresh_db):
    _enable(100.0)
    _book(fresh_db, 120.0)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    state = sched.daily_profit_target_state(now=_WEDNESDAY)
    assert state["overridden"] is True
    assert state["reached"] is False  # the badge goes back to the all-clear


def test_resuming_does_not_lift_the_windows_own_target(fresh_db):
    """Two independent gates. Clicking the badge answers the daily one; a
    window that has made its own number stays shut."""
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(
        _schedule_with_one_block(start="09:00", end="12:00", target=50.0))
    sched.set_daily_profit_target(100.0)
    _book(fresh_db, 120.0)

    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "profit target reached for this window" in reason


def test_resuming_does_not_lift_the_window_hours(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_schedule_with_one_block(start="14:00", end="16:00"))
    sched.set_daily_profit_target(100.0)
    _book(fresh_db, 120.0)

    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)  # 10:30
    assert allowed is False
    assert "outside today's trading schedule" in reason


def test_yesterdays_resume_does_not_carry_into_today(fresh_db):
    """The whole point of storing a day rather than a flag: an override taken
    on Wednesday must not silently disarm Thursday's target too."""
    _enable(100.0)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    _book(fresh_db, 120.0, now=_THURSDAY)

    allowed, reason = sched.check_trading_schedule(now=_THURSDAY)
    assert allowed is False
    assert "daily profit target reached" in reason


def test_a_resume_with_nothing_to_resume_still_records_the_day(fresh_db):
    """The header re-reads before it opens the dialog, but the two are seconds
    apart; calling this when the target has since moved must not raise."""
    _enable(100.0)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    assert sched.is_daily_profit_target_resumed(now=_WEDNESDAY) is True


# ── It travels to the paired node, like everything else in this schedule ────

def test_the_resume_rides_the_schedule_snapshot(fresh_db):
    _enable(100.0)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    snap = sched.trading_schedule_snapshot()
    assert snap["daily_target_resumed_day"] == _WEDNESDAY.strftime("%Y-%m-%d")


def test_applying_a_snapshot_restores_the_resume(fresh_db):
    _enable(100.0)
    _book(fresh_db, 120.0)
    sched.apply_trading_schedule_snapshot({
        "enabled": True,
        "daily_target": 100.0,
        "daily_target_resumed_day": _WEDNESDAY.strftime("%Y-%m-%d"),
    })
    allowed, _ = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is True


def test_a_snapshot_from_an_older_peer_leaves_the_resume_alone(fresh_db):
    """A node that predates this field sends no key at all. Treating that as
    'clear it' would re-arm a target the operator has deliberately resumed
    past, on every sync tick."""
    _enable(100.0)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    sched.apply_trading_schedule_snapshot({"enabled": True, "daily_target": 100.0})
    assert sched.is_daily_profit_target_resumed(now=_WEDNESDAY) is True

"""A later loss re-opens trading only if it takes the day below the target
(docs/simon-handover/038, owner 2026-10-11: option B).

Was `test_daily_target_latches.py`, which pinned option A (hit is hit, for
the rest of the day). A was built on 2026-10-05 as the provisional, more
cautious default while 038 was open; bugs/064 said it would be revisited if
the owner chose otherwise. The owner's answer: "only if the loss takes the
daily profit figure below the daily target". So the gate compares the day's
running total with the target every time it is asked, and the latch is gone.

The 2026-09-16 day, replayed: $210.68 at 07:10 stops trading. A -$27.00 close
leaves $183.68, below $200, so trading resumes. A loss that leaves the day at
or above the target keeps it stopped.

The other half of bugs/064 (the limit-order route now asks the schedule) is
untouched: it is what stopped the guard's own force-close from being the
loss that re-opened the door.
"""
import json
from datetime import datetime

from backend.src.services.risk import schedule as sched

from tests.core.test_trading_schedule import (
    _insert_closed_trade, _schedule_with_one_block, _WEDNESDAY,
)


def _book(fresh_db, trade_id, pnl, now=_WEDNESDAY, hour=10):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with fresh_db.db() as conn:
        _insert_closed_trade(conn, trade_id, "BUY", pnl, day_start + hour * 3600)


def _enable(daily_target, window_target=0.0):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(
        _schedule_with_one_block(start="09:00", end="12:00", target=window_target))
    sched.set_daily_profit_target(daily_target)


def test_reaching_the_target_stops_trading(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "daily profit target reached" in reason


def test_a_loss_that_takes_the_day_below_the_target_re_opens_trading(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False
    _book(fresh_db, "loss", -27.00)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_a_loss_that_leaves_the_day_above_the_target_keeps_it_stopped(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 260.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False
    _book(fresh_db, "loss", -27.00)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False


def test_landing_exactly_on_the_target_counts_as_reached(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 227.0)
    _book(fresh_db, "loss", -27.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False


def test_the_badge_follows_the_day_too(fresh_db):
    """The header badge and the gate must agree, or it announces a halt that
    is not in force."""
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    assert sched.daily_profit_target_state(now=_WEDNESDAY)["reached"] is True
    _book(fresh_db, "loss", -27.00)
    assert sched.daily_profit_target_state(now=_WEDNESDAY)["reached"] is False


def test_a_latch_left_by_the_option_a_build_is_ignored(fresh_db):
    """A machine that ran the 2026-10-05 build today may hold its latch row.
    It must not keep trading stopped after this change."""
    _enable(200.0)
    fresh_db.set_app_config("trading_schedule_target_reached_day", json.dumps(
        {"day": _WEDNESDAY.strftime("%Y-%m-%d"), "daily": 200.0, "windows": {}}))
    _book(fresh_db, "win", 150.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_resume_still_lets_the_operator_back_in(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_a_window_target_follows_its_window_the_same_way(fresh_db):
    _enable(0.0, window_target=50.0)
    _book(fresh_db, "win", 55.0)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "profit target reached for this window" in reason
    _book(fresh_db, "loss", -20.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_the_refusal_no_longer_promises_tomorrow(fresh_db):
    """Under option B the day can re-open, so "resumes tomorrow" is untrue."""
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    _, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert "resumes tomorrow" not in reason
    assert "falls back below it" in reason

"""Once a profit target is reached it stays reached for the day (bugs/064).

2026-09-16: the daily target was reached at 07:10 ($210.68 of $200), and at
19:32 a -$27.00 close -- booked by the schedule guard's own force-close --
pulled the running sum back to $185.80. The gate compared the live sum with
the target and nothing remembered it had ever been over, so automated entry
resumed and placed ticket 2031441425 at 20:12. The refusal text had said
"resumes tomorrow".

Option A of docs/simon-handover/038 (the recommendation, applied 2026-10-05
under the owner's approval for this session): hit is hit. The "resume past
today's target" override stays the only way back in. Raising the target above
the level that latched releases it, because the new target has not been
reached. The per-window targets have the same shape and get the same latch.
"""
from datetime import datetime

from backend.src.services.risk import schedule as sched

from tests.core.test_trading_schedule import (
    _insert_closed_trade, _schedule_with_one_block, _WEDNESDAY,
)

_THURSDAY = datetime(2026, 7, 23, 10, 30, 0)


def _book(fresh_db, trade_id, pnl, now=_WEDNESDAY, hour=10):
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with fresh_db.db() as conn:
        _insert_closed_trade(conn, trade_id, "BUY", pnl, day_start + hour * 3600)


def _enable(daily_target, window_target=0.0):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(
        _schedule_with_one_block(start="09:00", end="12:00", target=window_target))
    sched.set_daily_profit_target(daily_target)


def test_a_later_loss_does_not_un_reach_the_daily_target(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False
    _book(fresh_db, "loss", -27.00)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "daily profit target reached" in reason


def test_the_badge_stays_reached_after_the_loss(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    sched.check_trading_schedule(now=_WEDNESDAY)
    _book(fresh_db, "loss", -27.00)
    assert sched.daily_profit_target_state(now=_WEDNESDAY)["reached"] is True


def test_the_badge_alone_latches_it(fresh_db):
    """The header polls every 5 s; if it saw the target cleared before any
    signal asked the gate, the gate must still remember it."""
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    assert sched.daily_profit_target_state(now=_WEDNESDAY)["reached"] is True
    _book(fresh_db, "loss", -27.00)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False


def test_never_reached_is_not_latched(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 150.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_the_latch_expires_at_midnight(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    sched.check_trading_schedule(now=_WEDNESDAY)
    thursday = sched._default_schedule()
    thursday["thursday"][0] = {"enabled": True, "start": "09:00", "end": "12:00", "target": 0.0}
    sched.set_trading_schedule(thursday)
    assert sched.check_trading_schedule(now=_THURSDAY) == (True, "")


def test_resume_still_lets_the_operator_back_in(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    sched.check_trading_schedule(now=_WEDNESDAY)
    _book(fresh_db, "loss", -27.00)
    sched.resume_past_daily_profit_target(now=_WEDNESDAY)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_raising_the_target_above_the_latched_level_releases_it(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    sched.check_trading_schedule(now=_WEDNESDAY)
    sched.set_daily_profit_target(300.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY) == (True, "")


def test_lowering_the_target_keeps_it_latched(fresh_db):
    _enable(200.0)
    _book(fresh_db, "win", 210.68)
    sched.check_trading_schedule(now=_WEDNESDAY)
    _book(fresh_db, "loss", -100.0)
    sched.set_daily_profit_target(150.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False


def test_a_window_target_latches_too(fresh_db):
    _enable(0.0, window_target=50.0)
    _book(fresh_db, "win", 55.0)
    assert sched.check_trading_schedule(now=_WEDNESDAY)[0] is False
    _book(fresh_db, "loss", -20.0)
    allowed, reason = sched.check_trading_schedule(now=_WEDNESDAY)
    assert allowed is False
    assert "profit target reached for this window" in reason


def test_a_window_latch_does_not_leak_into_another_window(fresh_db):
    _enable(0.0, window_target=50.0)
    _book(fresh_db, "win", 55.0)
    sched.check_trading_schedule(now=_WEDNESDAY)
    other = _schedule_with_one_block(start="09:00", end="12:00", target=50.0)
    other["wednesday"][1] = {"enabled": True, "start": "13:00", "end": "16:00", "target": 50.0}
    sched.set_trading_schedule(other)
    afternoon = _WEDNESDAY.replace(hour=14)
    assert sched.check_trading_schedule(now=afternoon) == (True, "")

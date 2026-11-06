"""The daily goal counts every close of the day, winter and summer.

Owner, 2026-11-06: the goal was $33.28 and MT5's day reached +$33.67 at 05:05
UTC, yet trading carried on. The broker runs at UTC+2 once the US clocks go
back, and `realised_since` took a fixed 3 hours off MT5's stamps, so every close
between 00:00 and 01:00 UTC read as yesterday's: four closes worth +$11.99, and
a goal figure of +$27.33 on a day the header showed +$39.32.

The rows below are that day's real closes, stamped as MT5 stamped them.

Nothing here reaches a broker: the rows are canned and nothing is placed, closed
or modified, on any account.
"""
from datetime import datetime, timezone

from backend.src.services.analytics import todays_realised as todays
from backend.src.services.risk import daily_goal as dg

DAY_START = datetime(2026, 11, 6, tzinfo=timezone.utc).timestamp()
SUMMER_DAY_START = datetime(2026, 7, 15, tzinfo=timezone.utc).timestamp()
WINTER_OFFSET = 2 * 3600
SUMMER_OFFSET = 3 * 3600

# (MT5 close stamp, P&L), oldest first.
NOV_6 = [
    (1793930587.0, -9.68), (1793931337.0, 11.58), (1793932742.0, 16.17),
    (1793933990.0, -6.08), (1793934570.0, -6.10), (1793936534.0, 16.02),
    (1793938165.0, -6.95), (1793948740.0, 18.71), (1793952262.0, -15.09),
    (1793958875.0, -16.26), (1793961100.0, 9.57), (1793961103.0, 9.06),
    (1793962316.0, 9.31), (1793965567.0, 9.06),
]
THE_CLOSE_THAT_REACHED_THE_GOAL = 8
BALANCE_AT_THAT_CLOSE = 865.61


def _table(rows):
    return {"rows": [{"close_ts": ts, "pnl": pnl} for ts, pnl in rows], "error": None}


def _rs():
    return {"daily_goal_enabled": 1, "daily_goal_mode": "pct", "daily_goal_value": 4.0}


def test_the_goal_figure_is_the_whole_winter_day():
    assert todays.realised_since(_table(NOV_6), DAY_START) == 39.32


def test_a_close_just_after_midnight_in_winter_counts_today():
    assert todays.realised_since(_table([(DAY_START + WINTER_OFFSET, 5.0)]), DAY_START) == 5.0


def test_a_close_just_before_midnight_in_winter_counts_yesterday():
    assert todays.realised_since(_table([(DAY_START - 1 + WINTER_OFFSET, 5.0)]), DAY_START) == 0.0


def test_the_summer_boundary_stays_on_three_hours():
    rows = [(SUMMER_DAY_START - 1 + SUMMER_OFFSET, 100.0), (SUMMER_DAY_START + SUMMER_OFFSET, 5.0)]
    assert todays.realised_since(_table(rows), SUMMER_DAY_START) == 5.0


def test_the_close_that_reached_the_goal_is_seen_as_reaching_it():
    so_far = _table(NOV_6[:THE_CLOSE_THAT_REACHED_THE_GOAL])
    realised = todays.realised_since(so_far, DAY_START)
    assert realised == 33.67
    reason = dg.check_daily_goal(_rs(), BALANCE_AT_THAT_CLOSE, realised, realised)
    assert reason is not None and reason.startswith(dg.SECURED_PREFIX)


def test_the_close_before_it_does_not_reach_the_goal():
    so_far = _table(NOV_6[:THE_CLOSE_THAT_REACHED_THE_GOAL - 1])
    realised = todays.realised_since(so_far, DAY_START)
    assert realised == 14.96
    assert dg.check_daily_goal(_rs(), 846.90, realised, realised) is None

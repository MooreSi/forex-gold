"""Every "today" the money figures count is the trading clock's day.

Owner, 2026-10-10: the dashboard's Trading performance read -$44.22 on a
Saturday with no trades, and "ensure both the daily goal and trading
performance/equity curve are aligned to the trading clock so I can see the
actual performance by my day". The goal and the daily-loss limit counted from
00:00 UTC (01:00 BST), the header and the Calendar filed a close by its UTC
date, and the dashboard's 1D was the last 24 hours. Owner's choice: everything
on the Trading Clock, 00:00 local.

`clock.day_start_ts()` is the one definition. The goal and the daily-loss
limit count from it, their halts last until the next one, the header's Today's
Goal and every closed-trade row are filed by it.

Nothing here reaches a broker: the closes are local rows or canned dicts and
the halt is app_config rows.
"""
from __future__ import annotations

import time
from datetime import date, datetime, timezone

import pytest

from backend.src.db import database as db
from backend.src.services.analytics import todays_realised as tr
from backend.src.services.risk import clock
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg

from tests.core.test_giveback_guard import _closes


def _rs(mode="usd", value=100.0):
    return {"daily_goal_enabled": 1, "daily_goal_mode": mode,
            "daily_goal_value": value}


# ── the day itself ───────────────────────────────────────────────────────────

@pytest.mark.parametrize("offset", [60, 600, -300, 0])
def test_the_day_starts_at_midnight_on_the_trading_clock(fresh_db, offset):
    """UTC+01:00 (BST) starts the day at 23:00 UTC, not at 00:00 UTC."""
    clock.set_offset_minutes(offset)
    start = clock.day_start_ts()
    assert 0 <= time.time() - start < 86400
    assert start % 86400 == (-offset * 60) % 86400


def test_the_goal_and_the_loss_limit_count_from_it(fresh_db):
    clock.set_offset_minutes(600)
    assert rg.calendar_day_start_ts() == clock.day_start_ts()
    assert dg._goal_day_start() == clock.day_start_ts()


def test_the_goal_halt_lasts_until_the_trading_clocks_midnight(fresh_db):
    clock.set_offset_minutes(600)
    _closes([150], at=clock.day_start_ts() + 1)
    assert dg.apply_daily_goal(_rs(), balance=None) is True
    until = float(db.get_app_config("trade_pause_until"))
    assert until == pytest.approx(clock.day_start_ts() + 86400.0)
    assert until % 86400 == (-600 * 60) % 86400


def test_the_loss_limit_halt_lasts_until_the_trading_clocks_midnight(fresh_db):
    clock.set_offset_minutes(600)
    _closes([-500], at=clock.day_start_ts() + 1)
    rs = {**db.get_risk_settings(), "max_daily_loss_pct": 3.0}
    rg.apply_daily_loss_halt_on_close(rs, 9_500.0)
    assert rg.halt_reason().startswith("Daily loss"), rg.halt_reason()
    until = float(db.get_app_config("trade_pause_until"))
    assert until % 86400 == (-600 * 60) % 86400


# ── the header's Today's Goal ────────────────────────────────────────────────

# 2026-09-30 23:30 UTC, stored as MT5 broker time (UTC+3).
LATE = datetime(2026, 9, 30, 23, 30, tzinfo=timezone.utc).timestamp() + 3 * 3600


def test_a_close_after_local_midnight_is_on_the_new_day_in_bst():
    """00:30 BST on 1 October: the owner's 1 October, not 30 September."""
    table = {"error": None, "rows": [{"close_ts": LATE, "pnl": 9.0}]}
    assert tr.realised_on(table, date(2026, 10, 1), offset_minutes=60) == 9.0
    assert tr.realised_on(table, date(2026, 9, 30), offset_minutes=60) == 0.0


def test_the_same_close_is_still_the_old_day_on_utc():
    table = {"error": None, "rows": [{"close_ts": LATE, "pnl": 9.0}]}
    assert tr.realised_on(table, date(2026, 9, 30), offset_minutes=0) == 9.0

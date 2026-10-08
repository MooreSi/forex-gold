"""The daily-loss limit's day is the Calendar's day, as the daily goal's is.

Owner, 2026-10-08: the header read -$33.61 (MT5's closes filed by UTC date, as
the Calendar files them) while the limit counted from the broker rollover
(22:00 BST) and saw -$25.97. Owner's choice: the Calendar's day for both. So
the limit counts from 00:00 UTC today, and the halt it writes lasts until that
day ends (01:00 BST), not the broker rollover.

Nothing here reaches a broker: closes are local rows and the balance is passed in.
"""
from datetime import datetime, timezone

import pytest

from backend.src.db import database as db
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg

from tests.core.test_giveback_guard import _closes

DL = {"max_daily_loss_pct": 3.0}


def _calendar_day_start() -> float:
    d = datetime.now(timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


def test_one_definition_of_the_calendar_day():
    assert rg.calendar_day_start_ts() == _calendar_day_start()
    assert dg._goal_day_start() == rg.calendar_day_start_ts()


def test_a_loss_before_the_calendar_day_does_not_count(fresh_db):
    _closes([-40], at=_calendar_day_start() - 60)
    assert rg.check_daily_loss_limit(DL, balance=960.0) is None


def test_a_loss_inside_the_calendar_day_counts(fresh_db):
    _closes([-40], at=_calendar_day_start() + 1)
    assert rg.check_daily_loss_limit(DL, balance=960.0) is not None


def test_the_halt_lasts_until_the_calendar_day_ends(fresh_db):
    _closes([-40], at=_calendar_day_start() + 1)
    rg.apply_daily_loss_halt_on_close(DL, balance=960.0)
    assert rg.is_trading_paused()
    assert float(db.get_app_config("trade_pause_until")) == pytest.approx(
        _calendar_day_start() + 86400.0)

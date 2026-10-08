"""The daily goal's day is the Calendar's day, not the broker's.

Owner, 2026-10-08: trading stopped at 02:00 BST on "Daily goal secured: +$26.15
today vs a goal of $23.80" while the header read "$24.10 / $18.51". Both were
MT5's closes. The halt counted from the broker day (22:00 BST), the header and
the Calendar file a close by its UTC date (01:00 BST), and +$7.64 closed at
23:05 and 23:07 BST was in one and not the other. Owner's choice: the
Calendar's day. So the goal counts from 00:00 UTC today,
and the halt it writes lasts until that day ends, not until the broker's
rollover (or it would lift at 22:00 and re-halt straight away).

Nothing here reaches a broker: MT5's figures are canned, the balance comes from
a fake bridge, and the halt is app_config rows.
"""
import asyncio
from datetime import datetime, timezone

import pytest

from backend.src.db import database as db
from backend.src.services.risk import daily_goal as dg
from backend.src.services.risk import governor as rg

from tests.core.test_giveback_guard import _closes


def _rs(mode="usd", value=100.0):
    return {"daily_goal_enabled": 1, "daily_goal_mode": mode,
            "daily_goal_value": value}


def _calendar_day_start() -> float:
    d = datetime.now(timezone.utc).date()
    return datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp()


class _Bridge:
    def __init__(self, balance=1_000.0):
        self.balance = balance

    async def get_account(self):
        return {"balance": self.balance}

    async def get_positions(self):
        return []

    async def get_deal_history(self, days=7):  # pragma: no cover - stubbed below
        raise AssertionError("the sweep reads deals through todays_realised")


def test_the_goal_day_starts_at_the_calendars_midnight(fresh_db):
    assert dg._goal_day_start() == _calendar_day_start()


def test_mt5_is_asked_from_the_calendar_day(fresh_db, monkeypatch):
    calls = []

    async def fake(engine, since_ts):
        calls.append(since_ts)
        return 0.0

    monkeypatch.setattr(dg._todays, "since", fake)
    asyncio.run(dg.sweep(dg.SweepState(), _Bridge(), _rs(), now=1_000.0))
    assert calls == [_calendar_day_start()]


def test_the_owners_morning_is_not_reached(fresh_db, monkeypatch):
    """MT5 from the Calendar's midnight: +$18.51 against $24.10."""
    async def fake(engine, since_ts):
        return 18.51 if since_ts >= _calendar_day_start() else 26.15

    monkeypatch.setattr(dg._todays, "since", fake)
    asyncio.run(dg.sweep(dg.SweepState(), _Bridge(602.50 + 18.51),
                         _rs("pct", 4.0), now=1_000.0))
    assert not rg.is_trading_paused(), rg.halt_reason()


def test_the_halt_lasts_until_the_calendar_day_ends(fresh_db):
    _closes([150], at=max(_calendar_day_start(), rg.rg_day_start_ts()) + 1)
    assert dg.apply_daily_goal(_rs(), balance=None) is True
    until = float(db.get_app_config("trade_pause_until"))
    assert until == pytest.approx(_calendar_day_start() + 86400.0)


def test_local_closes_before_the_calendar_day_do_not_count(fresh_db):
    """The local-table fallback uses the same day."""
    start = _calendar_day_start()
    _closes([500], at=start - 60)
    _closes([20], at=max(start, rg.rg_day_start_ts()) + 1)
    assert dg.check_daily_goal(_rs(), balance=None) is None

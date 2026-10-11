"""The dashboard's "Today" is today on the trading clock, not the last 24 hours.

Owner, 2026-10-10: Trading performance and the Equity curve read -$44.22 on a
Saturday morning with nothing traded. The 1D window asked MT5 for `days=1`,
which is the 24 hours before now: most of Friday. Friday as a whole was
-$39.28; the owner's Saturday was $0.00.

`today=True` asks for enough history to cover the day and keeps only the
positions closed since 00:00 on the trading clock. Each row also carries
`day`, the trading-clock date it is filed under, so the Calendar files a close
the same way the header, the goal and this window do.

Nothing here reaches a broker: the engine and the bridge are stubs returning
canned deals.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.analytics import trade_table
from backend.src.services.broker import mt5_performance

from tests.services.analytics.test_trade_table import _Engine, _deal, maps  # noqa: F401

pytestmark = pytest.mark.asyncio

BROKER = 3 * 3600
# 10 October 2026, trading clock UTC+01:00: the day began at 23:00 UTC on the 9th.
DAY_START = datetime(2026, 10, 9, 23, 0, tzinfo=timezone.utc).timestamp()


def _position(pid, close_utc, profit):
    """An open and a close deal, stamped in MT5's broker time."""
    return [
        _deal(position_id=pid, entry=0, time=close_utc - 600 + BROKER),
        _deal(position_id=pid, entry=1, type=1, time=close_utc + BROKER,
              profit=profit),
    ]


BEFORE = DAY_START - 1800          # 23:30 BST on the 9th
AFTER = DAY_START + 1800           # 00:30 BST on the 10th
DEALS = _position(1, BEFORE, -44.22) + _position(2, AFTER, 5.0)


@pytest.fixture
def bst(monkeypatch):
    from backend.src.services.risk import clock
    monkeypatch.setattr(clock, "offset_minutes", lambda: 60)
    monkeypatch.setattr(clock, "day_start_ts", lambda: DAY_START, raising=False)


# ── the trade table and the curve ────────────────────────────────────────────

async def test_today_keeps_only_closes_since_local_midnight(maps, bst):
    table = await trade_table.closed_trades(_Engine(DEALS), 1, today=True)
    assert [r["ticket"] for r in table["rows"]] == [2]
    assert table["curve"]["trades"] == 1


async def test_today_asks_for_enough_history_to_cover_the_day(maps, bst):
    engine = _Engine(DEALS)
    await trade_table.closed_trades(engine, 1, today=True)
    assert engine.calls and engine.calls[0] >= 2


async def test_without_today_the_window_is_unchanged(maps, bst):
    table = await trade_table.closed_trades(_Engine(DEALS), 1)
    assert sorted(r["ticket"] for r in table["rows"]) == [1, 2]


async def test_each_row_carries_its_trading_clock_day(maps, bst):
    table = await trade_table.closed_trades(_Engine(DEALS), 1)
    days = {r["ticket"]: r["day"] for r in table["rows"]}
    assert days == {1: "2026-10-09", 2: "2026-10-10"}


# ── the performance figures ──────────────────────────────────────────────────

class _Bridge:
    def __init__(self, deals):
        self.deals = deals
        self.days = []

    async def get_account(self):
        return {"balance": 1_000.0, "equity": 1_000.0}

    async def get_deal_history(self, days):
        self.days.append(days)
        return self.deals

    async def get_positions(self):
        return []


@pytest.fixture
def no_fees(monkeypatch):
    monkeypatch.setattr(mt5_performance, "_platform_fee_rate", lambda: 0.0)


async def test_performance_counts_only_since_the_given_start(no_fees):
    perf = await mt5_performance.compute_mt5_performance(
        _Bridge(DEALS), 2, since_ts=DAY_START)
    assert perf["closed_trades"] == 1
    assert perf["total_net_pnl"] == pytest.approx(5.0)


async def test_performance_without_a_start_is_unchanged(no_fees):
    perf = await mt5_performance.compute_mt5_performance(_Bridge(DEALS), 2)
    assert perf["closed_trades"] == 2
    assert perf["total_net_pnl"] == pytest.approx(-39.22)

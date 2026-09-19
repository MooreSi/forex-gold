"""The header's P&L figure: equity minus what was actually paid in.

The NiceGUI header computed this inline in its 1-second update loop, reaching
through `engine._bridge.get_deal_history(3650)` and caching the deposit total
in a list-of-one closure variable. The number is worth keeping and the shape
is not: a ten-year deal history fetched from a render loop is the same mistake
bugs/030 documents, and a cache that lives in a closure cannot be tested.

What makes it a real figure rather than a nice-to-have: `total_net_pnl` on the
Analysis tab is the P&L of CLOSED TRADES in a window. This is the account's
whole life including deposits, withdrawals, swap and commission -- the only
number on screen that answers "am I up or down overall".
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import lifetime_pnl


class _Engine:
    """Just enough runtime to answer `get_deal_history`."""

    def __init__(self, deals, fail=False):
        self.deals = deals
        self.fail = fail
        self.calls = 0

    async def get_deal_history(self, days):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bridge is down")
        self.requested_days = days
        return self.deals


def _balance_deal(profit):
    """An MT5 balance operation: type 2, no position, no symbol."""
    return {"type": 2, "profit": profit, "position_id": 0, "symbol": ""}


def _trade_deal(profit):
    return {"type": 0, "profit": profit, "position_id": 771, "symbol": "XAUUSD"}


@pytest.fixture(autouse=True)
def _clear_cache():
    lifetime_pnl.reset_cache()
    yield
    lifetime_pnl.reset_cache()


class TestWhatCountsAsADeposit:

    @pytest.mark.asyncio
    async def test_a_credit_is_money_paid_in(self):
        eng = _Engine([_balance_deal(5000.0)])

        assert await lifetime_pnl.net_deposited(eng) == 5000.0

    @pytest.mark.asyncio
    async def test_a_withdrawal_is_subtracted_not_added(self):
        # A negative balance operation is money taken OUT. Summed as an
        # absolute value it would read as a second deposit, and the P&L
        # figure would go DOWN every time the owner took a profit out.
        eng = _Engine([_balance_deal(5000.0), _balance_deal(-1200.0)])

        assert await lifetime_pnl.net_deposited(eng) == 3800.0

    @pytest.mark.asyncio
    async def test_trading_profit_is_not_a_deposit(self):
        # type 0/1 are entry and exit deals. Counting a winning trade as a
        # deposit would cancel the profit it just made out of the figure.
        eng = _Engine([_balance_deal(5000.0), _trade_deal(430.0), _trade_deal(-88.0)])

        assert await lifetime_pnl.net_deposited(eng) == 5000.0

    @pytest.mark.asyncio
    async def test_it_asks_for_the_whole_account_history(self):
        # A 30-day window would miss the opening deposit on any account older
        # than a month, which is every account this matters for.
        eng = _Engine([_balance_deal(5000.0)])

        await lifetime_pnl.net_deposited(eng)

        assert eng.requested_days >= 3650


class TestTheFigureItself:

    @pytest.mark.asyncio
    async def test_equity_above_deposits_is_a_profit(self):
        eng = _Engine([_balance_deal(5000.0)])

        assert await lifetime_pnl.since_inception(eng, 5412.19) == pytest.approx(412.19)

    @pytest.mark.asyncio
    async def test_equity_below_deposits_is_a_loss(self):
        eng = _Engine([_balance_deal(5308.0)])

        assert await lifetime_pnl.since_inception(eng, 480.91) == pytest.approx(-4827.09)

    @pytest.mark.asyncio
    async def test_no_deposit_history_means_no_figure_not_a_zero(self):
        # An account whose deposits cannot be read would show its entire
        # equity as profit. No number is the honest answer.
        eng = _Engine([_trade_deal(430.0)])

        assert await lifetime_pnl.since_inception(eng, 5412.19) is None

    @pytest.mark.asyncio
    async def test_a_bridge_that_cannot_answer_gives_no_figure(self):
        eng = _Engine([], fail=True)

        assert await lifetime_pnl.since_inception(eng, 5412.19) is None


class TestTheCache:

    @pytest.mark.asyncio
    async def test_the_deal_history_is_not_refetched_every_poll(self):
        # The header polls every 5 seconds. Ten years of deals on every one of
        # those is bugs/030 all over again.
        eng = _Engine([_balance_deal(5000.0)])

        for _ in range(5):
            await lifetime_pnl.since_inception(eng, 5000.0)

        assert eng.calls == 1

    @pytest.mark.asyncio
    async def test_a_failed_fetch_is_not_cached_as_an_answer(self):
        # Caching "unknown" for five minutes would hide a bridge that came
        # back thirty seconds later.
        eng = _Engine([], fail=True)
        await lifetime_pnl.since_inception(eng, 5000.0)

        eng.fail = False
        eng.deals = [_balance_deal(5000.0)]

        assert await lifetime_pnl.since_inception(eng, 5412.19) == pytest.approx(412.19)

    @pytest.mark.asyncio
    async def test_the_equity_side_is_never_cached(self):
        # Only the deposit total is slow to change. Equity moves every tick,
        # and a cached P&L would freeze on screen while the account moved.
        eng = _Engine([_balance_deal(5000.0)])

        first = await lifetime_pnl.since_inception(eng, 5100.0)
        second = await lifetime_pnl.since_inception(eng, 5200.0)

        assert (first, second) == (pytest.approx(100.0), pytest.approx(200.0))

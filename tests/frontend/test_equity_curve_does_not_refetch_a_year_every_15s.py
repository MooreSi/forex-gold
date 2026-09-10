"""The equity curve must not pull a year of deals every 15 seconds.

Measured 2026-09-10: one page load produced **388 bridge round-trips in 25
seconds**, and at idle `/history?days=365` ran **4.3 times a minute** forever.
The cause is here:

    ui.timer(15.0, refresh_chart)
    ...
    deals = await engine._bridge.get_deal_history(365) or []

A full year of deal history, re-fetched four times a minute, to redraw a curve
that only changes when a trade closes. Every one of those crosses into the
Wine-hosted MT5 bridge (bugs/030).

**Why caching is safe HERE and not in general.** This is a display-only read.
`monitor_cycle` also reads the broker to manage open trades, and serving THAT
from a cache would mean acting on a stale book — a money bug rather than a
latency one. The TTL below is deliberately scoped to this one panel's history
fetch and nothing else.

The chart still redraws on its own 15 s timer; only the year-long fetch behind
it is shared.
"""
from __future__ import annotations

import asyncio

import pytest

from frontend.pages.history import _equity_curve as ec


class _Bridge:
    def __init__(self):
        self.calls = 0

    async def get_deal_history(self, days):
        self.calls += 1
        return [{"position_id": 1, "entry": 1, "time": 1788000000, "profit": 10.0}]


@pytest.fixture(autouse=True)
def _clear():
    ec._deal_cache.clear()
    yield
    ec._deal_cache.clear()


class TestRepeatedRedrawsShareOneFetch:
    def test_a_second_call_inside_the_window_does_not_refetch(self):
        b = _Bridge()
        clock = {"t": 1000.0}

        asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))
        asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))

        assert b.calls == 1, f"the year of history was fetched {b.calls} times"

    def test_the_cached_rows_are_returned_not_an_empty_list(self):
        b = _Bridge()
        clock = {"t": 1000.0}

        first = asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))
        second = asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))

        assert second == first and second, "the cache returned nothing"

    def test_it_refetches_once_the_window_passes(self):
        """Stale forever would be worse than the cost it saves."""
        b = _Bridge()
        clock = {"t": 1000.0}

        asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))
        clock["t"] += ec._DEAL_CACHE_TTL_S + 1
        asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))

        assert b.calls == 2


class TestItStaysAWindowWorthHaving:
    def test_the_window_outlives_the_redraw_timer(self):
        """A TTL shorter than the 15 s redraw would cache nothing at all."""
        assert ec._DEAL_CACHE_TTL_S > 15.0

    def test_a_failed_fetch_is_not_cached_as_success(self):
        """Caching an error would blank the chart for the whole window."""
        class _Boom:
            calls = 0
            async def get_deal_history(self, days):
                _Boom.calls += 1
                raise RuntimeError("bridge down")

        b = _Boom()
        clock = {"t": 1000.0}
        for _ in range(2):
            try:
                asyncio.run(ec.cached_deal_history(b, 365, now=lambda: clock["t"]))
            except Exception:
                pass

        assert _Boom.calls == 2, "a failure was cached and hid the retry"

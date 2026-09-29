"""The Analysis table's Spread and Cost columns are filled again.

Found 2026-09-29 (owner: the trades section "has missing info"): every trade
closed since 2026-09-21, 306 of 750 in the 30-day window, showed no spread and
a zero cost. The spread cache was written by the NiceGUI History page, which
fetched the entry-time tick for each ticket it had not seen. The React port
kept the read and dropped the write, so nothing has filled the cache since.

The tick comes from `get_ticks_range`, already on the runtime facade: the
first tick in the minute from the entry is what the bridge's own `/tick_at`
returns (`copy_ticks_from(ts, 1)`), and adding `get_tick_at` to the facade
would raise its baseline.

Nothing here reaches a broker: the engine is a stub with canned ticks, and
the cache write is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import entry_spread
from backend.src.services.analytics import trade_table
from tests.services.analytics.test_trade_table import _Engine, _deal, maps  # noqa: F401

pytestmark = pytest.mark.asyncio

_T0 = 1_790_000_000.0


class _TickEngine(_Engine):
    def __init__(self, deals=None, ticks=None, raises=None):
        super().__init__(deals)
        self.ticks = ticks if ticks is not None else {}
        self.tick_raises = raises
        self.tick_calls: list[tuple[float, float]] = []

    async def get_ticks_range(self, from_ts, to_ts):
        self.tick_calls.append((from_ts, to_ts))
        if self.tick_raises:
            raise self.tick_raises
        return list(self.ticks.get(from_ts, []))


@pytest.fixture
def cache(monkeypatch):
    written: list[tuple] = []

    async def _cache(ticket, price, points, cost):
        written.append((ticket, price, points, cost))

    monkeypatch.setattr(entry_spread._cache, "cache", _cache)
    monkeypatch.setattr(entry_spread, "_misses", set())
    return written


class TestTheBackfill:
    async def test_the_spread_is_the_first_tick_from_the_entry(self, cache):
        engine = _TickEngine(ticks={_T0: [
            {"time": _T0 + 0.4, "bid": 4170.00, "ask": 4170.24},
            {"time": _T0 + 2.0, "bid": 4170.10, "ask": 4170.90},
        ]})

        got = await entry_spread.fill(engine, [(7, _T0, 0.10)])

        assert got[7]["spread_points"] == pytest.approx(24.0)
        # 0.24 price x 0.10 lots x 100 oz.
        assert got[7]["spread_cost_usd"] == pytest.approx(2.40)
        assert cache == [(7, pytest.approx(0.24), pytest.approx(24.0), pytest.approx(2.40))]

    async def test_it_asks_for_a_short_window_from_the_entry(self, cache):
        engine = _TickEngine()

        await entry_spread.fill(engine, [(7, _T0, 0.10)])

        (frm, to), = engine.tick_calls
        assert frm == _T0
        assert 0 < to - frm <= 120

    async def test_no_ticks_leaves_it_blank_and_is_not_asked_again(self, cache):
        engine = _TickEngine()

        assert await entry_spread.fill(engine, [(7, _T0, 0.10)]) == {}
        await entry_spread.fill(engine, [(7, _T0, 0.10)])

        assert len(engine.tick_calls) == 1
        assert cache == []

    async def test_one_read_fills_a_bounded_number_newest_first(self, cache):
        """A cold cache is hundreds of tickets. Asking for all of them on one
        poll is what made the table slow in the NiceGUI days; the rest are
        filled on the next polls."""
        needs = [(i, _T0 + i, 0.01) for i in range(entry_spread.PER_READ + 25)]
        engine = _TickEngine(ticks={ts: [{"time": ts, "bid": 1.0, "ask": 1.2}]
                                    for _i, ts, _l in needs})

        got = await entry_spread.fill(engine, needs)

        assert len(engine.tick_calls) == entry_spread.PER_READ
        newest = max(i for i, _ts, _l in needs)
        assert newest in got

    async def test_a_bridge_failure_stops_this_read_and_loses_nothing(self, cache):
        engine = _TickEngine(raises=RuntimeError("bridge down"))

        got = await entry_spread.fill(engine, [(i, _T0 + i, 0.1) for i in range(20)])

        assert got == {}
        assert len(engine.tick_calls) <= entry_spread.CONCURRENCY
        assert cache == []

    async def test_an_engine_without_ticks_is_no_spread_not_a_crash(self, cache):
        assert await entry_spread.fill(_Engine(), [(7, _T0, 0.1)]) == {}


class TestTheTable:
    async def test_an_uncached_trade_gets_its_spread_and_cost(self, maps, cache):
        engine = _TickEngine(
            deals=[_deal(time=_T0), _deal(entry=1, type=1, time=_T0 + 60)],
            ticks={_T0: [{"time": _T0, "bid": 2400.00, "ask": 2400.30}]})

        row = (await trade_table.closed_trades(engine, 30))["rows"][0]

        assert row["spread_points"] == pytest.approx(30.0)
        assert row["fees"] == pytest.approx(3.00)

    async def test_a_cached_trade_is_not_asked_for_again(self, maps, cache):
        maps["spreads"] = {100: {"spread_points": 2.4, "spread_cost_usd": 2.40}}
        engine = _TickEngine(deals=[_deal(time=_T0), _deal(entry=1, type=1, time=_T0 + 60)])

        await trade_table.closed_trades(engine, 30)

        assert engine.tick_calls == []

    async def test_a_position_opened_before_the_window_is_not_asked_for(self, maps, cache):
        """No entry deal, no entry time: nothing to look up."""
        engine = _TickEngine(deals=[_deal(entry=1, type=1, time=_T0 + 60)])

        await trade_table.closed_trades(engine, 30)

        assert engine.tick_calls == []

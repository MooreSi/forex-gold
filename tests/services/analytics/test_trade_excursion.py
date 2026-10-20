"""How far each closed trade went against us and in our favour (docs/todo/024).

MAE (maximum adverse excursion) and MFE (maximum favourable excursion) were
measured only for Reversal Engine research, from ticks. Every trade in the
Analysis table now gets them, from candles: tick-history reads hang on the
broker's current access point (5b08eeb9, a105320a).

The bars come from `engine.get_candles`, which stamps them in MT5 server time,
the same clock as the deal times. No offset is applied anywhere, so the
broker's switch between UTC+3 and UTC+2 cannot shift a trade onto the wrong
minutes.

Nothing here reaches a broker: the engine is a stub with canned bars, and the
cache write is a recorder.
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import trade_excursion as tx

_M1 = 60
_T0 = 1_790_000_040.0  # 40 s into a minute that starts at 1_790_000_000


def _bar(ts, high, low):
    return {"ts": ts, "open": low, "high": high, "low": low, "close": high}


def _bars(*rows):
    base = 1_790_000_000
    return [_bar(base + i * _M1, h, l) for i, (h, l) in enumerate(rows)]


class TestMeasure:
    def test_a_buy_measures_both_ways_from_the_interior_bars(self):
        bars = _bars((4001.0, 3999.0),   # entry bar
                     (4004.0, 3997.5),   # interior: low 3997.5, high 4004
                     (4006.5, 4001.0),   # interior: high 4006.5
                     (4003.0, 4001.5))   # exit bar
        got = tx.measure("BUY", 4000.0, 4002.0, _T0, _T0 + 3 * _M1, bars, _M1)

        assert got["mfe"] == pytest.approx(6.5)
        assert got["mae"] == pytest.approx(2.5)

    def test_a_sell_is_the_mirror(self):
        bars = _bars((4001.0, 3999.0),
                     (4004.0, 3997.5),
                     (4006.5, 4001.0),
                     (4003.0, 4001.5))
        got = tx.measure("SELL", 4000.0, 4002.0, _T0, _T0 + 3 * _M1, bars, _M1)

        assert got["mfe"] == pytest.approx(2.5)
        assert got["mae"] == pytest.approx(6.5)

    def test_the_entry_and_exit_bars_count_only_their_prices(self):
        # The entry bar dipped to 3990 BEFORE the fill and the exit bar spiked
        # to 4020 AFTER the close. Neither was price the trade held.
        bars = _bars((4000.5, 3990.0),
                     (4001.0, 3999.5),
                     (4020.0, 4000.5))
        got = tx.measure("BUY", 4000.0, 4001.0, _T0, _T0 + 2 * _M1, bars, _M1)

        assert got["mae"] == pytest.approx(0.5)
        assert got["mfe"] == pytest.approx(1.0)

    def test_a_trade_inside_one_bar_is_its_entry_and_exit(self):
        bars = _bars((4010.0, 3990.0))
        got = tx.measure("BUY", 4000.0, 3998.0, _T0, _T0 + 10, bars, _M1)

        assert got == {"mae": pytest.approx(2.0), "mfe": pytest.approx(0.0)}

    def test_a_loser_never_shows_less_adverse_than_its_loss(self):
        bars = _bars((4000.5, 3999.5), (4000.0, 3999.0), (3996.0, 3995.0))
        got = tx.measure("BUY", 4000.0, 3995.0, _T0, _T0 + 2 * _M1, bars, _M1)

        assert got["mae"] >= 5.0

    def test_an_entry_price_its_bar_never_traded_is_unmeasurable(self):
        # The bars and the deal disagree about the clock (or the price): the
        # figure would be from the wrong minutes, so there is none.
        bars = _bars((3950.0, 3940.0), (3951.0, 3941.0), (3952.0, 3942.0))
        assert tx.measure("BUY", 4000.0, 3945.0, _T0, _T0 + 2 * _M1, bars, _M1) is None

    def test_an_exit_price_its_bar_never_traded_is_unmeasurable(self):
        bars = _bars((4000.5, 3999.5), (4001.0, 3999.0), (4001.0, 3999.0))
        assert tx.measure("BUY", 4000.0, 4050.0, _T0, _T0 + 2 * _M1, bars, _M1) is None

    def test_the_ask_side_allowance_lets_a_buy_fill_above_the_bid_high(self):
        # Bars are bid. A buy fills at the ask, a spread above the bid.
        bars = _bars((3999.7, 3999.0), (4001.0, 3999.5))
        got = tx.measure("BUY", 4000.0, 4000.5, _T0, _T0 + _M1, bars, _M1)

        assert got is not None

    def test_no_bar_covering_the_entry_is_unmeasurable(self):
        bars = _bars((4001.0, 3999.0), (4001.0, 3999.0))
        assert tx.measure("BUY", 4000.0, 4000.5, _T0 - 10 * _M1, _T0 + _M1, bars, _M1) is None

    def test_no_bar_covering_the_close_is_unmeasurable(self):
        bars = _bars((4001.0, 3999.0), (4001.0, 3999.0))
        assert tx.measure("BUY", 4000.0, 4000.5, _T0, _T0 + 10 * _M1, bars, _M1) is None


class _Engine:
    def __init__(self, bars_by_tf=None, raises=None):
        self.bars_by_tf = bars_by_tf or {}
        self.raises = raises
        self.calls: list[tuple[str, int]] = []

    async def get_candles(self, timeframe="M5", count=200):
        self.calls.append((timeframe, count))
        if self.raises:
            raise self.raises
        return list(self.bars_by_tf.get(timeframe, []))


@pytest.fixture
def cache(monkeypatch):
    written: list[tuple] = []

    async def _write(ticket, mae, mfe, res):
        written.append((ticket, mae, mfe, res))

    monkeypatch.setattr(tx._cache, "cache", _write)
    monkeypatch.setattr(tx, "_gaps", set())
    return written


def _need(ticket, open_ts, close_ts, entry=4000.0, exit_price=4001.0, direction="BUY"):
    return (ticket, direction, entry, exit_price, open_ts, close_ts)


@pytest.mark.asyncio
class TestFill:
    async def test_it_measures_and_caches_from_one_m1_read(self, cache):
        bars = _bars((4000.5, 3999.5), (4003.0, 3998.0), (4001.5, 4000.5))
        engine = _Engine({"M1": bars})

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 2 * _M1),
                                     _need(8, _T0, _T0 + 2 * _M1)])

        assert engine.calls and all(tf == "M1" for tf, _ in engine.calls)
        assert len(engine.calls) == 1
        assert got[7] == {"mae": pytest.approx(2.0), "mfe": pytest.approx(3.0), "res": "M1"}
        assert sorted(t for t, *_ in cache) == [7, 8]

    async def test_an_unmeasurable_trade_is_remembered_not_retried(self, cache):
        bars = _bars((3950.0, 3940.0), (3951.0, 3941.0), (3952.0, 3942.0))
        engine = _Engine({"M1": bars})

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 2 * _M1)])

        assert got[7] == {"mae": None, "mfe": None, "res": "none"}
        assert cache == [(7, None, None, "none")]

    async def test_a_trade_older_than_m1_reaches_is_measured_on_m5(self, cache):
        m1 = _bars((4000.5, 3999.5))           # starts at _T0's minute
        old = _T0 - 3 * 86_400
        m5_base = int(old) - int(old) % 300
        m5 = [_bar(m5_base, 4000.5, 3999.5), _bar(m5_base + 300, 4004.0, 3999.0),
              _bar(m5_base + 600, 4001.5, 4000.5)]
        engine = _Engine({"M1": m1, "M5": m5})

        got = await tx.fill(engine, [_need(9, old, old + 600)])

        assert [tf for tf, _ in engine.calls] == ["M1", "M5"]
        assert got[9]["res"] == "M5"
        assert got[9]["mfe"] == pytest.approx(4.0)
        assert got[9]["mae"] == pytest.approx(1.0)

    async def test_a_trade_older_than_any_bar_is_remembered_as_none(self, cache):
        engine = _Engine({"M1": _bars((4001.0, 3999.0)), "M5": _bars((4001.0, 3999.0))})

        got = await tx.fill(engine, [_need(9, _T0 - 400 * 86_400, _T0 - 399 * 86_400)])

        assert got[9]["res"] == "none"
        assert cache == [(9, None, None, "none")]

    async def test_a_bridge_failure_records_nothing(self, cache):
        engine = _Engine(raises=RuntimeError("bridge down"))

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 60)])

        assert got == {}
        assert cache == []

    async def test_no_bars_is_a_failure_not_a_verdict(self, cache):
        engine = _Engine({"M1": []})

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 60)])

        assert got == {}
        assert cache == []

    async def test_bounded_per_read_and_newest_first(self, cache):
        bars = _bars(*[(4001.0, 3999.0)] * 5)
        engine = _Engine({"M1": bars})
        needs = [_need(i, _T0, _T0 + 60 + i * 0.001) for i in range(tx.PER_READ + 25)]

        got = await tx.fill(engine, needs)

        assert len(got) == tx.PER_READ
        newest = sorted(needs, key=lambda n: n[5], reverse=True)[:tx.PER_READ]
        assert set(got) == {n[0] for n in newest}

    async def test_the_read_never_asks_for_more_than_the_history_cap(self, cache):
        engine = _Engine({"M1": _bars((4001.0, 3999.0))})

        await tx.fill(engine, [_need(7, 1_000_000.0, 1_000_060.0)])

        assert all(count <= tx.MAX_BARS for _, count in engine.calls)

    async def test_an_engine_without_candles_is_left_alone(self, cache):
        class _NoCandles:
            pass

        assert await tx.fill(_NoCandles(), [_need(7, _T0, _T0 + 60)]) == {}
        assert cache == []

    async def test_a_need_without_an_entry_price_is_skipped(self, cache):
        engine = _Engine({"M1": _bars((4001.0, 3999.0))})

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 30, entry=0.0)])

        assert got == {}
        assert engine.calls == []


class TestRowFields:
    def test_pips_dollars_and_capture(self):
        got = tx.row_fields({"mae": 2.0, "mfe": 5.0, "res": "M1"},
                            pips=30.0, lots=0.10, contract_size=100)

        assert got == {"mae_pips": pytest.approx(20.0), "mfe_pips": pytest.approx(50.0),
                       "mae_usd": pytest.approx(20.0), "mfe_usd": pytest.approx(50.0),
                       "capture": pytest.approx(0.6), "excursion_res": "M1"}

    def test_no_measurement_is_all_none(self):
        got = tx.row_fields(None, pips=30.0, lots=0.10, contract_size=100)

        assert set(got.values()) == {None}

    def test_an_unmeasurable_trade_says_so(self):
        got = tx.row_fields({"mae": None, "mfe": None, "res": "none"},
                            pips=30.0, lots=0.10, contract_size=100)

        assert got["excursion_res"] == "none"
        assert got["mae_pips"] is None and got["capture"] is None

    def test_zero_mfe_has_no_capture(self):
        got = tx.row_fields({"mae": 2.0, "mfe": 0.0, "res": "M1"},
                            pips=-20.0, lots=0.10, contract_size=100)

        assert got["capture"] is None


# -- The Analysis table carries them ----------------------------------------

from backend.src.services.analytics import trade_table  # noqa: E402
from tests.services.analytics.test_trade_table import _deal, maps  # noqa: E402,F401


class _TableEngine(_Engine):
    def __init__(self, deals, bars_by_tf=None):
        super().__init__(bars_by_tf)
        self.deals = deals

    async def get_deal_history(self, days=7):
        return self.deals


@pytest.mark.asyncio
class TestTheTable:
    async def test_a_cached_excursion_reaches_the_row(self, maps, monkeypatch):
        async def _cached(tickets):
            return {100: {"mae": 1.5, "mfe": 4.0, "res": "M1"}}
        monkeypatch.setattr(tx._cache, "get_cached", _cached)
        engine = _TableEngine([_deal(time=_T0),
                               _deal(entry=1, type=1, price=2402.0, time=_T0 + 600)])

        row, = (await trade_table.closed_trades(engine, 30))["rows"]

        assert engine.calls == []  # cached: the bridge is not asked
        assert row["mae_pips"] == pytest.approx(15.0)
        assert row["mfe_pips"] == pytest.approx(40.0)
        # 2400 -> 2402 is 20 pips banked of the 40 on offer.
        assert row["capture"] == pytest.approx(0.5)
        assert row["excursion_res"] == "M1"

    async def test_an_unseen_trade_is_measured_with_its_own_prices_and_times(
            self, maps, monkeypatch, cache):
        async def _cached(tickets):
            return {}
        monkeypatch.setattr(tx._cache, "get_cached", _cached)
        seen = []

        async def _fill(engine, needs):
            seen.extend(needs)
            return {100: {"mae": 0.5, "mfe": 2.5, "res": "M1"}}
        monkeypatch.setattr(tx, "fill", _fill)
        engine = _TableEngine([_deal(type=1, price=2400.0, time=_T0),
                               _deal(entry=1, type=0, price=2399.0, time=_T0 + 600)])

        row, = (await trade_table.closed_trades(engine, 30))["rows"]

        assert seen == [(100, "SELL", 2400.0, 2399.0, _T0, _T0 + 600)]
        assert row["mfe_pips"] == pytest.approx(25.0)

    async def test_a_still_open_position_is_not_measured(self, maps, monkeypatch):
        async def _cached(tickets):
            return {}
        monkeypatch.setattr(tx._cache, "get_cached", _cached)
        seen = []

        async def _fill(engine, needs):
            seen.extend(needs)
            return {}
        monkeypatch.setattr(tx, "fill", _fill)

        await trade_table.closed_trades(_TableEngine([_deal(time=_T0)]), 30)

        assert seen == []


# -- Gaps in the terminal's own history --------------------------------------
#
# Found on the demo account, 2026-10-20: 11 of 44 trades fell where the
# terminal held no M1 bars at all (the bridge was torn down 46 times on
# 2026-10-19, and froze again that morning). A missing bar is not a verdict on
# the trade -- MT5 may backfill it -- and a gap in the MIDDLE of a trade would
# silently understate both figures. So: no coverage, no number, and no
# permanent record either.

_DAY = 86_400


class TestCovered:
    def test_contiguous_bars_cover(self):
        bars = _bars((1, 0), (1, 0), (1, 0))
        assert tx.covered(bars, _T0, _T0 + 2 * _M1, _M1)

    def test_a_missing_entry_bar_does_not(self):
        bars = _bars((1, 0), (1, 0))[1:]
        assert not tx.covered(bars, _T0, _T0 + _M1, _M1)

    def test_a_missing_exit_bar_does_not(self):
        bars = _bars((1, 0), (1, 0))
        assert not tx.covered(bars, _T0, _T0 + 5 * _M1, _M1)

    def test_a_hole_in_the_middle_of_the_day_does_not(self):
        base = 1_790_000_000 - 1_790_000_000 % _DAY + 10 * 3600   # 10:00 server time
        bars = [_bar(base, 1, 0), _bar(base + 60, 1, 0),
                _bar(base + 60 + 30 * 60, 1, 0)]                 # 30 minutes missing
        assert not tx.covered(bars, base + 5, base + 60 + 30 * 60 + 5, _M1)

    def test_a_short_quiet_spell_is_not_a_hole(self):
        base = 1_790_000_000 - 1_790_000_000 % _DAY + 10 * 3600
        bars = [_bar(base, 1, 0), _bar(base + 3 * 60, 1, 0)]     # two quiet minutes
        assert tx.covered(bars, base + 5, base + 3 * 60 + 5, _M1)

    def test_the_daily_break_at_server_midnight_is_not_a_hole(self):
        midnight = 1_790_000_000 - 1_790_000_000 % _DAY + _DAY
        bars = [_bar(midnight - 120, 1, 0), _bar(midnight - 60, 1, 0),
                _bar(midnight + 3600, 1, 0)]                     # market shut 00:00-01:00
        assert tx.covered(bars, midnight - 115, midnight + 3605, _M1)

    def test_the_weekend_is_not_a_hole(self):
        friday = 1_790_000_000 - 1_790_000_000 % _DAY + _DAY - 120
        bars = [_bar(friday, 1, 0), _bar(friday + 60, 1, 0),
                _bar(friday + 2 * _DAY + 3720, 1, 0)]
        assert tx.covered(bars, friday + 5, friday + 2 * _DAY + 3725, _M1)


@pytest.mark.asyncio
class TestGapsAreNotVerdicts:
    async def test_a_trade_in_a_gap_is_not_cached(self, cache):
        bars = _bars((4001.0, 3999.0), (4001.0, 3999.0))       # no bar at the exit
        engine = _Engine({"M1": bars})

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 5 * _M1)])

        assert got == {}
        assert cache == []

    async def test_it_is_not_asked_again_in_this_process(self, cache):
        bars = _bars((4001.0, 3999.0), (4001.0, 3999.0))
        engine = _Engine({"M1": bars})

        await tx.fill(engine, [_need(7, _T0, _T0 + 5 * _M1)])
        engine.calls.clear()
        await tx.fill(engine, [_need(7, _T0, _T0 + 5 * _M1)])

        assert engine.calls == []

    async def test_a_gap_does_not_hold_up_the_others(self, cache):
        bars = _bars((4000.5, 3999.5), (4003.0, 3998.0), (4001.5, 4000.5))
        engine = _Engine({"M1": bars})

        got = await tx.fill(engine, [_need(7, _T0, _T0 + 2 * _M1),
                                     _need(8, _T0, _T0 + 9 * _M1)])   # exit bar missing

        assert set(got) == {7}
        assert [t for t, *_ in cache] == [7]

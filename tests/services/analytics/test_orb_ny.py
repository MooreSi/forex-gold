"""The New York ORB's decision, on hand-built M5 bars (analytics/orb_ny.py).

Monday 2026-07-20: New York is on EDT, so 09:30 there is 13:30 UTC, the
opening range closes at 14:00 UTC and the entry window at 15:30 UTC.
"""
from datetime import datetime, timezone

import pytest

from backend.src.services.analytics import orb_ny

OPEN = datetime(2026, 7, 20, 13, 30, tzinfo=timezone.utc).timestamp()
OR_END = OPEN + 30 * 60
UP_TREND = [2000.0] * 60 + [2100.0]      # last close far above its EMA50
DOWN_TREND = [2100.0] * 60 + [2000.0]


def _bar(ts, o, h, l, c):
    return {"ts": ts, "open": o, "high": h, "low": l, "close": c}


def _range_bars():
    """Six M5 bars spanning 2400-2410."""
    return [_bar(OPEN + i * 300, 2405, 2410, 2400, 2405) for i in range(6)]


def test_new_york_opens_at_1330_utc_in_summer_and_1430_in_winter():
    summer = datetime(2026, 7, 20, 12, 0, tzinfo=timezone.utc)
    winter = datetime(2026, 1, 12, 12, 0, tzinfo=timezone.utc)
    assert orb_ny.open_utc(summer) == OPEN
    assert orb_ny.open_utc(winter) == datetime(2026, 1, 12, 14, 30, tzinfo=timezone.utc).timestamp()


def test_nothing_before_the_open_and_forming_inside_the_range():
    assert orb_ny.evaluate([], UP_TREND, OPEN - 60, 2404, 2404.3)["phase"] == "before"
    assert orb_ny.evaluate(_range_bars()[:3], UP_TREND, OPEN + 900, 2404, 2404.3)["phase"] == "forming"


def test_a_bullish_close_with_the_trend_is_a_signal_stopped_at_the_far_side():
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    now = OR_END + 300 + 30
    r = orb_ny.evaluate(bars, UP_TREND, now, 2411.0, 2411.3)
    assert r["phase"] == "signal" and r["direction"] == "bullish"
    assert r["stop"] == 2400.0                       # the range's far side
    assert r["current_price"] == 2411.3              # a buy pays the ask
    assert r["target"] == pytest.approx(2411.3 + 2 * 11.3)


def test_a_bearish_close_with_a_downtrend_sells_at_the_bid():
    bars = _range_bars() + [_bar(OR_END, 2402, 2403, 2398, 2399)]
    r = orb_ny.evaluate(bars, DOWN_TREND, OR_END + 330, 2399.0, 2399.3)
    assert r["phase"] == "signal" and r["direction"] == "bearish"
    assert r["stop"] == 2410.0
    assert r["target"] == pytest.approx(2399.0 - 2 * 11.0)


def test_a_breakout_against_the_h4_trend_skips_the_day():
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    r = orb_ny.evaluate(bars, DOWN_TREND, OR_END + 330, 2411.0, 2411.3)
    assert r["phase"] == "done" and r["direction"] == "inside"
    assert "against" in r["position_note"]


def test_the_first_breakout_decides_the_day():
    """A counter-trend break first, then a with-trend one: the day was the
    first, and it was skipped. Hunting for a second would be a different
    strategy from the one replayed."""
    bars = _range_bars() + [_bar(OR_END, 2402, 2403, 2398, 2399),
                            _bar(OR_END + 300, 2405, 2412, 2404, 2411)]
    r = orb_ny.evaluate(bars, UP_TREND, OR_END + 630, 2411.0, 2411.3)
    assert r["phase"] == "done"


def test_a_breakout_bar_that_closed_too_far_out_is_not_chased():
    bars = _range_bars() + [_bar(OR_END, 2408, 2416, 2407, 2415)]   # 5 past a 10 range
    r = orb_ny.evaluate(bars, UP_TREND, OR_END + 330, 2415.0, 2415.3)
    assert r["phase"] == "done" and "chasing" in r["position_note"]


def test_price_that_has_since_run_away_is_not_chased():
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    r = orb_ny.evaluate(bars, UP_TREND, OR_END + 330, 2419.0, 2419.3)
    assert r["phase"] == "done"


def test_a_missed_breakout_is_not_taken_late():
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    r = orb_ny.evaluate(bars, UP_TREND, OR_END + 300 + orb_ny.FRESH_S + 60, 2411.0, 2411.3)
    assert r["phase"] == "done" and "missed" in r["position_note"]


def test_the_forming_bar_is_not_a_breakout():
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    r = orb_ny.evaluate(bars, UP_TREND, OR_END + 200, 2411.0, 2411.3)
    assert r["phase"] == "watching"


def test_no_close_beyond_the_range_in_the_window_ends_the_day():
    bars = _range_bars() + [_bar(OR_END + i * 300, 2405, 2409, 2401, 2405) for i in range(18)]
    end = OR_END + orb_ny.ENTRY_WINDOW_MINUTES * 60
    assert orb_ny.evaluate(bars, UP_TREND, end - 60, 2405, 2405.3)["phase"] == "watching"
    assert orb_ny.evaluate(bars, UP_TREND, end + 60, 2405, 2405.3)["phase"] == "done"


def test_no_trend_without_fifty_h4_closes():
    assert orb_ny.trend([2000.0] * 49) is None
    assert orb_ny.trend(UP_TREND) == "up" and orb_ny.trend(DOWN_TREND) == "down"


def test_a_weekend_is_never_open():
    sat = datetime(2026, 7, 25, 14, 30, tzinfo=timezone.utc).timestamp()
    assert orb_ny.evaluate([], UP_TREND, sat, 2400, 2400.3)["phase"] == "before"


class _Bridge:
    """The bridge's shapes: `/candles_range` in true UTC, `get_candles` with
    the forming bar last."""

    def __init__(self, m5, h4, bid, ask, skew=0.0):
        self.m5, self.h4, self.bid, self.ask, self.skew = m5, h4, bid, ask, skew

    async def get_tick(self):
        from types import SimpleNamespace
        return SimpleNamespace(bid=self.bid, ask=self.ask)

    async def get_candles_range(self, start, end, timeframe="M1"):
        assert timeframe == "M5"
        return [{**b, "ts": b["ts"] + self.skew} for b in self.m5 if start - 300 <= b["ts"] <= end]

    async def get_candles(self, tf, n):
        assert tf == "H4"
        return self.h4[-n:]


def _h4(closes):
    return [{"ts": i * 14400, "open": c, "high": c, "low": c, "close": c} for i, c in enumerate(closes)]


def test_build_report_reads_the_bridge_and_drops_the_forming_h4_bar():
    import asyncio
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    # The forming H4 bar would flip the trend; it must be ignored.
    bridge = _Bridge(bars, _h4(UP_TREND + [1000.0]), 2411.0, 2411.3)
    r = asyncio.run(orb_ny.build_report(bridge, OR_END + 330))
    assert r["phase"] == "signal" and r["direction"] == "bullish"


def test_build_report_rounds_the_bridges_skewed_stamps():
    """`/candles_range` converts with an offset measured off the last tick,
    unrounded; in a quiet market the stamps come back seconds late. Read
    raw, the breakout bar stamped 14:00:13 is still forming at 14:05:05 and
    the signal is missed."""
    import asyncio
    bars = _range_bars() + [_bar(OR_END, 2408, 2412, 2407, 2411)]
    bridge = _Bridge(bars, _h4(UP_TREND + [2100.0]), 2411.0, 2411.3, skew=13)
    r = asyncio.run(orb_ny.build_report(bridge, OR_END + 305))
    assert r["phase"] == "signal"

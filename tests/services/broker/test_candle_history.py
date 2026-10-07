"""Scroll-back history for the Broker chart (docs/todo/011 phase 2).

The chart asks for "the bars before the oldest one I have". The bridge has no
reliable date-addressed read (`_get_candles_range` itself fetches by position,
see its docstring), so the history is cut from a position read wide enough to
reach back past `before`. These pin the cut: nothing at or after `before`,
nothing out of order, nothing twice, and a read that is bounded however far
back the user scrolls.
"""
from __future__ import annotations

import asyncio

from backend.src.services.broker import candle_history as ch


def _bars(start: int, n: int, step: int = 300) -> list[dict]:
    return [{"ts": float(start + i * step), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5} for i in range(n)]


class _Engine:
    def __init__(self, rows: list[dict]):
        self.rows = rows
        self.asked: list[tuple[str, int]] = []

    async def get_candles(self, timeframe: str = "M5", count: int = 200):
        self.asked.append((timeframe, count))
        return self.rows


def test_only_bars_strictly_before_the_cut_are_returned():
    rows = _bars(300, 10)                     # ts 300, 600, ... 3000
    out = ch.older_than(rows, before=1500.0, count=100)
    assert [r["ts"] for r in out] == [300.0, 600.0, 900.0, 1200.0]


def test_a_bar_with_no_timestamp_is_dropped_not_drawn_at_1970():
    rows = [{"ts": None, "close": 1.0}, {"close": 1.0}] + _bars(300, 2)
    out = ch.older_than(rows, before=10_000.0, count=100)
    assert [r["ts"] for r in out] == [300.0, 600.0]


def test_the_newest_count_bars_before_the_cut_are_kept():
    rows = _bars(0, 10)
    out = ch.older_than(rows, before=1500.0, count=2)
    assert [r["ts"] for r in out] == [900.0, 1200.0]


def test_an_overlapping_or_unordered_read_comes_back_once_and_in_order():
    rows = _bars(300, 4)
    shuffled = [rows[2], rows[0], rows[2], rows[1], rows[3], rows[0]]
    out = ch.older_than(shuffled, before=10_000.0, count=100)
    assert [r["ts"] for r in out] == [300.0, 600.0, 900.0, 1200.0]


def test_the_read_reaches_from_now_back_past_the_cut_by_count_bars():
    # 10 bars of 300 s between `before` and now, plus 50 wanted, plus slack.
    n = ch.bars_needed(now=4000.0, before=1000.0, tf_seconds=300, count=50)
    assert n >= 10 + 50
    assert n <= 10 + 50 + ch.SLACK_BARS


def test_the_read_is_capped_however_far_back_the_user_scrolls():
    n = ch.bars_needed(now=1e9, before=0.0, tf_seconds=60, count=500)
    assert n == ch.MAX_HISTORY_BARS


def test_a_cut_in_the_future_reads_only_count_bars_and_slack():
    n = ch.bars_needed(now=1000.0, before=5000.0, tf_seconds=60, count=100)
    assert n == 100 + ch.SLACK_BARS


def test_history_asks_the_engine_once_with_the_mt5_timeframe_and_width():
    rows = _bars(0, 20)
    eng = _Engine(rows)
    out = asyncio.run(ch.history_before(eng, "M5", before=3000.0, count=4, now=6000.0))
    assert eng.asked == [("M5", ch.bars_needed(6000.0, 3000.0, 300, 4))]
    assert [r["ts"] for r in out] == [1800.0, 2100.0, 2400.0, 2700.0]


def test_an_unknown_mt5_timeframe_reads_nothing():
    eng = _Engine(_bars(0, 5))
    assert asyncio.run(ch.history_before(eng, "W1", before=3000.0, count=4)) == []
    assert eng.asked == []


def test_a_bridge_that_returns_nothing_gives_an_empty_history_not_an_error():
    eng = _Engine([])
    assert asyncio.run(ch.history_before(eng, "M1", before=3000.0, count=4, now=4000.0)) == []

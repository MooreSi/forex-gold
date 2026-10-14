"""docs/todo/017 item 4: the minute sweep that writes each signal's market state,
newest day first, so a new signal is measured within about a minute and the
whole history is backfilled over the following passes.

A fake bridge and in-memory stores; nothing reaches a broker.
"""
from __future__ import annotations

import asyncio
import json

from backend.src.services.market import market_state as ms
from backend.src.services.market import market_state_sweep as sweep

H = 3600.0
DAY = 86400.0
T = 1_760_000_000.0 - (1_760_000_000.0 % DAY) + 10 * H   # 10:00 UTC on a day


class _Bridge:
    def __init__(self, empty=False):
        self.calls = []
        self.empty = empty

    async def get_candles_range_for_symbol(self, symbol, lo, hi, tf):
        self.calls.append((symbol, lo, hi, tf))
        if self.empty:
            return []
        start = lo - (lo % H)
        n = int((hi - start) // H) + 1
        return [{"ts": start + i * H, "open": 100.0 + i, "close": 101.0 + i,
                 "high": 101.5 + i, "low": 99.5 + i} for i in range(n)]


def _store():
    rows = {1: {"id": 1, "created_at": T, "direction": "BUY"},
            2: {"id": 2, "created_at": T + 2 * H, "direction": "SELL"},
            3: {"id": 3, "created_at": T - 3 * DAY, "direction": "BUY"}}
    written = {}

    def missing():
        return sorted((dict(r) for i, r in rows.items() if i not in written),
                      key=lambda r: -r["created_at"])

    def store(pairs):
        for i, payload in pairs:
            written[i] = json.loads(payload)
    return missing, store, written


def _run(bridge, missing, store, skip):
    return asyncio.run(sweep.sweep_one_day(bridge, missing, store, skip, "TEST"))


class TestOnePass:
    def test_the_newest_day_first_and_only_that_day(self):
        missing, store, written = _store()
        n = _run(_Bridge(), missing, store, set())
        assert n == 2 and set(written) == {1, 2}

    def test_the_next_pass_does_the_older_day(self):
        missing, store, written = _store()
        b = _Bridge()
        _run(b, missing, store, set())
        _run(b, missing, store, set())
        assert set(written) == {1, 2, 3}

    def test_it_asks_for_enough_history_and_nothing_after_the_last_signal(self):
        missing, store, _ = _store()
        b = _Bridge()
        _run(b, missing, store, set())
        symbol, lo, hi, tf = b.calls[0]
        assert (symbol, tf) == ("XAUUSD", "H1")
        assert lo <= T - ms.LOOKBACK_S
        assert hi == T + 2 * H

    def test_what_it_writes_is_the_snapshot(self):
        missing, store, written = _store()
        b = _Bridge()
        _run(b, missing, store, set())
        bars = asyncio.run(b.get_candles_range_for_symbol(*b.calls[0]))
        assert written[2] == ms.snapshot(bars, T + 2 * H, "SELL")


class TestNoHistory:
    def test_a_day_the_broker_has_no_bars_for_is_skipped_not_written(self):
        missing, store, written = _store()
        skip = set()
        assert _run(_Bridge(empty=True), missing, store, skip) == 0
        assert written == {} and len(skip) == 1

    def test_the_skipped_day_is_stepped_over_next_time(self):
        missing, store, written = _store()
        skip = set()
        _run(_Bridge(empty=True), missing, store, skip)
        _run(_Bridge(), missing, store, skip)
        assert set(written) == {3}

    def test_no_bridge_does_nothing(self):
        missing, store, written = _store()
        assert _run(None, missing, store, set()) == 0
        assert written == {}

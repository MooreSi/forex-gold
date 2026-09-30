"""The Reversal cycle's ATR describes the market now, not yesterday.

docs/todo/reversal-engine/250, item 1. `_calc_atr` walked
`range(1, period + 1)` over the 80 M15 candles the bridge hands back OLDEST
first, so the "current" ATR was the average range of bars 17-20 hours old.
It sizes the entry zone, the stop under ATR barriers, the fill-time
re-check and the `atr` ML feature. `ict_patterns.atr` has always read
`candles[-period:]`.

Changes what the engine trades: owner sign-off and a demo session before
this merges.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import ict_patterns
from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine


def _bars(rng: float, n: int, mid: float = 4000.0) -> list[dict]:
    return [{"high": mid + rng / 2, "low": mid - rng / 2, "close": mid} for _ in range(n)]


def test_a_quiet_yesterday_does_not_hide_a_wild_now():
    candles = _bars(1.0, 66) + _bars(10.0, 14)      # oldest first, like the bridge

    assert ReversalEngine._calc_atr(candles) == pytest.approx(10.0)


def test_a_wild_yesterday_does_not_inflate_a_quiet_now():
    candles = _bars(10.0, 66) + _bars(1.0, 14)

    assert ReversalEngine._calc_atr(candles) == pytest.approx(1.0)


def test_it_agrees_with_the_ict_atr():
    """Two ATRs in one engine that disagree about which bars are "now" is how
    this went unnoticed."""
    candles = [{"high": 4000 + (i % 7), "low": 3995 - (i % 5), "close": 3998 + (i % 3)}
               for i in range(80)]

    assert ReversalEngine._calc_atr(candles) == pytest.approx(
        ict_patterns.atr(candles), abs=0.01)


def test_too_few_bars_keeps_the_old_fallback():
    assert ReversalEngine._calc_atr([]) == 8.0
    assert ReversalEngine._calc_atr(_bars(3.0, 1)) == 8.0

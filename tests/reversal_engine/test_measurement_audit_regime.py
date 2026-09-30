"""The regime split in docs/todo/reversal-engine/250 step 4 rests on this ADX.

A steady one-way march must read as trending and a flat chop as ranging, or
the "ranging" and "trending" cells are the same population under two names.
And a signal may only see bars that had CLOSED before it.
"""
from __future__ import annotations

from types import SimpleNamespace

from tools import re_measurement_audit as audit


def _h1(closes, width=1.0):
    return [{"ts": i * 3600, "high": c + width, "low": c - width, "close": c}
            for i, c in enumerate(closes)]


def test_a_steady_march_reads_as_trending():
    adx = audit.wilder_adx(_h1([4000.0 + 3 * i for i in range(80)]))
    assert adx and list(adx.values())[-1] >= audit.TRENDING_FROM


def test_a_flat_chop_reads_as_ranging():
    adx = audit.wilder_adx(_h1([4000.0 + (2 if i % 2 else -2) for i in range(80)]))
    assert adx and list(adx.values())[-1] < audit.RANGING_BELOW


def test_too_little_history_says_nothing():
    assert audit.wilder_adx(_h1([4000.0] * 10)) == {}


def test_a_signal_sees_only_bars_that_have_closed():
    adx = {7200: 10.0, 10800: 40.0}
    closes = sorted(adx)
    assert audit.adx_at(adx, closes, 10799.0) == 10.0
    assert audit.adx_at(adx, closes, 10800.0) == 40.0
    assert audit.adx_at(adx, closes, 100.0) is None


def test_h1_bars_are_built_from_the_minutes_inside_the_hour():
    m1 = [SimpleNamespace(ts=3600 + 60 * k, high=10.0 + k, low=5.0 - k, close=float(k))
          for k in range(60)] + [SimpleNamespace(ts=7200, high=1.0, low=1.0, close=1.0)]
    h1 = audit.h1_from_m1(m1)
    assert h1[0] == {"ts": 3600, "high": 69.0, "low": -54.0, "close": 59.0}
    assert h1[1] == {"ts": 7200, "high": 1.0, "low": 1.0, "close": 1.0}

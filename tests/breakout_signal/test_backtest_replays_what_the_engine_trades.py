"""The walk-forward replays the entries the engine actually takes (2026-10-17).

`run_walkforward` checked break-and-go and break-and-retest only. The live
cycle checks a third, `check_liquidity_sweep`, after them, and sweeps were
140 of the 157 signals the engine had closed. So the harness that exists to
stop a recalibration reaching live ("no counterfactual check", its own
docstring) measured about a tenth of what the engine traded.

It also handed the level finder bars keyed `time`, and
`market/levels._session_range` reads `ts`, so the replay had no Asian
high or low at all.

The indicators are stubbed: what is pinned is the routing, not the market.
"""
from __future__ import annotations

import pytest

from backend.src.services.breakout_signal import backtest as bt

T0 = 1_791_960_000.0          # a Wednesday, 06:40 UTC


def _bars(n, step, start, price=4000.0):
    return [{"time": start + k * step, "open": price, "high": price + 1.0,
             "low": price - 1.0, "close": price} for k in range(n)]


def _candles():
    m5 = _bars(400, 300, T0)
    return {"M5": m5, "M1": _bars(400 * 5 + 600, 60, T0),
            "H1": _bars(200, 3600, T0 - 200 * 3600), "H4": _bars(80, 14400, T0 - 80 * 14400)}


@pytest.fixture
def seen(monkeypatch):
    seen = {"sweeps": 0, "h1": []}

    def _sweep(m5, levels, htf, h4b, price, atr, adx, macd):
        seen["sweeps"] += 1
        if seen["sweeps"] > 1:
            return None
        return {"direction": "SELL", "breakout_type": "sweep", "broken_level": 4001.5,
                "broken_level_type": "swing_high", "level_strength": 3,
                "wick_extreme": 4002.5, "pierce_pts": 1.0}

    def _levels(h1, price, m15_candles=None):
        seen["h1"].append(h1)
        return [{"price": 4001.5, "type": "swing_high", "strength": 3}]

    monkeypatch.setattr(bt, "check_breakout_go", lambda *a: None)
    monkeypatch.setattr(bt, "check_breakout_retest", lambda *a: None)
    monkeypatch.setattr(bt, "check_liquidity_sweep", _sweep, raising=False)
    monkeypatch.setattr(bt, "identify_key_levels", _levels)
    monkeypatch.setattr(bt, "compute_adx", lambda c: 33.0)
    monkeypatch.setattr(bt, "compute_htf_bias", lambda h1: "bearish")
    monkeypatch.setattr(bt, "compute_h4_bias", lambda h4: "bearish")
    monkeypatch.setattr(bt, "compute_macd_hist", lambda closes: (0.0, -0.5))
    return seen


class TestTheSweepIsReplayed:
    def test_a_sweep_candidate_becomes_a_replayed_trade(self, seen):
        trades = bt.run_walkforward(_candles())
        assert [t["breakout_type"] for t in trades] == ["sweep"]

    def test_it_is_asked_only_after_go_and_retest_found_nothing(self, seen, monkeypatch):
        """The live order: a break-and-go wins the cycle and the sweep is
        never asked."""
        monkeypatch.setattr(bt, "check_breakout_go", lambda *a: {
            "direction": "BUY", "breakout_type": "go", "broken_level": 3999.0,
            "broken_level_type": "swing_high", "level_strength": 3})
        bt.run_walkforward(_candles())
        assert seen["sweeps"] == 0


class TestTheLevelFinderGetsStamps:
    def test_every_h1_bar_carries_ts(self, seen):
        bt.run_walkforward(_candles())
        assert seen["h1"] and all("ts" in c for c in seen["h1"][0])

    def test_ts_is_the_bars_own_utc_time(self, seen):
        bt.run_walkforward(_candles())
        assert all(c["ts"] == c["time"] for c in seen["h1"][0])

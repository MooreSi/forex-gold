"""FakeMT5Bridge market data is deterministic and self-consistent.

The fake price stream must be reproducible (same seed + clock → identical
series, so e2e tests can assert exact outcomes) and internally coherent
(candles are aggregations of the same closed-form mid the ticks come
from, and get_tick_at answers from the same curve).

No test in this file can reach a broker: FakeMT5Bridge has no network
code; the clock is injected.
"""
from __future__ import annotations

import asyncio

from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.broker.fake_market import FakeMarket

BASE = 1_700_000_000.0


def _clock_at(ts: float):
    return lambda: ts


def test_same_seed_and_time_produce_identical_ticks():
    a = FakeMT5Bridge(seed=42, clock=_clock_at(BASE + 500))
    b = FakeMT5Bridge(seed=42, clock=_clock_at(BASE + 500))
    ta = asyncio.run(a.get_tick())
    tb = asyncio.run(b.get_tick())
    assert ta.to_dict() == tb.to_dict()
    # Negative control: a different seed genuinely changes the price.
    c = FakeMT5Bridge(seed=43, clock=_clock_at(BASE + 500))
    tc = asyncio.run(c.get_tick())
    assert tc.bid != ta.bid


def test_the_price_actually_moves():
    """A frozen fake reads as 'MT5 Disconnected' — the stream must tick."""
    market = FakeMarket(seed=42)
    mids = {round(market.mid(BASE + s), 2) for s in range(0, 600, 5)}
    assert len(mids) > 10


def test_tick_shape_matches_the_real_client():
    tick = asyncio.run(FakeMT5Bridge(seed=42, clock=_clock_at(BASE)).get_tick())
    assert tick.ask > tick.bid
    assert tick.mid == round((tick.bid + tick.ask) / 2, 2)
    assert tick.spread_points > 0
    assert tick.source == "fake"
    assert tick.timestamp == BASE


def test_get_tick_at_answers_from_the_same_curve():
    bridge = FakeMT5Bridge(seed=42, clock=_clock_at(BASE + 5000))
    live = asyncio.run(bridge.get_tick())
    past = asyncio.run(bridge.get_tick_at(BASE + 5000))
    assert past is not None
    assert past["bid"] == live.bid
    assert past["ask"] == live.ask
    assert past["time"] == int(BASE + 5000)


def test_candles_agree_with_ticks():
    """The last completed M5 bar opens and closes on the same closed-form
    mid the tick stream reports."""
    now = BASE + 10 * 300
    bridge = FakeMT5Bridge(seed=42, base_ts=BASE, clock=_clock_at(now))
    market = FakeMarket(seed=42, base_ts=BASE)
    candles = asyncio.run(bridge.get_candles("M5", 5))
    assert len(candles) == 5
    last = candles[-1]
    assert set(last) == {"ts", "open", "high", "low", "close", "volume"}
    bar_start = last["ts"]
    assert last["open"] == round(market.mid(bar_start), 2)
    assert last["close"] == round(market.mid(bar_start + 300), 2)
    assert last["low"] <= min(last["open"], last["close"])
    assert last["high"] >= max(last["open"], last["close"])
    # Bars tile absolute epoch time: consecutive, 300s apart, the newest
    # ending at the bar boundary at-or-before `now` (as MT5 bars do).
    last_end = int(now // 300) * 300
    assert [c["ts"] for c in candles] == [last_end - 300 * k for k in range(5, 0, -1)]


def test_scripted_scenario_is_deterministic_and_followed():
    """A scenario pins the mid to piecewise-linear anchor points, so an e2e
    test can force a TP hit at a chosen second."""
    scenario = {"anchors": [[0, 2400.0], [100, 2410.0], [200, 2400.0]]}
    m1 = FakeMarket(seed=1, scenario=scenario, base_ts=BASE)
    m2 = FakeMarket(seed=1, scenario=scenario, base_ts=BASE)
    series1 = [round(m1.mid(BASE + s), 2) for s in (0, 50, 100, 150, 200, 999)]
    series2 = [round(m2.mid(BASE + s), 2) for s in (0, 50, 100, 150, 200, 999)]
    assert series1 == series2
    assert series1[0] == 2400.0
    assert series1[1] == 2405.0   # halfway up the first leg
    assert series1[2] == 2410.0
    assert series1[4] == 2400.0
    assert series1[5] == 2400.0   # holds the last anchor after the script ends

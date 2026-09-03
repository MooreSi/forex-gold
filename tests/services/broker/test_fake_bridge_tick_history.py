"""FakeMT5Bridge.get_ticks_range -- the fake side of the real bridge's new
/ticks endpoint (docs/todo/backtest/010 phase 1).

Deterministic and off the same curve as get_tick_at()/get_candles(), the
same guarantee test_fake_bridge_ticks.py pins for the live single-tick
stream -- so a backtest test can drive a tick-walking simulator without a
live broker connection.
"""
from __future__ import annotations

import asyncio

from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.broker.fake_market import FakeMarket

BASE = 1_700_000_000.0


def test_ticks_span_the_requested_window():
    market = FakeMarket(seed=42)
    ticks = market.ticks(BASE, BASE + 10, interval=2.0)

    assert [t["time"] for t in ticks] == [BASE, BASE + 2, BASE + 4, BASE + 6, BASE + 8]


def test_ticks_agree_with_tick_at_the_same_timestamp():
    """Same curve as the live stream -- a backtest replaying ticks and a
    live monitor reading get_tick() must never disagree about the price at
    a given instant."""
    market = FakeMarket(seed=42)
    ticks = market.ticks(BASE, BASE + 1, interval=1.0)
    live = market.tick(BASE)

    assert ticks[0]["bid"] == live.bid
    assert ticks[0]["ask"] == live.ask


def test_ask_is_always_above_bid():
    market = FakeMarket(seed=42)
    for t in market.ticks(BASE, BASE + 60, interval=5.0):
        assert t["ask"] > t["bid"]


def test_same_seed_produces_identical_history():
    a = FakeMarket(seed=7).ticks(BASE, BASE + 20, interval=5.0)
    b = FakeMarket(seed=7).ticks(BASE, BASE + 20, interval=5.0)

    assert a == b
    # Negative control: a different seed genuinely changes the prices.
    c = FakeMarket(seed=8).ticks(BASE, BASE + 20, interval=5.0)
    assert a != c


def test_the_bridge_forwards_to_the_market():
    bridge = FakeMT5Bridge(seed=42, clock=lambda: BASE)
    ticks = asyncio.run(bridge.get_ticks_range(BASE, BASE + 10))

    assert len(ticks) == 10
    assert ticks[0]["time"] == BASE

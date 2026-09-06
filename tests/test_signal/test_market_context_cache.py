"""market_context's 15-minute cache must actually prevent a fetch.

The module docstring has promised a 15-minute cache since it was written, but
`_get_hourly_closes` stored only the LAST close as a packed float. The hit
branch could not rebuild the list it is supposed to return, so it fell straight
through to a re-fetch -- the comment on that line said as much. Every
`get_context()` was therefore five live yfinance round trips.

Breakout survived it by calling once per signal creation. Anything on a timer
would not: spec docs/todo/001-reversal-macro-context.md wants this on the
Reversal Engine's 60s cycle, which would put five blocking HTTP calls inside
the async engine loop every minute.

These tests count FETCHES, not values. Asserting that the second call returns
the same numbers passes vacuously against the broken code -- a re-fetch of a
stubbed source returns the same numbers too.
"""
from __future__ import annotations

import pandas as pd
import pytest

from backend.src.services.test_signal import market_context as mc


class _FakeTicker:
    def __init__(self, closes, counter, symbol):
        self._closes = closes
        self._counter = counter
        self._symbol = symbol

    def history(self, **kwargs):
        self._counter[self._symbol] = self._counter.get(self._symbol, 0) + 1
        return pd.DataFrame({"Close": list(self._closes)})


@pytest.fixture
def fake_yf(monkeypatch):
    """Replace yfinance with a per-symbol fetch counter. Returns the counter."""
    counter: dict[str, int] = {}
    closes = [100.0, 101.0, 102.0, 103.0, 104.0]

    class _FakeYF:
        @staticmethod
        def Ticker(symbol):
            return _FakeTicker(closes, counter, symbol)

    monkeypatch.setattr(mc, "yf", _FakeYF, raising=False)
    monkeypatch.setattr(mc, "_YF_AVAILABLE", True)
    monkeypatch.setattr(mc, "_CACHE", {})
    return counter


class TestTheCachePreventsAFetch:
    def test_second_call_inside_the_ttl_does_not_refetch(self, fake_yf):
        """The property the docstring claims. Broken code counts 2."""
        mc._get_hourly_closes("^VIX", n=3)
        mc._get_hourly_closes("^VIX", n=3)
        assert fake_yf["^VIX"] == 1

    def test_the_cached_call_returns_the_whole_list(self, fake_yf):
        """A cache that returns a truncated list would satisfy the counter
        assertion above while silently breaking every momentum calculation,
        which needs two closes to compute a delta."""
        first = mc._get_hourly_closes("^VIX", n=3)
        second = mc._get_hourly_closes("^VIX", n=3)
        assert second == first
        assert len(second) == 3

    def test_a_different_n_is_not_served_a_shorter_cached_list(self, fake_yf):
        """n is part of the request. `_dxy_momentum` asks for 3 and
        `get_context`'s `_latest` asks for 2 against the SAME symbol -- if the
        n=2 entry answered the n=3 caller, momentum would quietly change
        meaning depending on call order."""
        mc._get_hourly_closes("^VIX", n=2)
        wider = mc._get_hourly_closes("^VIX", n=3)
        assert len(wider) == 3

    def test_an_expired_entry_refetches(self, fake_yf, monkeypatch):
        """Negative control for the two tests above: if the cache never
        expired they would pass with a permanent cache, which is not what is
        wanted from a live market feed."""
        mc._get_hourly_closes("^VIX", n=3)
        real_time = mc.time.time

        monkeypatch.setattr(
            mc.time, "time", lambda: real_time() + mc._CACHE_TTL + 1
        )
        mc._get_hourly_closes("^VIX", n=3)
        assert fake_yf["^VIX"] == 2

    def test_get_context_hits_each_symbol_once_then_never_again(self, fake_yf):
        """The behaviour the Reversal Engine wiring depends on. Broken code
        counts 2 per symbol across two calls."""
        first = mc.get_context()
        mc.get_context()
        assert set(fake_yf.values()) == {1}
        assert set(first) == {
            "dxy_momentum", "us10y_level", "vix_level",
            "gvz_level", "tip_momentum",
        }


class TestFallbacksAreUnchanged:
    def test_no_yfinance_returns_the_neutrals(self, monkeypatch):
        """Must keep degrading to neutral -- yfinance is an optional import."""
        monkeypatch.setattr(mc, "_YF_AVAILABLE", False)
        monkeypatch.setattr(mc, "_CACHE", {})
        ctx = mc.get_context()
        assert ctx["us10y_level"] == mc._NEUTRAL["us10y_level"]
        assert ctx["vix_level"] == mc._NEUTRAL["vix_level"]
        assert ctx["dxy_momentum"] == 0.0

    def test_a_raising_fetch_returns_the_neutrals(self, monkeypatch):
        class _Boom:
            @staticmethod
            def Ticker(symbol):
                raise RuntimeError("network down")

        monkeypatch.setattr(mc, "yf", _Boom, raising=False)
        monkeypatch.setattr(mc, "_YF_AVAILABLE", True)
        monkeypatch.setattr(mc, "_CACHE", {})
        ctx = mc.get_context()
        assert ctx["gvz_level"] == mc._NEUTRAL["gvz_level"]
        assert ctx["tip_momentum"] == 0.0

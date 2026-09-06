"""Macro context features for the Reversal Engine vector.

Spec: docs/todo/001-reversal-macro-context.md.

The five values are normalised HERE rather than passed through raw as the
breakout engine does, because the Reversal model fits an SGDRegressor
alongside LightGBM and SGD is scale-sensitive -- a raw VIX of 20 next to a
dxy_momentum of 0.03 would dominate the gradient on scale alone.

Two things these tests exist to pin:

  * `MACRO_NEUTRAL` holds the NORMALISED values, because ml_engine merges it
    into `_FEATURE_NEUTRAL`, which right-pads stored 33-wide vectors. A neutral
    in raw units there would tell the model the ten-year was at 4.5% *after*
    normalisation, i.e. off the top of the scale, for all ~576 historical rows.
  * a legitimate 0.0 must survive. The breakout engine reads these with
    `signal_data.get(k) or ctx.get(k) or neutral`, which silently swaps a real
    zero for the default.
"""
from __future__ import annotations

import asyncio
import threading

import pytest

from backend.src.services.reversal_engine import re_macro


class TestTheFeatureContract:
    def test_names_are_the_five_macro_series_in_order(self):
        assert re_macro.MACRO_FEATURE_NAMES == [
            "dxy_momentum", "us10y_level", "vix_level",
            "gvz_level", "tip_momentum",
        ]

    def test_neutral_covers_exactly_those_names(self):
        """ml_engine merges MACRO_NEUTRAL into _FEATURE_NEUTRAL by name. A key
        that does not match a feature name pads with 0.0 and is invisible."""
        assert set(re_macro.MACRO_NEUTRAL) == set(re_macro.MACRO_FEATURE_NAMES)

    def test_the_neutrals_are_normalised_not_raw(self):
        """4.5% ten-year, VIX 20, GVZ 17 -- expressed on the [0,1] scale the
        live path produces, not in their own units."""
        assert re_macro.MACRO_NEUTRAL["us10y_level"] == pytest.approx(0.75)
        assert re_macro.MACRO_NEUTRAL["vix_level"] == pytest.approx(0.5)
        assert re_macro.MACRO_NEUTRAL["gvz_level"] == pytest.approx(0.425)
        assert re_macro.MACRO_NEUTRAL["dxy_momentum"] == 0.0
        assert re_macro.MACRO_NEUTRAL["tip_momentum"] == 0.0

    def test_an_absent_context_yields_exactly_the_neutrals(self):
        assert re_macro.macro_features({}, {}) == [
            re_macro.MACRO_NEUTRAL[n] for n in re_macro.MACRO_FEATURE_NAMES
        ]

    def test_a_none_context_is_not_an_error(self):
        assert len(re_macro.macro_features({}, None)) == 5


class TestNormalisation:
    def test_raw_context_is_scaled(self):
        ctx = {
            "dxy_momentum": -0.4, "us10y_level": 3.0, "vix_level": 30.0,
            "gvz_level": 20.0, "tip_momentum": 0.25,
        }
        assert re_macro.macro_features({}, ctx) == pytest.approx(
            [-0.4, 0.5, 0.75, 0.5, 0.25]
        )

    def test_out_of_range_values_are_clamped(self):
        ctx = {
            "dxy_momentum": 3.0, "us10y_level": -1.0, "vix_level": 90.0,
            "gvz_level": 200.0, "tip_momentum": -7.0,
        }
        assert re_macro.macro_features({}, ctx) == pytest.approx(
            [1.0, 0.0, 1.0, 1.0, -1.0]
        )

    def test_a_non_numeric_value_falls_back_to_its_neutral(self):
        ctx = {"vix_level": "not a number"}
        out = re_macro.macro_features({}, ctx)
        i = re_macro.MACRO_FEATURE_NAMES.index("vix_level")
        assert out[i] == re_macro.MACRO_NEUTRAL["vix_level"]


class TestReadPrecedence:
    def test_signal_data_wins_over_live_context(self):
        """A stored signal re-scored later must use the macro conditions it was
        created under, not today's."""
        out = re_macro.macro_features(
            {"vix_level": 40.0}, {"vix_level": 10.0}
        )
        assert out[re_macro.MACRO_FEATURE_NAMES.index("vix_level")] == pytest.approx(1.0)

    def test_a_real_zero_is_not_mistaken_for_absence(self):
        """The `or` idiom's trap. A ten-year of 0.0 normalises to 0.0, which is
        a long way from the 0.75 neutral."""
        out = re_macro.macro_features({"us10y_level": 0.0}, {"us10y_level": 4.5})
        assert out[re_macro.MACRO_FEATURE_NAMES.index("us10y_level")] == 0.0


class TestCycleContext:
    @pytest.fixture(autouse=True)
    def _clear(self, monkeypatch):
        monkeypatch.setattr(re_macro, "_ctx_cache", {})
        monkeypatch.setattr(re_macro, "_ctx_ts", 0.0)

    def test_it_is_fetched_once_per_refresh_window(self, monkeypatch):
        """The Reversal cycle is 60s. Without this, an expired
        market_context TTL means a network fetch every minute."""
        calls = []
        monkeypatch.setattr(
            re_macro.market_context, "get_context",
            lambda: (calls.append(1), {"vix_level": 21.0})[1],
        )

        async def go():
            await re_macro.get_cycle_context()
            await re_macro.get_cycle_context()

        asyncio.run(go())
        assert len(calls) == 1

    def test_it_refetches_once_the_window_expires(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            re_macro.market_context, "get_context",
            lambda: (calls.append(1), {"vix_level": 21.0})[1],
        )
        asyncio.run(re_macro.get_cycle_context())
        monkeypatch.setattr(re_macro, "_ctx_ts", re_macro.time.time() - re_macro._REFRESH_S - 1)
        asyncio.run(re_macro.get_cycle_context())
        assert len(calls) == 2

    def test_the_fetch_does_not_run_on_the_event_loop_thread(self, monkeypatch):
        """get_context() is blocking HTTP. The Reversal cycle shares its event
        loop with position management, so a stalled loop is not cosmetic."""
        seen = {}
        monkeypatch.setattr(
            re_macro.market_context, "get_context",
            lambda: (seen.update(thread=threading.current_thread().name), {})[1],
        )

        async def go():
            seen["loop"] = threading.current_thread().name
            await re_macro.get_cycle_context()

        asyncio.run(go())
        assert seen["thread"] != seen["loop"]

    def test_a_failing_fetch_yields_an_empty_context(self, monkeypatch):
        def boom():
            raise RuntimeError("yfinance down")

        monkeypatch.setattr(re_macro.market_context, "get_context", boom)
        assert asyncio.run(re_macro.get_cycle_context()) == {}

    def test_a_failing_fetch_does_not_discard_the_last_good_context(self, monkeypatch):
        """Degrading to neutrals for one cycle is acceptable; throwing away a
        context that is still inside its window is not."""
        monkeypatch.setattr(
            re_macro.market_context, "get_context", lambda: {"vix_level": 21.0}
        )
        good = asyncio.run(re_macro.get_cycle_context())
        assert good == {"vix_level": 21.0}

        def boom():
            raise RuntimeError("yfinance down")

        monkeypatch.setattr(re_macro.market_context, "get_context", boom)
        monkeypatch.setattr(re_macro, "_ctx_ts", re_macro.time.time() - re_macro._REFRESH_S - 1)
        assert asyncio.run(re_macro.get_cycle_context()) == {"vix_level": 21.0}

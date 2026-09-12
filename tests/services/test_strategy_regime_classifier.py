"""The rule-based regime classifier, which decides when Claude cannot.

`channels/strategy_ai.classify_regime` does two jobs: it is the fallback that
picks a strategy family when the AI call is unavailable, and it is the
pre-filter context handed to the AI prompt when it is. Either way it decides
which strategy a channel's next signals are managed with. The module sat at
8.3% coverage and nothing tested this function at all.

It is a pure decision over three inputs — ATR, ADX and the UTC hour — and the
hour is the one worth pinning, because this file holds one of the app's **four**
disagreeing definitions of a trading session (`docs/todo/bugs/057`). The tests
below state its boundaries as they are, so that reconciling them later is a
visible change rather than a silent one.

Nothing here changes behaviour.
"""
from __future__ import annotations

import pytest

from backend.src.services.channels import strategy_ai


@pytest.fixture
def at_hour(monkeypatch):
    def _set(hour: int):
        monkeypatch.setattr(strategy_ai, "_utc_hour", lambda: hour)
    return _set


class TestVolatilityOverridesEverything:
    def test_extreme_atr_is_conservative_whatever_the_hour_or_trend(self, at_hour):
        at_hour(13)  # the overlap, the one window that can return no_sl_scale

        assert strategy_ai.classify_regime(atr_h1=50.0, adx_h1=40.0) == "conservative"

    def test_the_danger_threshold_itself_does_not_trip_it(self, at_hour):
        """`>`, not `>=`. A boundary that moves by one tick when someone
        rewrites the comparison is a silent change to how trades are managed.

        At exactly 45 the danger branch is skipped, and the overlap's own
        volatility band (15-35) then rejects it too — so the answer is
        `protected_scale`, not the strongest family. Both facts matter: the
        first is the boundary, the second is that nothing above ATR 35 can ever
        reach `no_sl_scale`."""
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=45.0, adx_h1=40.0) == "protected_scale"
        assert strategy_ai.classify_regime(atr_h1=45.01, adx_h1=40.0) == "conservative"

    def test_the_top_of_the_volatility_band_still_reaches_the_strongest_family(self, at_hour):
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=35.0, adx_h1=40.0) == "no_sl_scale"
        assert strategy_ai.classify_regime(atr_h1=35.01, adx_h1=40.0) == "protected_scale"


class TestTheOverlap:
    def test_a_strong_trend_in_normal_volatility_runs_without_a_stop_scale(self, at_hour):
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=30.0) == "no_sl_scale"

    def test_a_strong_trend_in_thin_volatility_is_not_trusted(self, at_hour):
        """ATR below the band fails the `_ATR_LOW <= atr` test, so a 30 ADX at
        ATR 10 falls through to the weaker branch rather than the strongest."""
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=10.0, adx_h1=30.0) == "protected_scale"

    def test_a_middling_trend_gets_the_protected_variant(self, at_hour):
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=22.0) == "protected_scale"

    def test_no_trend_at_all_is_conservative(self, at_hour):
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=15.0) == "conservative"


class TestLondonBeforeTheOverlap:
    def test_a_strong_trend_gets_protected_scale_not_no_sl_scale(self, at_hour):
        """London alone never reaches the strongest family — only the overlap
        does. Same ADX and ATR, two hours apart, two different answers."""
        at_hour(9)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=30.0) == "protected_scale"

    def test_the_same_inputs_at_the_overlap_go_further(self, at_hour):
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=30.0) == "no_sl_scale"

    def test_a_weak_trend_in_london_is_conservative(self, at_hour):
        at_hour(9)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=20.0) == "conservative"


class TestTheHoursThisFileCallsAsian:
    """`_ASIAN_END_UTC = 7`, so 00:00-06:59 — one of four definitions in this
    app and the narrowest (`docs/todo/bugs/057`).

    **The branch is decorative**, and mutation testing is how that showed:
    deleting it changes no answer, because 00:00-06:59 matches neither the
    London branch nor the overlap branch and reaches the same `conservative`
    default anyway. The tests below are still worth having — they pin the
    ANSWER for those hours, which is what callers depend on — but a mutant that
    removes the line survives, and that is an equivalent mutant rather than a
    hole.
    """

    def test_before_seven_is_conservative_however_good_the_trend_looks(self, at_hour):
        at_hour(6)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=40.0) == "conservative"

    def test_seven_is_already_london_here(self, at_hour):
        """Both signal engines still call 07:00 Asian. This file does not."""
        at_hour(7)

        assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=30.0) == "protected_scale"


class TestTheHoursWithNoBranchAtAll:
    def test_everything_from_sixteen_hundred_is_conservative(self, at_hour):
        """New York's whole session falls past the last `if` and lands on the
        default. Possibly intended — this is a regime classifier, not a session
        map — but it is a rule nobody wrote down, living in the shape of an
        if-chain. bugs/057."""
        for hour in (16, 18, 20, 22, 23):
            at_hour(hour)

            assert strategy_ai.classify_regime(atr_h1=25.0, adx_h1=40.0) == "conservative", hour


class TestMissingInputs:
    def test_a_missing_atr_is_treated_as_a_normal_market_not_a_dangerous_one(self, at_hour):
        """The ATR default is 20 — inside the band — so an unknown volatility
        does not veto the strongest family on its own. Raise that default above
        the danger line and every ATR-less call becomes conservative; this is
        the test that notices."""
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=None, adx_h1=30.0) == "no_sl_scale"

    def test_no_atr_or_adx_falls_back_to_neutral_defaults(self, at_hour):
        """ATR 20 and ADX 18 — inside the band, below the trend threshold — so
        an unknown market is treated as a weak one rather than a strong one."""
        at_hour(13)

        assert strategy_ai.classify_regime(atr_h1=None, adx_h1=None) == "conservative"

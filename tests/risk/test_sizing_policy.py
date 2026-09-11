"""Sizing as a policy, not a constant.

Section 5.4 of docs/todo/reversal-engine/200. The app sizes by risk-per-
trade percent (`fees_sizing.suggest_lot_size`), which is the right base and
the whole of it. Three things a desk does on top are missing:

  * **volatility targeting** -- the same percent risk on a violent day is
    more risk, not the same risk
  * **drawdown scaling** -- size down while losing, which is the only free
    reduction in ruin probability there is
  * **a correlated exposure cap** -- six open XAUUSD signals in the same
    direction is ONE position of six times the size, and `_MAX_OPEN_SIGNALS
    = 6` does not know that

This module MODIFIES a base lot size; it does not compute one. Duplicating
`suggest_lot_size` would put two sizing rules in the codebase and the wrong
one would eventually win.

Every scalar is 1.0 when its input is missing, so the composed default is
exactly today's lot size.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import sizing_policy as sp


class TestVolatilityScalar:
    def test_a_violent_day_sizes_down(self):
        assert sp.volatility_scalar(atr=16.0, reference_atr=8.0) == pytest.approx(0.5)

    def test_a_quiet_day_sizes_up_but_not_without_limit(self):
        """Uncapped, a very quiet hour would ask for many times normal size,
        and the quiet is usually the hour before it stops being quiet."""
        assert sp.volatility_scalar(atr=1.0, reference_atr=8.0) == pytest.approx(
            sp.MAX_VOL_SCALAR)

    def test_a_missing_or_zero_atr_changes_nothing(self):
        assert sp.volatility_scalar(atr=0.0, reference_atr=8.0) == 1.0
        assert sp.volatility_scalar(atr=8.0, reference_atr=0.0) == 1.0


class TestDrawdownScalar:
    def test_no_drawdown_changes_nothing(self):
        assert sp.drawdown_scalar(0.0) == 1.0

    def test_a_shallow_drawdown_inside_the_grace_band_changes_nothing(self):
        assert sp.drawdown_scalar(0.02, start_pct=0.05) == 1.0

    def test_size_tapers_as_the_drawdown_deepens(self):
        mid = sp.drawdown_scalar(0.10, start_pct=0.05, full_pct=0.20)
        deep = sp.drawdown_scalar(0.18, start_pct=0.05, full_pct=0.20)
        assert 1.0 > mid > deep > 0.0

    def test_it_never_reaches_zero_by_accident(self):
        """A scalar of exactly zero silently stops trading with no reason
        anywhere. Stopping is a circuit-breaker decision, not a rounding
        outcome of a sizing curve."""
        assert sp.drawdown_scalar(0.95, start_pct=0.05, full_pct=0.20) >= sp.MIN_DD_SCALAR


class TestKelly:
    def test_a_positive_edge_gives_a_positive_fraction(self):
        assert sp.kelly_fraction(prob=0.6, payoff_r=2.0, fraction=1.0) > 0

    def test_no_edge_gives_nothing(self):
        assert sp.kelly_fraction(prob=0.33, payoff_r=2.0, fraction=1.0) == 0.0

    def test_the_fraction_is_applied(self):
        full = sp.kelly_fraction(prob=0.6, payoff_r=2.0, fraction=1.0)
        quarter = sp.kelly_fraction(prob=0.6, payoff_r=2.0, fraction=0.25)
        assert quarter == pytest.approx(full * 0.25)

    def test_it_is_capped_well_below_full_kelly(self):
        """Full Kelly on an estimated probability is a rapid route to ruin,
        because the estimate is wrong and Kelly is convex in the error."""
        assert sp.kelly_fraction(prob=0.99, payoff_r=10.0, fraction=1.0) <= sp.MAX_KELLY


class TestCorrelatedExposure:
    def test_room_is_what_the_cap_leaves(self):
        assert sp.correlated_room(open_lots=0.04, cap_lots=0.10) == pytest.approx(0.06)

    def test_a_full_book_leaves_no_room(self):
        assert sp.correlated_room(open_lots=0.12, cap_lots=0.10) == 0.0

    def test_a_zero_cap_means_no_cap_not_no_trading(self):
        """Zero is OFF everywhere else in this codebase's settings, and a
        cap that silently meant "never trade" would be found the hard way."""
        assert sp.correlated_room(open_lots=5.0, cap_lots=0.0) is None


class TestComposition:
    def test_with_nothing_configured_the_base_size_is_returned_unchanged(self):
        out = sp.apply(0.05, sp.SizingInputs())
        assert out.lots == pytest.approx(0.05)
        assert out.notes == []

    def test_scalars_multiply(self):
        out = sp.apply(0.10, sp.SizingInputs(atr=16.0, reference_atr=8.0,
                                             drawdown_pct=0.10,
                                             dd_start_pct=0.05,
                                             dd_full_pct=0.20))
        assert out.lots < 0.05
        assert len(out.notes) == 2

    def test_the_correlated_cap_is_a_ceiling_not_a_scalar(self):
        out = sp.apply(0.10, sp.SizingInputs(open_correlated_lots=0.07,
                                             correlated_cap_lots=0.10))
        assert out.lots == pytest.approx(0.03)

    def test_the_result_never_goes_below_the_broker_minimum(self):
        out = sp.apply(0.01, sp.SizingInputs(atr=100.0, reference_atr=8.0))
        assert out.lots == pytest.approx(sp.MIN_LOT)

    def test_a_full_correlated_book_returns_zero_and_says_so(self):
        """Zero lots here means "do not open this", which is a real answer
        and must be distinguishable from a sizing floor."""
        out = sp.apply(0.10, sp.SizingInputs(open_correlated_lots=0.10,
                                             correlated_cap_lots=0.10))
        assert out.lots == 0.0
        assert any("correlated" in n for n in out.notes)

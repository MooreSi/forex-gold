"""`sizing_policy.apply` must not hand back more lots than the account allows.

**Nothing in this file reaches a broker.** Pure functions and dicts.

## The hole

`fees_sizing.suggest_lot_size` clamps every lot it computes to
`Global Parameters > Max lot size` (`vantage_risk_settings.max_lot_size`,
0.10 on this account). It is the last ceiling before an order, and it is
applied inside that one function precisely so every one of its twenty-odd
call sites gets it without asking.

`sizing_policy.apply` was written to wrap that result and had no ceiling of
its own. Its volatility scalar has an UPSIDE: `MAX_VOL_SCALAR` is 1.5, so a
quiet market multiplies the base lot up. On this account's real numbers --
98% of trades sized at exactly the 0.10 cap, and an H1 ATR distribution
whose quietest decile sits at 3.13 against a 7.74 median -- that is
0.10 * 1.5 = 0.15 lots, half again over a cap the user set.

Nothing caught it because `apply` has no callers yet. It is being fixed
while it is still inert, which is the only cheap time to fix something on
the sizing path.

## Why the cap is an input rather than a read

`sizing_policy` is a pure module and must stay one; `capability_gates` is
already the layer that turns a settings row into these inputs, and already
reads `correlated_exposure_cap_lots` there. A cap of 0.0 means OFF, matching
`correlated_cap_lots` and every other cap in this codebase.
"""
import pytest

from backend.src.services.risk import capability_gates as caps
from backend.src.services.risk import sizing_policy as sp


class TestTheCeilingIsApplied:

    def test_a_quiet_market_cannot_size_over_the_cap(self):
        """The exact case that would have shipped: base at the 0.10 cap,
        ATR in the quietest decile, so the scalar clamps to 1.5."""
        got = sp.apply(0.10, sp.SizingInputs(atr=3.13, reference_atr=7.74,
                                             max_lots=0.10))
        assert got.lots == 0.10

    def test_the_breach_is_real_without_the_cap(self):
        """Negative control. Without this fix the same call returns 0.15,
        so the assertion above is not passing for an unrelated reason."""
        got = sp.apply(0.10, sp.SizingInputs(atr=3.13, reference_atr=7.74))
        assert got.lots == 0.15

    def test_the_cap_says_so_in_the_notes(self):
        """A lot that was reduced without a reason is a lot nobody can
        explain afterwards -- every other reduction in `apply` leaves one."""
        got = sp.apply(0.10, sp.SizingInputs(atr=3.13, reference_atr=7.74,
                                             max_lots=0.10))
        assert any("max lot" in n.lower() for n in got.notes)

    @pytest.mark.parametrize("base,atr,ref,cap,expected", [
        (0.10, 7.74, 7.74, 0.10, 0.10),   # at the cap, scalar 1.0, untouched
        (0.10, 30.16, 7.74, 0.10, 0.03),  # violent: scaled down, cap irrelevant
        (0.04, 3.13, 7.74, 0.10, 0.06),   # quiet but under the cap: scales up
        (0.20, 7.74, 7.74, 0.10, 0.10),   # a base already over the cap is cut
    ])
    def test_the_ceiling_only_binds_when_it_should(self, base, atr, ref, cap,
                                                   expected):
        got = sp.apply(base, sp.SizingInputs(atr=atr, reference_atr=ref,
                                             max_lots=cap))
        assert got.lots == expected


class TestOffByDefault:

    def test_zero_means_off(self):
        """rules/60 and every other cap here: 0.0 is OFF, never 'never
        trade again'. Same trap `correlated_room` documents."""
        got = sp.apply(0.10, sp.SizingInputs(atr=3.13, reference_atr=7.74,
                                             max_lots=0.0))
        assert got.lots == 0.15

    def test_a_default_row_still_returns_the_base_lot_untouched(self):
        """The load-bearing property of this whole module, restated because
        adding a field to SizingInputs is exactly how it would break."""
        assert sp.apply(0.07, sp.SizingInputs()).lots == 0.07

    def test_the_cap_never_forces_a_lot_below_the_minimum(self):
        got = sp.apply(0.10, sp.SizingInputs(max_lots=0.001))
        assert got.lots == sp.MIN_LOT


class TestTheGateSuppliesIt:

    def test_the_accounts_max_lot_size_reaches_the_policy(self):
        """A ceiling nothing passes in is a ceiling that is off."""
        got = caps.sizing_inputs({"vol_target_sizing_enabled": 1,
                                  "max_lot_size": 0.10},
                                 atr=3.13, reference_atr=7.74,
                                 drawdown_pct=0.0, open_correlated_lots=0.0)
        assert got.max_lots == 0.10

    def test_a_row_without_the_column_leaves_the_ceiling_off(self):
        """`max_lot_size` predates all of this, but a settings row read
        through a stale facade may not carry it. Off, not 0.01."""
        got = caps.sizing_inputs({"vol_target_sizing_enabled": 1},
                                 atr=3.13, reference_atr=7.74,
                                 drawdown_pct=0.0, open_correlated_lots=0.0)
        assert got.max_lots == 0.0

    def test_the_ceiling_applies_even_with_the_scaling_switch_off(self):
        """The cap is the account's, not the capability's. With scaling off
        the scalars are 1.0 and it changes nothing -- but a caller that
        passes an over-cap base lot must still be cut."""
        got = caps.sizing_inputs({"max_lot_size": 0.10},
                                 atr=3.13, reference_atr=7.74,
                                 drawdown_pct=0.0, open_correlated_lots=0.0)
        assert got.max_lots == 0.10
        assert sp.apply(0.20, got).lots == 0.10

"""An ATR-sized ladder, not just an ATR-sized TP1.

Section 2 of docs/todo/reversal-engine/200, corrected after reading the code
rather than the field names.

`use_dynamic_atr` IS implemented, in Python, not in the EA: `template_sl_at`
sizes the stop at ATR x atr_sl_mult and `resolve_template_tps` overrides TP
LEVEL 1 at ATR x atr_tp1_mult. Its documented scope stops there -- "Dynamic
ATR sizing of SL/TP1", per the field's own comment -- so every level above
TP1 stays a fixed pip distance.

That is a problem for the fix the engine actually needs. If the stop and TP1
scale with volatility but TP2 does not, R is constant at the first target and
drifts everywhere above it, which is the same inversion
docs/todo/reversal-engine/200 section 1.1 set out to remove.

`atr_ladder_scale` keeps the ladder's SHAPE (the relative spacing somebody
tuned) and rescales the whole thing so TP1 lands where ATR says. Off by
default, so no existing template changes.
"""
from __future__ import annotations

import pytest

from backend.src.services.trading import template_levels as tl

# 1.0 in price = 10 EA pips on XAUUSD.
ATR = 4.0


def tpl(**kw):
    base = {"use_dynamic_atr": True, "atr_tp1_mult": 1.5,
            "atr_ladder_scale": True, "tp1_pips": 100.0, "tp2_pips": 200.0,
            "tp3_pips": 400.0}
    base.update(kw)
    return base


def ladder(template, ref=3300.0, sign=1, atr=ATR):
    """The pips ladder as `resolve_template_tps` builds it, before scaling."""
    tps = {n: ref + sign * (float(template.get(f"tp{n}_pips", 0.0)) * 0.10)
           for n in range(1, 9) if float(template.get(f"tp{n}_pips", 0.0) or 0) > 0}
    return tl.atr_scaled_ladder(template, atr, ref, sign, tps)


class TestScaling:
    def test_tp1_lands_exactly_where_the_atr_multiple_says(self):
        out = ladder(tpl())
        assert out[1] == pytest.approx(3300.0 + ATR * 1.5)

    def test_the_ladders_relative_spacing_is_preserved(self):
        """TP2 was twice TP1 and TP3 four times. After scaling they still
        are: the human-tuned shape survives, only its size changes."""
        out = ladder(tpl())
        d1 = out[1] - 3300.0
        assert (out[2] - 3300.0) == pytest.approx(2 * d1)
        assert (out[3] - 3300.0) == pytest.approx(4 * d1)

    def test_a_more_volatile_market_pushes_every_level_further_out(self):
        quiet = ladder(tpl(), atr=2.0)
        wild = ladder(tpl(), atr=8.0)
        for n in (1, 2, 3):
            assert (wild[n] - 3300.0) > (quiet[n] - 3300.0)

    def test_a_sell_scales_downward(self):
        out = ladder(tpl(), sign=-1)
        assert out[1] == pytest.approx(3300.0 - ATR * 1.5)
        assert out[3] < out[1]


class TestItChangesNothingUnlessAsked:
    def test_off_by_default_the_ladder_is_returned_untouched(self):
        t = tpl(atr_ladder_scale=False)
        out = ladder(t)
        # TP1 still gets the level-1 override that already existed; every
        # other level keeps its fixed pip distance.
        assert out[2] == pytest.approx(3300.0 + 200.0 * 0.10)
        assert out[3] == pytest.approx(3300.0 + 400.0 * 0.10)

    def test_the_existing_level_one_override_still_applies_when_off(self):
        out = ladder(tpl(atr_ladder_scale=False))
        assert out[1] == pytest.approx(3300.0 + ATR * 1.5)

    def test_without_dynamic_atr_nothing_is_touched_at_all(self):
        out = ladder(tpl(use_dynamic_atr=False))
        assert out[1] == pytest.approx(3300.0 + 100.0 * 0.10)
        assert out[2] == pytest.approx(3300.0 + 200.0 * 0.10)


class TestItRefusesRatherThanGuesses:
    def test_a_ladder_with_no_tp1_cannot_be_scaled_and_is_left_alone(self):
        """The scale factor is defined relative to TP1. Without one there is
        no shape to preserve, and inventing a reference would silently move
        every level on a template whose author never set a first target."""
        t = tpl(tp1_pips=0.0)
        out = ladder(t)
        assert out[2] == pytest.approx(3300.0 + 200.0 * 0.10)

    def test_a_zero_atr_leaves_the_ladder_alone(self):
        out = ladder(tpl(), atr=0.0)
        assert out[1] == pytest.approx(3300.0 + 100.0 * 0.10)

    def test_an_empty_ladder_stays_empty(self):
        assert tl.atr_scaled_ladder(tpl(), ATR, 3300.0, 1, {}) == {}

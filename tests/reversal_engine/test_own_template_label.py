"""A signal is replayed through the template it traded under, or refused.

docs/todo/reversal-engine/250, step 2. `tpl_label` walks every signal through
one hard-coded approximation of "30 TP1 SL50 and Trail"; on the Mac 551 of
963 executed signals traded under something else. This labeller hands the
signal's own template to `backtest/template_simulator` (the walk read out of
the EA's ManageTemplate) and returns a reason, never a number, for what that
walk cannot model.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import own_template_label as otl

T0 = 1_790_000_040.0          # trigger, inside the minute bar that opens at T0-40
BAR0 = T0 - 40.0

# 50 pips = a 5.0 stop; one target 40 pips (4.0) closing everything.
SIMPLE = {"name": "Simple", "mode": "single", "sl_pips": 50, "tp1_pips": 40,
          "tp1_pct": 100, "lot_anchor": 1.0, "trail_mode": "off",
          "tpsl_mode": "on", "partials": 1}


def _bar(i, lo, hi):
    return {"ts": BAR0 + 60 * i, "open": lo, "high": hi, "low": lo, "close": hi}


def test_the_template_name_is_read_from_the_strategy():
    assert otl.template_name("template:30 TP1 SL50 and Trail") == "30 TP1 SL50 and Trail"
    assert otl.template_name("conservative_trial") is None
    assert otl.template_name(None) is None


def test_a_target_hit_is_its_distance_over_the_stop_less_cost():
    bars = [_bar(0, 4000.0, 4000.5), _bar(1, 4000.0, 4004.5)]

    lab = otl.label(SIMPLE, bars, "BUY", 4000.0, T0, cost_pts=0.5)

    assert lab.refusal == ""
    assert lab.r == pytest.approx(4.0 / 5.0 - 0.5 / 5.0)


def test_the_stop_is_minus_one_less_cost():
    bars = [_bar(0, 4000.0, 4000.5), _bar(1, 3994.0, 4001.0)]

    lab = otl.label(SIMPLE, bars, "BUY", 4000.0, T0, cost_pts=0.5)

    assert lab.r == pytest.approx(-1.0 - 0.1)


def test_a_sell_is_mirrored():
    bars = [_bar(0, 3999.5, 4000.0), _bar(1, 3995.5, 4000.0)]

    lab = otl.label(SIMPLE, bars, "SELL", 4000.0, T0, cost_pts=0.0)

    assert lab.r == pytest.approx(0.8)


def test_the_entry_bars_favourable_side_is_not_credited():
    """The trigger fell inside bar 0; its high may have come before the fill.
    Walking it would bank a target the trade may never have seen."""
    bars = [_bar(0, 4000.0, 4010.0), _bar(1, 3999.0, 4001.0)]

    lab = otl.label(SIMPLE, bars, "BUY", 4000.0, T0, cost_pts=0.0)

    assert lab.r == pytest.approx(0.2)        # timed out at bar 1's close, +1.0


def test_the_entry_bars_adverse_side_is_walked():
    bars = [_bar(0, 3990.0, 4000.0), _bar(1, 4000.0, 4010.0)]

    lab = otl.label(SIMPLE, bars, "BUY", 4000.0, T0, cost_pts=0.0)

    assert lab.r == pytest.approx(-1.0)


def test_nothing_past_the_horizon_is_walked():
    far = otl.HORIZON_S // 60 + 5
    bars = [_bar(0, 4000.0, 4000.5), _bar(1, 4000.0, 4001.0),
            _bar(far, 4000.0, 4010.0)]

    lab = otl.label(SIMPLE, bars, "BUY", 4000.0, T0, cost_pts=0.0)

    assert lab.r == pytest.approx(0.2)


def test_a_grid_template_is_refused_with_the_simulators_reason():
    grid = dict(SIMPLE, mode="grid", name="Auto Limit Scalp")

    lab = otl.label(grid, [_bar(0, 4000.0, 4001.0), _bar(1, 4000.0, 4001.0)],
                    "BUY", 4000.0, T0, cost_pts=0.0)

    assert lab.r is None
    assert "grid" in lab.refusal


def test_no_template_is_refused():
    lab = otl.label(None, [_bar(0, 4000.0, 4001.0)], "BUY", 4000.0, T0, 0.0)

    assert lab.r is None
    assert lab.refusal


def test_bars_that_miss_the_trigger_are_refused():
    """A label from bars that start after the fill would be a different trade."""
    bars = [_bar(3, 4000.0, 4001.0), _bar(4, 4000.0, 4001.0)]

    lab = otl.label(SIMPLE, bars, "BUY", 4000.0, T0, 0.0)

    assert lab.r is None
    assert "bars" in lab.refusal


def test_a_template_without_a_stop_is_refused():
    """R has no denominator without one."""
    lab = otl.label(dict(SIMPLE, sl_pips=0), [_bar(0, 4000.0, 4001.0),
                    _bar(1, 4000.0, 4001.0)], "BUY", 4000.0, T0, 0.0)

    assert lab.r is None
    assert "stop" in lab.refusal

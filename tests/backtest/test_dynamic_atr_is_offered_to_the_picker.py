"""A template the bar walk CAN simulate must be offered by the picker.

`can_simulate` already knows this: its `atr_available` flag exists precisely
because "the bar walk can supply the ATR -- `engine._atr14` computes exactly
that number a few lines from the call site -- so `use_dynamic_atr` is only a
refusal when nobody can."

`summarise` -- the one thing the Backtest tab reads to build its picker --
called it with the default `atr_available=False`, so every volatility-sized
template was listed as un-backtestable while `_simulate_template` was walking
them perfectly well. The result is the inverse of the failure the rest of this
module guards against: not a refused template reported as zeros, but a
simulatable template reported as refused, which hides the only kind of
template that adapts to market conditions.

The candle walk is the picker's default granularity and always has an ATR.
A tick run still refuses a dynamic-ATR template, loudly and with its reason,
inside `run_backtest` -- that path is unchanged and is not what this pins.
"""
from __future__ import annotations

from backend.src.services.backtest import template_support as ts


def _atr_template(**over) -> dict:
    base = {
        "name": "Adaptive Volatility v1", "mode": "single", "pendings": 0,
        "lot_anchor": 0.01, "risk_pct": 0.0, "tpsl_mode": "on",
        "partials": 1, "close_full_on_last": 1,
        "be_mode": "entry_buffer", "be_trigger": 1,
        "trail_mode": "step", "trail_distance": 40.0, "trail_step": 15.0,
        "tp1_pips": 150.0, "tp1_pct": 100.0, "sl_pips": 70.0,
        "use_dynamic_atr": True, "atr_ladder_scale": True,
        "atr_period": 14, "atr_sl_mult": 1.6, "atr_tp1_mult": 4.8,
        "harvest_enabled": False,
    }
    base.update(over)
    return base


def test_a_dynamic_atr_template_is_listed_as_backtestable():
    # Arrange: the exact shape the bar walk supplies an ATR for.
    rows = ts.summarise([_atr_template()])

    # Assert
    assert len(rows) == 1
    assert rows[0]["supported"] is True
    assert rows[0]["reasons"] == []


def test_a_grid_template_is_still_refused_and_says_why():
    # The flag must not become a blanket "everything is supported". Grid is
    # refused for a reason that has nothing to do with the ATR.
    rows = ts.summarise([_atr_template(mode="grid")])

    assert rows[0]["supported"] is False
    assert any("grid" in r.lower() for r in rows[0]["reasons"])


def test_a_pending_leg_template_is_still_refused():
    rows = ts.summarise([_atr_template(pendings=1)])

    assert rows[0]["supported"] is False
    assert any("resting" in r.lower() for r in rows[0]["reasons"])


def test_harvest_template_is_still_refused():
    # harvest closes on aggregate profit, which has no meaning for one signal.
    rows = ts.summarise([_atr_template(harvest_enabled=True)])

    assert rows[0]["supported"] is False

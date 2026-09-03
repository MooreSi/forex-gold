"""Which EA templates the backtest will simulate, and which it refuses.

Owner, 2026-09-03: option A of docs/todo/backtest/010 -- simulate the template
fields that map cleanly onto a price walk, and REFUSE anything else, naming
the field, rather than approximating it.

The reason for refusing rather than approximating: the backtest's numbers are
used to choose the templates that trade real money, and nothing compares a
simulated template against what the EA actually does. A silent approximation
produces a figure that looks authoritative and is wrong. "Not supported, and
here is why" is worth more than a number you would trust wrongly.

The owner said "no grid mode", and the three CURRENT channel bindings are
indeed all single. But 11 of the 22 stored templates are grid, and three of
them traded within the last 30 days (Grid - Zone Mode, Auto Limit Balanced,
Auto Limit Scalp), so the exclusion list is real rather than theoretical --
which is exactly why it has to name what it excluded.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import template_support as ts


def _tpl(**over) -> dict:
    """A minimal single-mode template: the supported baseline."""
    base = {
        "name": "T", "mode": "single", "lot_anchor": 0.10, "risk_pct": 0.0,
        "sl_pips": 50.0, "tp1_pips": 30.0, "tp1_pct": 100.0,
        "be_mode": "off", "trail_mode": "off",
    }
    base.update(over)
    return base


class TestASupportedTemplate:
    def test_a_plain_single_template_is_supported(self):
        ok, reasons = ts.can_simulate(_tpl())

        assert ok is True
        assert reasons == []

    def test_a_tp_ladder_is_supported(self):
        ok, _ = ts.can_simulate(_tpl(tp1_pips=30, tp1_pct=50,
                                     tp2_pips=60, tp2_pct=50))

        assert ok is True

    def test_breakeven_is_supported(self):
        ok, _ = ts.can_simulate(_tpl(be_mode="entry", be_trigger=20.0))

        assert ok is True

    @pytest.mark.parametrize("mode", ["points", "candle", "tp"])
    def test_the_trail_modes_are_supported(self, mode):
        ok, _ = ts.can_simulate(_tpl(trail_mode=mode, trail_distance=15.0))

        assert ok is True


class TestGridIsRefused:
    def test_grid_mode_is_not_simulated(self):
        ok, reasons = ts.can_simulate(_tpl(mode="grid"))

        assert ok is False

    def test_the_reason_names_the_field(self):
        """"Unsupported" with no field is an answer nobody can act on."""
        _, reasons = ts.can_simulate(_tpl(mode="grid"))

        assert any("mode" in r for r in reasons)

    def test_the_reason_is_readable(self):
        """It is shown in the UI beside the template."""
        _, reasons = ts.can_simulate(_tpl(mode="grid"))

        assert reasons and all(len(r) > 20 for r in reasons)


class TestTheOtherExclusions:
    @pytest.mark.parametrize("field,value", [
        ("use_dynamic_atr", 1),      # SL/TP derived from live ATR
        ("pendings", 1),             # a resting entry, not a market fill
        ("equity_protect", 1),       # acts on account equity, not this trade
        ("harvest_enabled", 1),      # closes on account-wide profit
    ])
    def test_a_field_the_walk_cannot_model_is_refused(self, field, value):
        ok, reasons = ts.can_simulate(_tpl(**{field: value}))

        assert ok is False
        assert any(field in r for r in reasons)

    def test_every_unsupported_field_is_listed_not_just_the_first(self):
        """A template with three problems should say all three, or fixing one
        at a time is a guessing game."""
        ok, reasons = ts.can_simulate(
            _tpl(mode="grid", use_dynamic_atr=1, equity_protect=1))

        assert ok is False
        assert len(reasons) >= 3


class TestItDoesNotOverReach:
    def test_an_off_switch_is_not_an_exclusion(self):
        """The fields above only exclude when they are ON. A template that
        carries them switched off is perfectly simulable, and treating the
        mere presence of a key as unsupported would refuse everything."""
        ok, reasons = ts.can_simulate(
            _tpl(use_dynamic_atr=0, equity_protect=0, harvest_enabled=0,
                 pendings=0))

        assert ok is True, reasons

    def test_pending_mode_alone_does_not_exclude(self):
        """It is "zone" or "step" on EVERY template, describing how legs
        WOULD be placed. Keying on it refused all 22 of the owner's
        templates, including the three his channels are bound to. `pendings`
        -- the count -- is what actually says there are resting orders."""
        ok, reasons = ts.can_simulate(_tpl(pending_mode="zone", pendings=0))

        assert ok is True, reasons

    def test_an_unknown_field_does_not_refuse(self):
        """A template row gains columns over time. A new field defaulting to
        off must not silently disable backtesting for every template."""
        ok, _ = ts.can_simulate(_tpl(some_future_field=1))

        assert ok is True

    def test_a_missing_template_is_refused_not_crashed(self):
        ok, reasons = ts.can_simulate(None)

        assert ok is False
        assert reasons


class TestTheSummaryForTheUI:
    def test_supported_and_unsupported_are_split(self):
        rows = ts.summarise([
            _tpl(name="Single A"),
            _tpl(name="Grid B", mode="grid"),
        ])

        by_name = {r["name"]: r for r in rows}
        assert by_name["Single A"]["supported"] is True
        assert by_name["Grid B"]["supported"] is False

    def test_an_unsupported_row_carries_its_reasons(self):
        rows = ts.summarise([_tpl(name="Grid B", mode="grid")])

        assert rows[0]["reasons"]

    def test_every_template_gets_a_row(self):
        """Omitting the unsupported ones would look like they do not exist."""
        rows = ts.summarise([_tpl(name="A"), _tpl(name="B", mode="grid"),
                             _tpl(name="C")])

        assert [r["name"] for r in rows] == ["A", "B", "C"]

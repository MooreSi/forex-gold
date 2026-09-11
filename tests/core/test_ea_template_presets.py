"""The built-in "Reversal ATR v1" template.

Section 2 of docs/todo/reversal-engine/200. A NEW template rather than an
edit of an existing one, so the change is A/B observable instead of a silent
retune of whatever channels already use.

These tests pin the properties the template exists for. Each one corresponds
to a measured problem, and a preset that quietly lost one of them would look
fine and trade like the thing it replaced.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import ea_template_presets as presets
from backend.src.services.broker import ea_templates as et


@pytest.fixture
def tpl():
    return presets.REVERSAL_ATR_V1


class TestItIsAValidTemplate:
    def test_it_survives_the_field_cleaner_unchanged(self):
        """`_clean_fields` drops unknown keys and rejects invalid enum
        values silently. A preset that loses a field there would install
        looking correct and behave like the defaults."""
        clean = et._clean_fields(dict(presets.REVERSAL_ATR_V1))
        for key, value in presets.REVERSAL_ATR_V1.items():
            assert clean[key] == value, f"{key} did not survive cleaning"

    def test_it_names_no_field_the_schema_does_not_have(self):
        assert set(presets.REVERSAL_ATR_V1) <= set(et.DEFAULTS)


class TestTheThingsItExistsToFix:
    def test_the_stop_and_the_first_target_are_the_same_atr_multiple(self):
        """A 1:1 first target at a 59.4% win rate is profitable. The 0.43R
        first target the reference-channel geometry produces is not, and
        that inversion is the engine's whole problem."""
        assert tpl_value("atr_sl_mult") == tpl_value("atr_tp1_mult")

    def test_the_whole_ladder_scales_with_volatility_not_just_tp1(self):
        assert tpl_value("use_dynamic_atr") is True
        assert tpl_value("atr_ladder_scale") is True

    def test_breakeven_arms_only_after_the_first_target_has_paid(self):
        """Item 030: 315 trades with breakeven never moved returned +0.767R
        against 126 that moved at +0.333R. Arming after a booked partial
        cannot convert a winner into a scratch, because the winner has
        already banked something."""
        assert tpl_value("be_trigger") == 1
        assert tpl_value("partials") is True
        assert tpl_value("tp1_pct") > 0

    def test_the_ladder_is_two_rungs_not_eight(self):
        """83% of signals never reach TP1 and 7% reach TP4. Levels 3 to 8
        are decoration, and a decorated ladder makes the pct column lie
        about where the position actually goes."""
        used = [n for n in range(1, et.MAX_TP_LEVELS + 1)
                if tpl_value(f"tp{n}_pips") > 0]
        assert used == [1, 2]

    def test_the_percentages_close_the_whole_position(self):
        total = sum(tpl_value(f"tp{n}_pct") for n in range(1, et.MAX_TP_LEVELS + 1))
        assert total == pytest.approx(100.0)

    def test_a_signal_whose_own_payoff_is_upside_down_is_refused(self):
        assert tpl_value("signal_rr_ratio") >= 0.9

    def test_a_fill_far_beyond_its_zone_is_refused(self):
        """The template-level twin of reversal-engine/040, whose cohort lost
        $2,142: a fill well past the zone is the same adverse selection in
        different coordinates."""
        assert tpl_value("late_guard_pips") > 0


class TestTheThingsItMustNotDo:
    def test_pip_harvest_is_off(self):
        """It shipped as 1.0 and silently overrode the dollar threshold:
        on 2026-08-26 a template set to harvest at $30 closed two trades at
        $1.40. It is not in the UI, so it could not be seen or switched
        off."""
        assert tpl_value("harvest_pips") == 0.0

    def test_the_trail_mode_is_one_the_backtest_can_simulate(self):
        """`template_simulator.py` refuses `staged` and `fractal` outright.
        A template the backtest cannot evaluate is a money change with no
        evidence behind it."""
        assert tpl_value("trail_mode") in ("off", "step", "candle", "tp")

    def test_it_is_single_entry_not_a_grid(self):
        """The simulator refuses grid mode and resting entries too, and a
        grid doubles the exposure question while the payoff one is open."""
        assert tpl_value("mode") == "single"

    def test_it_leaves_portfolio_level_risk_to_the_risk_governor(self):
        """Duplicating equity protection per channel makes attribution
        impossible: two things closed the basket and neither log says
        which."""
        assert tpl_value("equity_protect") == 0.0
        assert tpl_value("basket_harvest_threshold") == 0.0

    def test_the_stop_guards_keep_their_defaults(self):
        """These are what stop a breakeven modification being rejected as an
        invalid stop, the failure that cost a full -$100 on ticket
        1663956102."""
        assert tpl_value("guard_pips") == et.DEFAULTS["guard_pips"]
        assert tpl_value("safety_cap_pips") == et.DEFAULTS["safety_cap_pips"]


class TestInstalling:
    def test_installing_is_explicit_and_never_assigns_a_channel(self):
        """A template that exists but is bound to nothing trades nothing.
        That separation is what makes shipping this safe: the owner selects
        it after a demo session, not because an upgrade installed it."""
        import inspect
        src = inspect.getsource(presets.install)
        assert "channel" not in src.lower()
        assert "override" not in src.lower()


def tpl_value(key):
    return presets.REVERSAL_ATR_V1.get(key, et.DEFAULTS[key])


class TestTheBacktestCanActuallyEvaluateIt:
    """The point of choosing every field inside the simulator's limits. A
    template the backtest refuses is a money change with no evidence behind
    it, which is the thing this repo's rules exist to prevent."""

    def test_the_bar_walk_accepts_it(self):
        from backend.src.services.backtest.template_support import can_simulate
        merged = dict(et.DEFAULTS)
        merged.update(presets.REVERSAL_ATR_V1)
        ok, reasons = can_simulate(merged, atr_available=True)
        assert ok is True, reasons

    def test_the_tick_walk_still_refuses_it_and_says_why(self):
        """Not a defect: a tick series has no candle series to derive an
        ATR from. Worth pinning so nobody reads an empty tick-run row as
        "this template traded nothing"."""
        from backend.src.services.backtest import template_simulator as ts
        merged = dict(et.DEFAULTS)
        merged.update(presets.REVERSAL_ATR_V1)
        assert "atr" in ts.unsupported_reason(merged, tick_mode=True).lower()

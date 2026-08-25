"""Auto mode must only ever hand a channel an EA template or stand_down.

Two things broke on 2026-08-17 because the AI half of Auto could also return
a built-in strategy:

  * Position size changed silently. core_signal_resolution applies the global
    Fixed Lot Size to a built-in (`if strategy_lot > 0 and not _is_template`)
    but leaves a template on its own Anchor/Pending Lot fields. So an AI pick
    of "conservative_trial" over "template:Auto Limit Balanced" moved the
    Reversal Engine from the configured 0.05 to 0.1 -- live on tickets
    1776668203/1776668211. Sizing that depends on which strategy name a model
    happened to return is not sizing anyone configured.
  * The two halves of Auto disagreed. _MAP, _DEFAULT_BY_REGIME and
    apply_baselines can only ever return a template or stand_down, so a
    built-in from the AI half was something the deterministic half would
    immediately contradict on its next pass.

No AI is called in this module -- these are the guarantees that have to hold
regardless of what any provider returns, including when it returns nothing.
"""
import inspect

from backend.src.services.channels import strategy_ai as ai
from backend.src.services.positions import core_auto_template as auto


def test_only_templates_and_stand_down_are_selectable():
    valid = auto.auto_templates() + [auto.STAND_DOWN]
    assert auto.STAND_DOWN in valid
    assert all(s.startswith("template:") for s in valid if s != auto.STAND_DOWN)
    assert len(valid) >= 2


def test_built_in_strategy_names_are_not_selectable():
    """The specific regression: these are real, valid built-in strategies and
    every one of them bypasses template lot sizing."""
    from backend.src.utils.models import STRATEGY_NAMES

    valid = set(auto.auto_templates() + [auto.STAND_DOWN])
    leaked = sorted(set(STRATEGY_NAMES) & valid)
    assert not leaked, f"built-in strategies selectable by Auto: {leaked}"

    for name in ("conservative", "conservative_trial", "limit_runner", "signal_climber"):
        assert name not in valid


def test_evaluator_builds_its_choice_list_from_the_template_map():
    """Guards the wiring, not just the data -- restoring STRATEGY_NAMES to
    valid_strategies is exactly the regression this module exists to stop."""
    src = inspect.getsource(ai.evaluate_channels)
    assert "valid_strategies = _auto.auto_templates() + [_auto.STAND_DOWN]" in src
    assert "list(STRATEGY_NAMES.keys())" not in src


def test_fallback_paths_use_the_backtested_baseline_not_the_rule_regime():
    """rule_regime names a BUILT-IN strategy. Writing it when the API key is
    missing or the provider fails would move every channel off its template
    (and onto the global fixed lot) at exactly the moment nobody is looking."""
    src = inspect.getsource(ai.evaluate_channels)
    assert "set_channel_strategy_rec(src, rule_regime" not in src
    # Both fallback branches resolve a baseline instead.
    assert src.count("_auto.baseline_for(src, _auto_regime)") >= 2


def test_every_baseline_the_fallbacks_can_write_is_itself_selectable():
    """A fallback that wrote something the validator rejects would loop the
    channel straight back through the unknown-strategy path."""
    valid = set(auto.auto_templates() + [auto.STAND_DOWN])
    for ch in ("GOLD DIGGERS INSTITUTIONAL", "Gold Diggers VIP", "Reversal Engine",
               "Gold Diggers Scalping", "Breakout Engine", "some brand new channel"):
        for reg in auto.REGIMES:
            pick = auto.baseline_for(ch, reg)
            assert pick in valid, f"{ch}/{reg} -> {pick} is not selectable"


def test_prompt_does_not_advertise_choices_the_validator_would_reject():
    """The prompt used to print the whole built-in catalogue under 'AVAILABLE
    STRATEGIES' while the validator accepted none of them -- an answer drawn
    from that menu is coerced back to the baseline, wasting the call."""
    src = inspect.getsource(ai.evaluate_channels)
    assert "AVAILABLE CHOICES (EA templates only" in src
    for built_in in ("no_sl_scale:", "protected_scale:", "trail_stop:"):
        assert built_in not in src, f"prompt still offers rule for {built_in}"

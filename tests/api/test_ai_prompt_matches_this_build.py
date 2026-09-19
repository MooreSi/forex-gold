"""The AI is told which engines exist. It should be told the truth.

The signal-generator system prompt describes three engines to the model and
asks it to assess each one. Bounce's code was deleted on 2026-09-14 and the
prompt still described it, so every billable analysis asked a paid model to
judge an engine that does not exist -- and a model asked to assess something
absent will say something about it, because that is what it was asked for.

The prompt is an input to a billable call, not a trading decision, so this is
safe to fix. What it must not do is drift again, which is what this pins: the
engines named in the prompt are checked against the registry's own list.
"""
from __future__ import annotations

from backend.src.controllers import ai_analysis_controller as ai_ctl
from backend.src.services.engines import registry


def _prompt() -> str:
    return ai_ctl.signal_generator_system_prompt()


class TestWhatItClaimsExists:

    def test_it_does_not_describe_an_engine_this_build_deleted(self):
        assert "Bounce Engine" not in _prompt()

    def test_it_still_describes_the_engines_that_do_exist(self):
        # The other half: a prompt that named none of them would pass the test
        # above and tell the model nothing.
        prompt = _prompt()

        assert "Breakout Engine" in prompt
        assert "Reversal Engine" in prompt

    def test_every_engine_it_names_is_one_this_build_runs(self):
        prompt = _prompt()
        named = {name for name in ("breakout", "bounce", "reversal")
                 if f"{name.capitalize()} Engine" in prompt}

        assert named <= set(registry.IMPLEMENTED_NAMES)


class TestItStillAsksForWhatTheScreenRenders:

    def test_it_asks_for_json_only(self):
        # The panel parses the answer into sections. A prose answer is
        # rendered as-is rather than hidden, but the structured one is the
        # point of asking.
        assert "JSON" in _prompt()

    def test_the_schema_is_in_the_prompt(self):
        prompt = _prompt()

        for field in ("overall_assessment", "engines", "collective_verdict",
                      "what_would_make_them_professional"):
            assert field in prompt, field

    def test_each_engine_entry_carries_the_fields_the_panel_shows(self):
        prompt = _prompt()

        for field in ("verdict", "trend", "ml_contribution", "key_strength",
                      "key_weakness", "recommendation"):
            assert field in prompt, field

"""Three subjects, one prompt: two of them were asking the wrong question.

`/api/ai/analyse` gathers the right EVIDENCE for each subject -- channel rows,
strategy-vs-DPM rows, or signal-generator rows -- and then sent every one of
them the SIGNAL GENERATOR system prompt, which opens "You are given
performance data for internal signal generator engines" and ends by demanding
a JSON schema whose top-level key is `engines`.

So asking about Telegram channels handed a paid model a pile of channel rows,
told it they were engines, and asked it to report on engines. A model will
answer that. It will just be answering a question nobody asked.

The NiceGUI page had all three prompts; only one was carried over. These tests
hold each subject to its own.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import ai as ai_router
from backend.src.controllers import ai_analysis_controller as ai_analysis_ctl


def _prompt(subject: str) -> str:
    return ai_analysis_ctl.system_prompt_for(subject)


class TestEverySubjectHasOne:

    @pytest.mark.parametrize("subject", sorted(ai_router.SUBJECTS))
    def test_it_gets_a_prompt(self, subject):
        assert _prompt(subject).strip()

    @pytest.mark.parametrize("subject", sorted(ai_router.SUBJECTS))
    def test_the_prompt_asks_for_json_only(self, subject):
        # Every one of these is rendered as sections by AnswerSection, which
        # falls back to raw text for prose.
        assert "JSON" in _prompt(subject)

    def test_the_three_prompts_are_different(self):
        prompts = {_prompt(s) for s in ai_router.SUBJECTS}

        assert len(prompts) == len(ai_router.SUBJECTS)


class TestEachAsksItsOwnQuestion:

    def test_the_channel_prompt_is_about_channels(self):
        prompt = _prompt("channels")

        assert "signal channel" in prompt.lower()
        assert "engines" not in prompt.lower().split("schema")[0]

    def test_the_channel_prompt_keeps_the_rules_that_stop_a_wrong_verdict(self):
        # These are the owner's own corrections to the model, carried over
        # verbatim from the NiceGUI page. Each exists because the model got it
        # wrong without them: TP1 is the win, entry drift is latency rather
        # than signal quality, and a wider SL may have saved a correct call.
        prompt = _prompt("channels")

        assert "TP1 = WIN" in prompt
        assert "ENTRY DRIFT = LATENCY" in prompt
        assert "WIDER SL ANALYSIS" in prompt

    def test_the_strategy_prompt_is_about_dpm_against_fixed_exits(self):
        prompt = _prompt("strategies")

        assert "DPM" in prompt
        assert "scale_out" in prompt

    def test_the_strategy_prompt_says_what_thin_data_means(self):
        # "< 10 trades" is the original's threshold. A verdict from six trades
        # presented as a verdict is the failure this line exists to stop.
        assert "10 trades" in _prompt("strategies")

    def test_the_generator_prompt_is_still_about_the_engines(self):
        assert "signal generator engines" in _prompt("generator")


class TestTheSchemasReachTheModel:

    def test_the_channel_schema_is_in_its_prompt(self):
        prompt = _prompt("channels")

        for field in ("reliability_score", "phantom_tps", "sl_management",
                      "partial_close_model", "rr_analysis", "entry_drift",
                      "lot_sizing", "overall_recommendation"):
            assert field in prompt, field

    def test_the_strategy_schema_is_in_its_prompt(self):
        prompt = _prompt("strategies")

        for field in ("overall_verdict", "best_approach", "dpm_assessment",
                      "strategy_notes", "actionable_advice"):
            assert field in prompt, field

    def test_the_generator_schema_is_still_in_its_prompt(self):
        assert "collective_verdict" in _prompt("generator")


class TestAnUnknownSubject:

    def test_it_does_not_silently_fall_back_to_another_subject_s_prompt(self):
        # Falling back would send a paid model the wrong question with no
        # trace. The router refuses an unknown subject before this is reached;
        # this is the second line of that defence.
        with pytest.raises(KeyError):
            _prompt("teapot")

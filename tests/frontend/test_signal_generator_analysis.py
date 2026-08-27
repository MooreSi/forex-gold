"""Run Analysis must reach the AI provider, not raise on the way.

docs/todo/bugs/011: the panel's "Run Analysis" button called
ai_provider.complete(cfg, _SIGNAL_GEN_SYSTEM, ...) with a name that was bound
nowhere in the page, so every click raised NameError before a request was ever
built. The prompt itself was never missing -- the M3 page drain had moved it
into ai_analysis_repo alongside its JSON schema and never repointed the caller.

These drive the real function with a fake provider. Nothing here talks to
Anthropic or DeepSeek.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.controllers import ai_analysis_controller as ai_ctl
from frontend.pages import ai_trade_analysis as page


def _data():
    """The shape _build_signal_generator_prompt reads."""
    half = {"count": 5, "win_rate": 60.0, "total_pnl": 120.0,
            "avg_pnl": 24.0, "sl_exits": 2, "be_exits": 1}
    return {
        "days": 7,
        "total_trades": 10,
        "engines": [{
            "label": "Bounce Engine", "strategy": "bounce",
            "all": half, "early_half": half, "late_half": half,
        }],
    }


@pytest.fixture
def fake_provider(monkeypatch):
    """Captures what the page would have sent."""
    sent = {}

    async def _complete(cfg, system, prompt, max_tokens=None, timeout=None):
        sent["system"] = system
        sent["prompt"] = prompt
        return '{"overall_assessment":"ok","engines":[],' \
               '"collective_verdict":"","what_would_make_them_professional":""}'

    monkeypatch.setattr(page.ai_provider, "is_configured", lambda cfg: True)
    monkeypatch.setattr(page.ai_provider, "complete", _complete)
    return sent


class TestItReachesTheProvider:
    def test_the_analysis_returns_a_result_rather_than_an_error(self, fake_provider):
        """The bug's whole signature: it used to come back as
        {"error": "Analysis failed: name '_SIGNAL_GEN_SYSTEM' is not defined"},
        because run_signal_generator_analysis catches Exception and reports it
        as an error dict. The panel then rendered a red card."""
        out = asyncio.run(page.run_signal_generator_analysis(_data(), cfg={}))

        assert "error" not in out, out.get("error")
        assert out["overall_assessment"] == "ok"

    def test_a_system_prompt_actually_reaches_the_provider(self, fake_provider):
        asyncio.run(page.run_signal_generator_analysis(_data(), cfg={}))

        system = fake_provider["system"]
        assert system, "no system prompt was sent at all"
        assert len(system) > 200, "suspiciously short for a system prompt"

    def test_the_prompt_carries_the_json_schema_the_renderer_needs(self, fake_provider):
        """_render_signal_generator_panel reads these keys off the response. A
        prompt that does not ask for them produces a panel that renders blank
        while reporting success."""
        asyncio.run(page.run_signal_generator_analysis(_data(), cfg={}))

        system = fake_provider["system"]
        for key in ("engines", "collective_verdict",
                    "what_would_make_them_professional", "acting_like_pro_trader",
                    "ml_contribution", "self_learning_progress"):
            assert key in system, f"the schema never asks for {key!r}"

    def test_the_engine_data_reaches_the_prompt(self, fake_provider):
        asyncio.run(page.run_signal_generator_analysis(_data(), cfg={}))

        prompt = fake_provider["prompt"]
        assert "Bounce Engine" in prompt
        assert "bounce" in prompt


class TestTheControllerRoute:
    def test_the_page_gets_its_prompt_through_the_controller(self):
        """Not by importing the repo. The frontend talks to controllers, and
        controllers do not import a service's repo -- so this is the only route
        that satisfies the layering contract."""
        system = ai_ctl.signal_generator_system_prompt()
        assert isinstance(system, str) and system.strip()

    def test_it_describes_all_three_engines(self):
        """Bounce, Breakout and Reversal are what gather_signal_generator_data
        reports on. A prompt that only knows two of them invites the model to
        invent the third."""
        system = ai_ctl.signal_generator_system_prompt()
        for engine in ("Bounce Engine", "Breakout Engine", "Reversal Engine"):
            assert engine in system, f"{engine} is not described to the model"


class TestFailuresStayContained:
    def test_an_unconfigured_provider_is_reported_not_raised(self, monkeypatch):
        monkeypatch.setattr(page.ai_provider, "is_configured", lambda cfg: False)
        out = asyncio.run(page.run_signal_generator_analysis(_data(), cfg={}))
        assert out == {"error": "AI provider not configured."}

    def test_invalid_json_is_reported_not_raised(self, monkeypatch):
        """The panel has to survive a model that ignores the schema -- one bad
        response must not take the channel analyses down with it."""
        async def _bad(cfg, system, prompt, max_tokens=None, timeout=None):
            return "not json at all"

        monkeypatch.setattr(page.ai_provider, "is_configured", lambda cfg: True)
        monkeypatch.setattr(page.ai_provider, "complete", _bad)

        out = asyncio.run(page.run_signal_generator_analysis(_data(), cfg={}))
        assert "error" in out and "invalid JSON" in out["error"]

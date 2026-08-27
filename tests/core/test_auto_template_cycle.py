"""One tick of the auto-template loop.

The loop body carried real decision logic and had no test: what force= gets
passed to apply_baselines, when the paid AI review is allowed to run, and what
happens when it fails. It is moving out of runtime.py, so it gets pinned first.

The force= rule is the one with a scar behind it. apply_baselines(force=True)
re-asserts the backtested pick, so forcing it on every 60s tick reverts an AI
override within a minute of it being made. It may only be forced on the first
pass and on an actual regime change.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_auto_template as auto


@pytest.fixture
def spy(monkeypatch):
    calls = {"baselines": [], "ai": 0}

    def _apply(regime, sources=None, force=False):
        calls["baselines"].append({"regime": regime, "sources": sources, "force": force})
        return {}

    async def _evaluate(engine, cfg):
        calls["ai"] += 1
        return {}

    monkeypatch.setattr(auto, "apply_baselines", _apply)
    monkeypatch.setattr(auto, "auto_enabled_sources", lambda: ["chan"])
    monkeypatch.setattr(auto, "regime_from_candles", lambda c: c)
    import backend.src.services.channels.strategy_ai as csai
    monkeypatch.setattr(csai, "evaluate_channels", _evaluate)
    from backend.src.services.ai import provider as ai_provider
    monkeypatch.setattr(ai_provider, "is_configured", lambda cfg: True)
    return calls


def _tick(state, regime="trend", now=0.0):
    return asyncio.run(auto.run_auto_template_cycle(
        state, candles=regime, cfg={}, runtime=object(), now=now))


class TestBaselineForcing:
    def test_the_first_pass_forces_the_baseline(self, spy):
        """Nothing has been written yet, so the pick has to be asserted."""
        _tick(auto.AutoTemplateState())
        assert [c["force"] for c in spy["baselines"]] == [True]

    def test_a_steady_regime_does_not_force(self, spy):
        """The scar: forcing every tick reverts an AI override within 60s."""
        st = auto.AutoTemplateState()
        _tick(st, "trend")
        _tick(st, "trend")
        assert [c["force"] for c in spy["baselines"]] == [True, False]

    def test_a_regime_change_forces_again(self, spy):
        st = auto.AutoTemplateState()
        _tick(st, "trend")
        _tick(st, "range")
        assert [c["force"] for c in spy["baselines"]] == [True, True]

    def test_no_auto_sources_does_nothing_at_all(self, spy, monkeypatch):
        """The default configuration pays neither the detection nor the AI
        cost."""
        monkeypatch.setattr(auto, "auto_enabled_sources", lambda: [])
        _tick(auto.AutoTemplateState())
        assert spy["baselines"] == [] and spy["ai"] == 0


class TestAiCadence:
    def test_the_first_pass_reviews(self, spy):
        _tick(auto.AutoTemplateState(), now=1000.0)
        assert spy["ai"] == 1

    def test_a_steady_regime_does_not_review_again_within_the_interval(self, spy):
        st = auto.AutoTemplateState()
        _tick(st, "trend", now=1000.0)
        _tick(st, "trend", now=1000.0 + 899)
        assert spy["ai"] == 1

    def test_it_reviews_again_once_the_interval_has_passed(self, spy):
        st = auto.AutoTemplateState()
        _tick(st, "trend", now=1000.0)
        _tick(st, "trend", now=1000.0 + 900)
        assert spy["ai"] == 2

    def test_a_regime_change_reviews_immediately(self, spy):
        """Ahead of the interval -- a flip is exactly when the pick is most
        likely to be wrong."""
        st = auto.AutoTemplateState()
        _tick(st, "trend", now=1000.0)
        _tick(st, "range", now=1000.0 + 10)
        assert spy["ai"] == 2

    def test_no_review_when_the_ai_is_not_configured(self, spy, monkeypatch):
        """This is the only part that costs money."""
        from backend.src.services.ai import provider as ai_provider
        monkeypatch.setattr(ai_provider, "is_configured", lambda cfg: False)
        _tick(auto.AutoTemplateState())
        assert spy["ai"] == 0
        assert spy["baselines"], "detection is free and must still run"


class TestFailureHandling:
    def test_a_failed_review_leaves_the_backtested_baseline_in_place(self, spy, monkeypatch):
        """It degrades to backtested behaviour, not to no management."""
        async def _boom(engine, cfg):
            raise RuntimeError("anthropic down")
        import backend.src.services.channels.strategy_ai as csai
        monkeypatch.setattr(csai, "evaluate_channels", _boom)
        _tick(auto.AutoTemplateState())
        assert [c["force"] for c in spy["baselines"]] == [True]

    def test_a_failed_review_does_not_retry_on_the_very_next_tick(self, spy, monkeypatch):
        """The attempt is what costs, not the success. Retrying every 60s
        after an outage is the credit burn this cadence exists to prevent."""
        async def _boom(engine, cfg):
            spy["ai"] += 1
            raise RuntimeError("anthropic down")
        import backend.src.services.channels.strategy_ai as csai
        monkeypatch.setattr(csai, "evaluate_channels", _boom)
        st = auto.AutoTemplateState()
        _tick(st, "trend", now=1000.0)
        _tick(st, "trend", now=1000.0 + 60)
        assert spy["ai"] == 1

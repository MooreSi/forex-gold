"""Auto template switching waits for a regime to hold. docs/todo/016 item 7.

`dpm_engine.detect_regime` is ADX(14) on 30 M5 bars around fixed 20/25
cutoffs, and every change of its label forces the baseline templates and
triggers a paid AI review (core_auto_template.run_auto_template_cycle). An
ADX sitting on a cutoff flips that label back and forth. Expert Tunable
`auto_regime_min_dwell_ticks` makes a new regime count only once it has been
read on that many consecutive one-minute ticks.

**The default is 1, which is exactly the behaviour before the tunable
existed**: the first tick that reads a new regime acts on it. Nothing
changes on upgrade, and no source is on Auto today anyway.

Fakes only. Nothing here reaches a broker.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_auto_template as auto


@pytest.fixture
def spy(monkeypatch):
    calls = {"baselines": [], "ai": 0}

    def _apply(regime, sources=None, force=False):
        calls["baselines"].append((regime, force))
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


def _dwell(monkeypatch, n):
    monkeypatch.setattr(auto, "_min_dwell_ticks", lambda: n)


def _ticks(state, regimes):
    for i, r in enumerate(regimes):
        asyncio.run(auto.run_auto_template_cycle(
            state, candles=r, cfg={}, runtime=object(), now=1000.0 + i))


class TestTheDefaultIsTodaysBehaviour:
    def test_the_tunable_defaults_to_one(self):
        from backend.src.services.risk import expert_params as ep
        spec = ep.spec("auto_regime_min_dwell_ticks")
        assert spec is not None and spec.default == 1 and spec.integer

    def test_a_dwell_of_one_acts_on_the_first_different_tick(self, spy, monkeypatch):
        _dwell(monkeypatch, 1)
        st = auto.AutoTemplateState()
        _ticks(st, ["trending", "ranging", "trending"])
        assert spy["baselines"] == [("trending", True), ("ranging", True),
                                    ("trending", True)]

    def test_an_unreadable_tunable_falls_back_to_one(self, monkeypatch):
        from backend.src.services.risk import expert_params as ep

        def boom(key):
            raise RuntimeError("no database")
        monkeypatch.setattr(ep, "get", boom)
        assert auto._min_dwell_ticks() == 1


class TestADwellOfThree:
    def test_a_one_tick_flicker_is_ignored(self, spy, monkeypatch):
        _dwell(monkeypatch, 3)
        st = auto.AutoTemplateState()
        _ticks(st, ["trending", "ranging", "trending", "trending"])
        assert [r for r, _ in spy["baselines"]] == ["trending"] * 4
        assert [f for _, f in spy["baselines"]] == [True, False, False, False]
        assert spy["ai"] == 1   # only the first pass reviewed

    def test_a_regime_that_holds_three_ticks_is_acted_on_at_the_third(self, spy, monkeypatch):
        _dwell(monkeypatch, 3)
        st = auto.AutoTemplateState()
        _ticks(st, ["trending", "ranging", "ranging", "ranging"])
        assert spy["baselines"] == [("trending", True), ("trending", False),
                                    ("trending", False), ("ranging", True)]
        assert st.last_regime == "ranging"

    def test_an_interrupted_run_starts_counting_again(self, spy, monkeypatch):
        _dwell(monkeypatch, 3)
        st = auto.AutoTemplateState()
        _ticks(st, ["trending", "ranging", "ranging", "spike", "ranging", "ranging"])
        assert all(r == "trending" for r, _ in spy["baselines"])

    def test_the_first_pass_never_waits(self, spy, monkeypatch):
        """With nothing written yet there is no current regime to hold on to."""
        _dwell(monkeypatch, 5)
        _ticks(auto.AutoTemplateState(), ["ranging"])
        assert spy["baselines"] == [("ranging", True)]

"""Letting an AI set the engine's capability switches, safely.

Owner request, 2026-09-11: a button that has the configured AI read the
market and set the parameters itself, re-analysing every fifteen minutes,
plus a one-shot Recommend that uses the ML evidence and the AI together.

**This writes live trading settings from a model's free-text output**, so
almost all of the code is refusal. The tests below are the refusals. What
the AI is allowed to touch is a fixed list; everything else it returns is
dropped, every number it returns is clamped to the same range the form
allows, and anything it says that cannot be parsed changes nothing at all.

Sizing is not on the list and never will be by this route.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.reversal_engine import ai_tuner


class TestWhatItMayChange:
    def test_the_capability_switches_are_tunable(self):
        assert "re_atr_barriers_enabled" in ai_tuner.TUNABLE
        assert "meta_label_threshold" in ai_tuner.TUNABLE
        assert "re_blocked_level_types" in ai_tuner.TUNABLE

    def test_sizing_is_not_tunable_by_an_ai(self):
        """Position sizing is on this repo's stop-and-ask list. An AI
        adjusting it every fifteen minutes without a human in the loop is
        the single worst thing this feature could be allowed to do."""
        assert "vol_target_sizing_enabled" not in ai_tuner.TUNABLE
        assert "correlated_exposure_cap_lots" not in ai_tuner.TUNABLE

    def test_live_execution_is_not_tunable_by_an_ai(self):
        assert "re_live_execution" not in ai_tuner.TUNABLE
        assert not any("live" in k for k in ai_tuner.TUNABLE)


class TestSanitising:
    def test_an_unknown_key_is_dropped(self):
        out = ai_tuner.sanitise({"re_live_execution": 1, "max_lot_size": 99,
                                 "meta_label_threshold": 0.6})
        assert out == {"meta_label_threshold": 0.6}

    def test_a_number_beyond_the_allowed_range_is_clamped_not_refused(self):
        """Clamped rather than dropped: a model asking for a 40x ATR stop
        has misjudged the scale, not the direction, and the clamp is the
        same one the form applies to a human."""
        assert ai_tuner.sanitise({"re_atr_stop_mult": 40.0})["re_atr_stop_mult"] == 5.0
        assert ai_tuner.sanitise({"re_atr_stop_mult": -3.0})["re_atr_stop_mult"] == 0.2

    def test_a_boolean_comes_through_as_zero_or_one(self):
        assert ai_tuner.sanitise({"entry_trigger_enabled": True})["entry_trigger_enabled"] == 1
        assert ai_tuner.sanitise({"entry_trigger_enabled": "yes"})["entry_trigger_enabled"] == 1
        assert ai_tuner.sanitise({"entry_trigger_enabled": 0})["entry_trigger_enabled"] == 0

    def test_a_refusal_list_keeps_only_real_level_types(self):
        out = ai_tuner.sanitise({"re_blocked_level_types": ["round_5", "nonsense", "UNICORN"]})
        assert out["re_blocked_level_types"] == "round_5,unicorn"

    def test_a_refusal_list_that_would_block_everything_is_refused(self):
        """A model that decides nothing is tradeable has not tuned the
        engine, it has switched it off, and it must not be able to do that
        silently through a settings field."""
        from backend.src.services.risk import capability_gates as cg
        out = ai_tuner.sanitise({"re_blocked_level_types": list(cg.KNOWN_LEVEL_TYPES)})
        assert "re_blocked_level_types" not in out

    def test_junk_values_are_dropped_rather_than_coerced_to_zero(self):
        out = ai_tuner.sanitise({"meta_label_threshold": "quite high"})
        assert out == {}

    def test_nothing_in_gives_nothing_out(self):
        assert ai_tuner.sanitise({}) == {}
        assert ai_tuner.sanitise(None) == {}


class TestParsingWhatTheModelSaid:
    def test_it_reads_a_plain_json_object(self):
        raw = json.dumps({"settings": {"meta_label_threshold": 0.7},
                          "rationale": "gate is too loose"})
        got = ai_tuner.parse_response(raw)
        assert got["settings"] == {"meta_label_threshold": 0.7}
        assert "loose" in got["rationale"]

    def test_it_digs_the_object_out_of_a_fenced_block(self):
        raw = "Here you go:\n```json\n{\"settings\": {\"entry_trigger_enabled\": 1}}\n```"
        assert ai_tuner.parse_response(raw)["settings"]["entry_trigger_enabled"] == 1

    def test_unparseable_output_changes_nothing_and_says_so(self):
        got = ai_tuner.parse_response("I am unable to help with that.")
        assert got["settings"] == {}
        assert got["error"]

    def test_an_empty_response_changes_nothing(self):
        assert ai_tuner.parse_response("")["settings"] == {}


class TestTheAutoLoop:
    def test_it_does_nothing_while_the_switch_is_off(self, monkeypatch):
        called = []
        monkeypatch.setattr(ai_tuner, "recommend",
                            lambda *a, **k: called.append(1))
        out = asyncio.run(ai_tuner.auto_tune(bridge=None, rs={"re_ai_tuning_enabled": 0}))
        assert out["skipped"] == "off"
        assert called == []

    def test_with_the_switch_on_it_applies_what_it_is_given(self, monkeypatch):
        applied = {}
        monkeypatch.setattr(ai_tuner, "_write_settings", lambda d: applied.update(d))

        async def _fake(bridge, rs=None):
            return {"settings": {"meta_label_threshold": 0.65},
                    "rationale": "tightening", "evidence": {}}
        monkeypatch.setattr(ai_tuner, "recommend", _fake)

        out = asyncio.run(ai_tuner.auto_tune(bridge=None,
                                             rs={"re_ai_tuning_enabled": 1}))
        assert applied == {"meta_label_threshold": 0.65}
        assert out["applied"] == {"meta_label_threshold": 0.65}

    def test_a_recommendation_it_cannot_use_writes_nothing(self, monkeypatch):
        applied = {}
        monkeypatch.setattr(ai_tuner, "_write_settings", lambda d: applied.update(d))

        async def _fake(bridge, rs=None):
            return {"settings": {}, "rationale": "", "error": "no json"}
        monkeypatch.setattr(ai_tuner, "recommend", _fake)

        asyncio.run(ai_tuner.auto_tune(bridge=None, rs={"re_ai_tuning_enabled": 1}))
        assert applied == {}

    def test_the_ai_failing_never_raises_into_the_engine_loop(self, monkeypatch):
        async def _boom(bridge, rs=None):
            raise RuntimeError("provider down")
        monkeypatch.setattr(ai_tuner, "recommend", _boom)
        out = asyncio.run(ai_tuner.auto_tune(bridge=None,
                                             rs={"re_ai_tuning_enabled": 1}))
        assert "error" in out


class TestTheEngineLoopEntryPoint:
    def test_tune_once_reads_the_switch_itself(self, monkeypatch):
        """The engine decides WHEN; this decides whether and what. Keeping
        the settings read here is what lets the service stay an
        orchestrator and stay under its line ceiling."""
        seen = {}

        async def _fake(bridge, rs=None):
            seen["rs"] = rs
            return {"skipped": "off"}
        monkeypatch.setattr(ai_tuner, "auto_tune", _fake)
        monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                            lambda: {"re_ai_tuning_enabled": 0})
        asyncio.run(ai_tuner.tune_once(bridge=None))
        assert seen["rs"] == {"re_ai_tuning_enabled": 0}

    def test_an_unreadable_settings_row_does_not_raise(self, monkeypatch):
        def _boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr("backend.src.db.database.get_risk_settings", _boom)
        assert "error" in asyncio.run(ai_tuner.tune_once(bridge=None))

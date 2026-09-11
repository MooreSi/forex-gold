"""The Reversal Engine operations the panel reaches through the controller.

Added 2026-09-11 with the research study, the AI tuner and the reporting
reset. The frontend may only reach the backend through a controller (a
layer rule enforced at zero), so every one of these is the only route the
panel has, and a forwarder that quietly stopped forwarding would look like
a dead button.

Nothing here reaches a broker, the network or an AI provider.
"""
from __future__ import annotations

import asyncio
from unittest.mock import patch

import pytest

from backend.src.controllers import engines_controller as ec


class TestTheResearchStudy:
    def test_it_refuses_politely_when_the_engine_is_not_running(self):
        """No engine means no broker connection to read history through.
        A message the user can act on, not an exception."""
        with patch.object(ec._re_svc, "get_instance", return_value=None):
            out = asyncio.run(ec.reversal_research_study())
        assert "not running" in out.lower()

    def test_it_renders_the_study_when_there_is_a_bridge(self):
        class _Engine:
            _bridge = object()

        async def _fake_run(bridge, **kw):
            return {"n_closed": 3}

        with patch.object(ec._re_svc, "get_instance", return_value=_Engine()):
            with patch("backend.src.services.reversal_engine.research_lab.run_study",
                       _fake_run):
                with patch("backend.src.services.reversal_engine.research_lab.render",
                           return_value="REPORT") as render:
                    out = asyncio.run(ec.reversal_research_study())
        assert out == "REPORT"
        assert render.call_args[0][0] == {"n_closed": 3}


class TestTheShadowReport:
    def test_it_forwards_to_the_service(self):
        rows = [{"variant": "live (champion)", "is_champion": True}]
        with patch("backend.src.services.reversal_engine.shadow.report",
                   return_value=rows) as fwd:
            assert ec.reversal_shadow_report() == rows
        assert fwd.call_count == 1


class TestTheMacroBackfill:
    def test_it_is_a_dry_run_unless_told_otherwise(self):
        with patch("backend.src.services.reversal_engine.macro_backfill.run",
                   return_value={"dry_run": True}) as fwd:
            ec.reversal_macro_backfill()
        assert fwd.call_args.kwargs["apply"] is False

    def test_applying_is_passed_through(self):
        with patch("backend.src.services.reversal_engine.macro_backfill.run",
                   return_value={"dry_run": False}) as fwd:
            ec.reversal_macro_backfill(apply=True)
        assert fwd.call_args.kwargs["apply"] is True


class TestTheAiTuner:
    def test_recommend_passes_the_current_settings_and_the_bridge(self):
        class _Engine:
            _bridge = "BRIDGE"
        seen = {}

        async def _fake(bridge, rs):
            seen["bridge"] = bridge
            seen["rs"] = rs
            return {"settings": {}, "rationale": ""}

        with patch.object(ec._re_svc, "get_instance", return_value=_Engine()):
            with patch.object(ec, "get_risk_settings", return_value={"x": 1}):
                with patch("backend.src.services.reversal_engine.ai_tuner.recommend",
                           _fake):
                    asyncio.run(ec.reversal_ai_recommend())
        assert seen == {"bridge": "BRIDGE", "rs": {"x": 1}}

    def test_recommend_still_works_with_no_engine_running(self):
        async def _fake(bridge, rs):
            return {"settings": {}, "rationale": "", "bridge_was": bridge}

        with patch.object(ec._re_svc, "get_instance", return_value=None):
            with patch.object(ec, "get_risk_settings", return_value={}):
                with patch("backend.src.services.reversal_engine.ai_tuner.recommend",
                           _fake):
                    out = asyncio.run(ec.reversal_ai_recommend())
        assert out["bridge_was"] is None

    def test_apply_re_sanitises_rather_than_trusting_what_came_back(self):
        """What reaches this has been through a browser and back. The
        allowlist is the only thing between a model's output and a live
        trading setting, so it is applied again here."""
        with patch.object(ec, "update_risk_settings") as write:
            written = ec.reversal_ai_apply(
                {"meta_label_threshold": 0.7, "re_live_execution": 1,
                 "max_lot_size": 50})
        assert written == {"meta_label_threshold": 0.7}
        assert write.call_args[0][0] == {"meta_label_threshold": 0.7}

    def test_apply_writes_nothing_when_nothing_survives(self):
        with patch.object(ec, "update_risk_settings") as write:
            assert ec.reversal_ai_apply({"re_live_execution": 1}) == {}
        assert write.call_count == 0


class TestTheStatsReset:
    def test_it_forwards_to_the_panel_service(self):
        async def _fake():
            return 1234.0
        with patch.object(ec.reversal, "reset_stats", _fake):
            assert asyncio.run(ec.reversal_reset_stats()) == 1234.0

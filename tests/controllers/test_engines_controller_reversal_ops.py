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
import inspect
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
    """Asserted on the SERVICE since 2026-09-18.

    It was re-exported through the controller until then, and nothing called
    it there: it is a manual repair tool, not a screen. A controller operation
    exists for a router, and applying this changes what the ML gate learns at
    its next retrain — not something to leave one HTTP route away.

    The property below is unchanged and is the one that matters: **the default
    reports and writes nothing.** A repair tool whose default is to repair is
    one somebody runs to "see what it would do".
    """

    def test_it_is_a_dry_run_unless_told_otherwise(self):
        from backend.src.services.reversal_engine import macro_backfill

        apply_param = inspect.signature(macro_backfill.run).parameters["apply"]

        assert apply_param.default is False

    def test_applying_is_still_possible(self):
        """Negative control for the one above: a function with no `apply` at
        all would satisfy "the default does not write" trivially."""
        from backend.src.services.reversal_engine import macro_backfill

        assert "apply" in inspect.signature(macro_backfill.run).parameters

    def test_it_is_not_reachable_through_the_controller(self):
        assert not hasattr(ec, "reversal_macro_backfill"), (
            "the repair tool is back on the controller, one route away from "
            "a button that rewrites stored training vectors"
        )


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

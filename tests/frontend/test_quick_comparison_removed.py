"""The Quick comparison table is gone from Trading > Strategy.

Owner, 2026-09-02: "remove the quick comparison at the bottom of the page".

Safe to remove on its own: the table only READ strategy definitions and drew
them side by side. Nothing selects a strategy through it, and nothing about
how a trade is entered or managed goes through it.

The rest of item 4 -- removing the Strategy Parameters card and turning the
built-in strategies into EA templates -- is deliberately NOT done here. See
docs/simon-handover/023.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "frontend" / "pages" / "trading" / "_strategy.py"


def _code() -> str:
    return "\n".join(ln for ln in SRC.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


class TestTheTableIsGone:
    def test_no_quick_comparison_heading(self):
        assert "Quick comparison" not in _code()

    def test_the_draw_helper_is_gone(self):
        assert "_draw_compare" not in _code()

    def test_the_comparison_groups_are_gone(self):
        code = _code()
        assert "_COMPARE_GROUP_1" not in code
        assert "_COMPARE_GROUP_2" not in code


class TestTheRestOfThePageSurvives:
    """The cards above the table are untouched -- removing a read-only
    display must not take a control with it."""

    def test_channel_strategy_still_renders(self):
        assert "_render_channel_strategy_card" in _code()

    def test_global_parameters_still_renders(self):
        assert "_render_global_parameters_card" in _code()

    def test_ea_templates_still_render(self):
        assert "_render_ea_templates_card" in _code()

    def test_strategy_parameters_is_now_gone_too(self):
        """It was kept when the comparison table went, because channels were
        still bound to built-in strategies and this card was the only way to
        tune the geometry they entered with.

        Removed 2026-09-03 once the owner confirmed every channel is on a
        template and the built-ins left the picker. Verified against the
        database first: everything trading that day was on a template.
        """
        assert "_render_strategy_params_card" not in _code()

    def test_the_module_still_parses(self):
        ast.parse(SRC.read_text(encoding="utf-8"))

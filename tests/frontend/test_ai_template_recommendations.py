"""AI Analysis recommends an EA template, per channel.

Owner, 2026-09-02: "under the recommended strategy this needs to break it down
by channel and now just recommend an EA template".

Half of this was already true and worth stating, because it changes what
needed building: the AI has picked from EA templates since 2026-08-17 --
`strategy_ai.py` builds its candidate list as `auto_templates() +
[STAND_DOWN]`, not from STRATEGY_NAMES -- and the picks are already stored per
channel in `channel_strategy_rec`.

What was missing was the display. The AI Analysis page showed one free-text
"Overall Recommendation" for everything, and its strategy/DPM panel still
spoke in built-in strategy names (scale_out, be_runner, trail_stop). The
per-channel template picks sat in the database unused by this page.

So this is a rendering change over data that already exists. It recommends
nothing new and applies nothing -- selecting a template stays a deliberate act
on Trading > Strategy.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from frontend.pages.ai_trade_analysis import _panels

REPO = pathlib.Path(__file__).resolve().parents[2]


class TestThePanelExists:
    def test_it_is_defined(self):
        assert hasattr(_panels, "_render_template_recs_panel")

    def test_the_page_renders_it(self):
        src = (REPO / "frontend/pages/ai_trade_analysis/__init__.py").read_text(
            encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "_render_template_recs_panel" in code


class TestWhatItShows:
    """Driven through the row builder, which is where the decisions are."""

    def test_a_channel_with_a_recommendation_is_listed(self):
        rows = _panels._template_rec_rows(
            [{"channel_name": "Gold Diggers VIP", "source": "gd"}],
            {"gd": {"strategy": "template:Auto Limit Balanced",
                    "reasoning": "ranging", "confidence": 0.6}},
        )

        assert len(rows) == 1
        assert rows[0]["channel"] == "Gold Diggers VIP"

    def test_the_template_prefix_is_stripped_for_display(self):
        """The stored value is "template:<name>" -- the prefix is machinery,
        not something to show the user."""
        rows = _panels._template_rec_rows(
            [{"channel_name": "A", "source": "a"}],
            {"a": {"strategy": "template:Auto Limit Balanced",
                   "reasoning": "", "confidence": 0.6}},
        )

        assert rows[0]["template"] == "Auto Limit Balanced"

    def test_stand_down_is_shown_as_words_not_a_token(self):
        """STAND_DOWN is a real recommendation -- trade nothing here -- and
        showing the raw token would read as a broken template name."""
        rows = _panels._template_rec_rows(
            [{"channel_name": "A", "source": "a"}],
            {"a": {"strategy": "stand_down", "reasoning": "choppy",
                   "confidence": 0.8}},
        )

        assert rows[0]["template"].lower().startswith("stand down")

    def test_a_channel_with_no_recommendation_says_so(self):
        """Silently omitting it would look like the channel does not exist."""
        rows = _panels._template_rec_rows(
            [{"channel_name": "A", "source": "a"}], {})

        assert len(rows) == 1
        assert rows[0]["template"] == "—"
        assert rows[0]["confidence"] is None

    def test_confidence_is_carried_through(self):
        rows = _panels._template_rec_rows(
            [{"channel_name": "A", "source": "a"}],
            {"a": {"strategy": "template:X", "reasoning": "", "confidence": 0.42}},
        )

        assert rows[0]["confidence"] == pytest.approx(0.42)

    def test_the_reasoning_is_carried_through(self):
        """Without it the panel is a verdict with no argument."""
        rows = _panels._template_rec_rows(
            [{"channel_name": "A", "source": "a"}],
            {"a": {"strategy": "template:X", "reasoning": "trend regime",
                   "confidence": 0.6}},
        )

        assert rows[0]["reasoning"] == "trend regime"

    def test_every_channel_gets_a_row(self):
        rows = _panels._template_rec_rows(
            [{"channel_name": "A", "source": "a"},
             {"channel_name": "B", "source": "b"},
             {"channel_name": "C", "source": "c"}],
            {"b": {"strategy": "template:X", "reasoning": "", "confidence": 0.5}},
        )

        assert [r["channel"] for r in rows] == ["A", "B", "C"]


class TestItRecommendsTemplatesNotStrategies:
    def test_the_ai_candidate_list_is_templates(self):
        """Pins what was already true, so a change back is visible."""
        src = (REPO / "backend/src/services/channels/strategy_ai.py").read_text(
            encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "auto_templates()" in code
        assert "valid_strategies = _auto.auto_templates()" in code

    def test_the_panel_module_still_parses(self):
        ast.parse((REPO / "frontend/pages/ai_trade_analysis/_panels.py"
                   ).read_text(encoding="utf-8"))

"""The channel-strategy rows show no win-rate / P&L stats.

Owner, 2026-09-02: "next to each engine or telegram channel remove the WR
stats - don't need this". The row is a picker: it exists to choose which EA
template or engine handles a channel, and a per-channel win rate beside it
invites reading a strategy decision off a number that says nothing about the
template being chosen.

Structural, because the alternative is rendering the whole Trading page to
assert the absence of a label -- and an absence is exactly what a render test
is worst at proving.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
SRC = REPO / "frontend" / "pages" / "trading" / "_strategy_cards.py"


def _code() -> str:
    """Source with comments stripped.

    Comments are removed deliberately: this file explains WHY the stats went,
    and a plain substring search would match that explanation and pass with
    the stats still rendering. That trap has cost this repo three false
    passes already.
    """
    return "\n".join(ln for ln in SRC.read_text(encoding="utf-8").splitlines()
                     if not ln.strip().startswith("#"))


class TestTheStatsAreGone:
    def test_no_win_rate_is_rendered(self):
        assert "win_rate" not in _code()

    def test_no_WR_label_is_built(self):
        assert "WR " not in _code()

    def test_no_stats_label_remains(self):
        assert "stats_txt" not in _code()


class TestThePickerStillWorks:
    """Removing a label must not take the control with it."""

    def test_the_strategy_select_is_still_built(self):
        assert "strat_opts" in _code()

    def test_the_recommendation_icon_is_still_built(self):
        code = _code()
        assert "get_channel_strategy_rec" in code
        assert "psychology" in code

    def test_the_module_still_parses(self):
        ast.parse(SRC.read_text(encoding="utf-8"))

"""Refuse the level types that lose money.

The first thing the live study measured that nothing in the app can act on.
Attribution over 785 closed live trades, 2026-09-11:

    unicorn       18   77.8%   +0.559R    +$625
    swing_high    44   65.9%   +0.011R     -$44
    congestion   169   63.9%   -0.082R    -$325
    round_10     243   59.7%   -0.062R    -$809
    round_5      210   55.2%   -0.157R  -$1,323

`score_level` rates `round_5` highest of all at 0.78, because its weights
were calibrated against how often the reference channel fired near a level
rather than against whether the trade made money. The owner retired that
objective on 2026-09-11 (simon-handover/029).

Refitting those weights changes every score in the system. Refusing a named
type is the smaller, reversible move that the evidence already supports, and
it is a list the owner controls rather than a number a model moved.

Empty by default: nothing is refused until somebody names it.
"""
from __future__ import annotations

import pytest

from backend.src.services.reversal_engine import cycle_setup as cs
from backend.src.services.risk import capability_gates as cg


def lvl(t, price=3300.0):
    return {"price": price, "type": t, "score": 0.9, "direction": "BUY"}


class TestReadingTheSetting:
    def test_nothing_is_blocked_by_default(self):
        assert cg.blocked_level_types({"re_blocked_level_types": ""}) == set()

    def test_a_settings_row_without_the_column_blocks_nothing(self):
        assert cg.blocked_level_types({}) == set()

    def test_a_comma_separated_list_is_parsed(self):
        assert cg.blocked_level_types(
            {"re_blocked_level_types": "round_5,congestion"}) == {"round_5", "congestion"}

    def test_whitespace_and_case_do_not_matter(self):
        assert cg.blocked_level_types(
            {"re_blocked_level_types": " Round_5 , CONGESTION "}) == {"round_5", "congestion"}

    def test_empty_entries_are_dropped_rather_than_blocking_a_blank_type(self):
        """A trailing comma is the obvious way to produce an empty string,
        and a level whose type is missing reads as "" -- so a stray comma
        would silently start refusing those."""
        assert cg.blocked_level_types(
            {"re_blocked_level_types": "round_5,,"}) == {"round_5"}


class TestFilteringCandidates:
    def test_with_nothing_blocked_every_candidate_survives(self):
        cands = [lvl("round_5"), lvl("unicorn"), lvl("swing_high")]
        assert cs.filter_blocked_types(cands, {}) == cands

    def test_a_blocked_type_is_removed(self):
        cands = [lvl("round_5"), lvl("unicorn")]
        out = cs.filter_blocked_types(cands, {"re_blocked_level_types": "round_5"})
        assert [c["type"] for c in out] == ["unicorn"]

    def test_a_level_with_no_type_is_kept_rather_than_silently_dropped(self):
        """An untyped candidate is a bug somewhere upstream. Dropping it
        here would hide that bug and quietly reduce what the engine trades."""
        cands = [{"price": 3300.0, "score": 0.9, "direction": "BUY"}]
        assert cs.filter_blocked_types(cands, {"re_blocked_level_types": "round_5"}) == cands

    def test_blocking_every_type_leaves_nothing(self):
        cands = [lvl("round_5"), lvl("round_10")]
        assert cs.filter_blocked_types(
            cands, {"re_blocked_level_types": "round_5,round_10"}) == []

    def test_an_empty_candidate_list_is_returned_unchanged(self):
        assert cs.filter_blocked_types([], {"re_blocked_level_types": "round_5"}) == []


class TestTheTypeListOfferedToTheUser:
    def test_it_covers_every_type_the_detector_can_emit(self):
        """A type missing from the list is a type nobody can refuse, which
        is how the control silently stops covering the thing it was built
        for when a new level source is added."""
        from backend.src.services.reversal_engine import level_detector as ld
        from backend.src.services.market import liquidity_map as lm
        known = set(cg.KNOWN_LEVEL_TYPES)
        assert set(lm.LEVEL_TYPES) <= known
        assert {"asia_low", "asia_high", "swing_high", "swing_low",
                "round_10", "round_5", "congestion", "unicorn"} <= known

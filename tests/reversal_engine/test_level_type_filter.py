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


class TestRefusingATypeInTheAsianSessionOnly:
    """Owner, 2026-11-17: drop round-number levels during the Asian session.

    The 14-day benchmark had round_5 and round_10 as the worst cells and the
    Asian session as the worst session (55% won against 76% by chance), while
    London and NY round numbers sat near chance. Refusing them everywhere would
    remove the sessions where they are not the problem, so this list applies
    only while the engine reads the session as "asian". It adds to the
    all-session list rather than replacing it.
    """

    RS = {"re_blocked_level_types": "congestion",
          "re_asian_blocked_level_types": "round_5,round_10"}

    def test_in_the_asian_session_both_lists_apply(self):
        assert cg.blocked_level_types(self.RS, "asian") == {
            "congestion", "round_5", "round_10"}

    @pytest.mark.parametrize("session", ["london", "overlap", "ny", "off"])
    def test_outside_the_asian_session_only_the_all_session_list_applies(self, session):
        assert cg.blocked_level_types(self.RS, session) == {"congestion"}

    def test_a_caller_that_names_no_session_gets_the_all_session_list(self):
        """Every existing caller passes no session. Its answer must not change."""
        assert cg.blocked_level_types(self.RS) == {"congestion"}

    def test_the_session_name_is_read_case_blind(self):
        assert "round_5" in cg.blocked_level_types(self.RS, "Asian")

    def test_empty_by_default_so_nothing_new_is_refused_until_named(self):
        assert cg.blocked_level_types({"re_asian_blocked_level_types": ""}, "asian") == set()

    def test_the_filter_drops_round_numbers_in_asia_and_keeps_them_in_london(self):
        cands = [lvl("round_5"), lvl("round_10"), lvl("swing_high")]
        rs = {"re_asian_blocked_level_types": "round_5,round_10"}
        assert [c["type"] for c in cs.filter_blocked_types(cands, rs, "asian")] == ["swing_high"]
        assert cs.filter_blocked_types(cands, rs, "london") == cands


class TestTheAsianListIsWired:
    """Asserted separately from the behaviour: a filter nothing passes the
    session to refuses nothing and looks healthy doing it."""

    def _src(self, rel):
        from pathlib import Path
        return (Path(__file__).resolve().parents[2] / rel).read_text(encoding="utf-8")

    def test_the_engine_passes_the_session_to_the_filter(self):
        src = self._src("backend/src/services/reversal_engine/reversal_engine_service.py")
        assert "filter_blocked_types(candidates, _rs, session)" in src

    def test_a_migration_adds_the_column_empty_by_default(self):
        line = next(l for l in self._src("backend/migrations/steps_recent.py").splitlines()
                    if "ADD COLUMN re_asian_blocked_level_types" in l)
        assert "DEFAULT ''" in line, line

    def test_it_travels_to_the_node_that_trades(self):
        from backend.src.services.cluster.sync import synced_settings as ss
        assert "re_asian_blocked_level_types" in ss.SYNCED_SETTINGS_KEYS

    def test_a_fresh_database_has_the_column_and_it_is_empty(self, fresh_db):
        from backend.src.db import database as db
        assert db.get_risk_settings().get("re_asian_blocked_level_types") == ""

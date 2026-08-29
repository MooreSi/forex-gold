"""AI-derived parse rules applied to live Telegram messages.

These are regexes a language model wrote, saved after approval, and then run
against real signals ahead of the hand-written parsers. Whatever they extract
becomes an entry, a stop and a set of targets -- i.e. an order. So the
interesting cases are not the happy path but every way a rule can be wrong:

  * a pattern that does not compile (a model can emit anything);
  * a pattern that compiles but captures the wrong number of groups, which is
    how a stop-loss becomes whatever number happened to be nearby;
  * a pattern long enough to be a denial-of-service on the regex engine;
  * a rule that matches only some of what it needs, which must yield NOTHING
    rather than a half-built signal.

A half-built signal is the dangerous outcome: a dict with an entry and no stop
is a trade with no protection.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.signals import _learned_rules as lr


GOOD_RULE = {
    "gate_pattern": r"XAUUSD",
    "direction_pattern": r"\b(BUY|SELL)\b",
    "entry_pattern": r"(\d{4}\.\d{2})\s*-\s*(\d{4}\.\d{2})",
    "sl_pattern": r"SL[:\s]+(\d{4}\.\d{2})",
    "tp_block_pattern": r"TP[:\s]+([\d\.\s]+)",
}

MESSAGE = ("XAUUSD BUY 4000.00 - 4002.00\n"
           "SL: 3990.00\n"
           "TP: 4010.00 4020.00 4030.00")


class TestACompleteRule:
    def test_it_parses_the_signal(self):
        out = lr.apply_learned_rule(GOOD_RULE, MESSAGE)

        assert out is not None
        assert out["direction"] == "BUY"
        assert out["entry_low"] == 4000.00
        assert out["entry_high"] == 4002.00
        assert out["stop_loss"] == 3990.00
        assert out["tp1"] == 4010.00

    def test_the_entry_bounds_are_ORDERED_not_positional(self):
        """A rule may capture the high first. Trusting capture order would
        invert the zone and make every breach check read backwards."""
        rule = dict(GOOD_RULE,
                    entry_pattern=r"(\d{4}\.\d{2})\s*-\s*(\d{4}\.\d{2})")
        out = lr.apply_learned_rule(rule, MESSAGE.replace(
            "4000.00 - 4002.00", "4002.00 - 4000.00"))

        assert out["entry_low"] == 4000.00
        assert out["entry_high"] == 4002.00

    def test_unfilled_target_slots_are_None_not_missing(self):
        """Callers index tp1..tp8 directly."""
        out = lr.apply_learned_rule(GOOD_RULE, MESSAGE)

        for i in range(1, 9):
            assert f"tp{i}" in out
        assert out["tp4"] is None

    def test_it_takes_at_most_EIGHT_targets(self):
        """The schema has eight columns. A ninth would be dropped silently by
        the database; better it is dropped here, deliberately."""
        many = MESSAGE.replace(
            "TP: 4010.00 4020.00 4030.00",
            "TP: " + " ".join(f"40{10 + i}.00" for i in range(12)))

        out = lr.apply_learned_rule(GOOD_RULE, many)

        assert out["tp8"] is not None
        assert len(out) >= 8


class TestAnIncompleteRuleParsesNOTHING:
    @pytest.mark.parametrize("missing", [
        "gate_pattern", "direction_pattern", "entry_pattern",
        "sl_pattern", "tp_block_pattern",
    ])
    def test_every_pattern_is_required(self, missing):
        """A dict with an entry and no stop is a trade with no protection.
        All five or nothing."""
        rule = dict(GOOD_RULE)
        rule[missing] = ""

        assert lr.apply_learned_rule(rule, MESSAGE) is None

    def test_a_gate_that_does_not_match_is_a_normal_no(self):
        """Most rules only apply to the one message shape they were derived
        from, so this is the common case, not an error."""
        rule = dict(GOOD_RULE, gate_pattern=r"EURUSD")
        assert lr.apply_learned_rule(rule, MESSAGE) is None

    def test_a_missing_stop_loss_in_the_TEXT_yields_nothing(self):
        assert lr.apply_learned_rule(
            GOOD_RULE, MESSAGE.replace("SL: 3990.00", "")) is None

    def test_no_targets_in_the_text_yields_nothing(self):
        assert lr.apply_learned_rule(
            GOOD_RULE, MESSAGE.replace("TP: 4010.00 4020.00 4030.00", "TP:")) is None


class TestAMalformedPatternIsRefusedNotRaised:
    def test_an_uncompilable_pattern_returns_None(self):
        """A model can emit anything. An exception here would take down the
        scan loop for every later message too."""
        rule = dict(GOOD_RULE, sl_pattern=r"SL: (\d+")     # unbalanced

        assert lr.apply_learned_rule(rule, MESSAGE) is None

    def test_an_OVERLONG_pattern_is_refused(self):
        """The 300-character cap. A pathological regex on attacker-shaped
        input is a denial of service on the message scanner."""
        rule = dict(GOOD_RULE, gate_pattern="a" * 301)

        assert lr.apply_learned_rule(rule, MESSAGE) is None
        assert lr._safe_compile_rule_pattern("a" * 301) is None
        assert lr._safe_compile_rule_pattern("a" * 300) is not None

    def test_an_empty_pattern_is_refused(self):
        assert lr._safe_compile_rule_pattern("") is None


class TestTheGroupCountsAreCHECKED:
    """The subtle failure. A pattern that compiles and matches but captures
    the wrong number of groups would otherwise read a nearby number as the
    stop."""

    def test_a_direction_pattern_with_two_groups_is_refused(self):
        rule = dict(GOOD_RULE, direction_pattern=r"\b(BUY|SELL)\b (\w+)")
        assert lr.apply_learned_rule(rule, MESSAGE) is None

    def test_an_entry_pattern_with_one_group_is_refused(self):
        """One group cannot describe a zone."""
        rule = dict(GOOD_RULE, entry_pattern=r"(\d{4}\.\d{2})")
        assert lr.apply_learned_rule(rule, MESSAGE) is None

    def test_an_sl_pattern_with_two_groups_is_refused(self):
        rule = dict(GOOD_RULE, sl_pattern=r"SL[:\s]+(\d{4})\.(\d{2})")
        assert lr.apply_learned_rule(rule, MESSAGE) is None

    def test_a_direction_that_is_not_buy_or_sell_is_refused(self):
        """The captured word goes straight into an order's direction."""
        rule = dict(GOOD_RULE, direction_pattern=r"\b(XAUUSD)\b")
        assert lr.apply_learned_rule(rule, MESSAGE) is None


class TestTryingEveryRuleForAChannel:
    @pytest.fixture
    def rules(self, monkeypatch):
        box = {"rows": []}
        from backend.src.db import database as _db
        monkeypatch.setattr(_db, "get_learned_parser_rules",
                            lambda ch: list(box["rows"]))
        return box

    def test_a_matching_rule_is_used(self, rules):
        rules["rows"] = [{"pattern": json.dumps(GOOD_RULE)}]

        out = lr.parse_with_learned_rules(MESSAGE, "Gold Diggers VIP")

        assert out is not None and out["direction"] == "BUY"

    def test_no_rules_means_no_parse(self, rules):
        assert lr.parse_with_learned_rules(MESSAGE, "Gold Diggers VIP") is None

    def test_a_CORRUPT_ROW_DOES_NOT_STOP_THE_REST(self, rules):
        """Rules are stored as JSON text. One bad row must not stop a good
        rule further down the list from being tried."""
        rules["rows"] = [{"pattern": "{not json"},
                         {"pattern": json.dumps(GOOD_RULE)}]

        out = lr.parse_with_learned_rules(MESSAGE, "Gold Diggers VIP")

        assert out is not None

    def test_a_null_pattern_row_is_skipped(self, rules):
        rules["rows"] = [{"pattern": None},
                         {"pattern": json.dumps(GOOD_RULE)}]

        assert lr.parse_with_learned_rules(MESSAGE, "Gold Diggers VIP") is not None

    def test_a_rule_yielding_NO_TP1_is_not_accepted(self, rules):
        """tp1 is what the caller executes against. A parse without one is
        not a usable signal, so the search continues rather than returning it."""
        no_tp = dict(GOOD_RULE, tp_block_pattern=r"(NOTHING_HERE)")
        rules["rows"] = [{"pattern": json.dumps(no_tp)}]

        assert lr.parse_with_learned_rules(MESSAGE, "Gold Diggers VIP") is None

    def test_the_FIRST_matching_rule_wins(self, rules):
        """Rows come back most-recent-first, so the newest approved rule takes
        precedence over an older one for the same shape."""
        older = dict(GOOD_RULE, direction_pattern=r"\b(SELL|BUY)\b")
        rules["rows"] = [{"pattern": json.dumps(GOOD_RULE)},
                         {"pattern": json.dumps(older)}]

        out = lr.parse_with_learned_rules(MESSAGE, "Gold Diggers VIP")

        assert out["direction"] == "BUY"


class TestSlAdjustmentRules:
    SL_RULE = {"gate_pattern": r"move sl|adjust sl",
               "sl_value_pattern": r"to\s+(\d{4}\.\d{2})"}

    def test_it_reads_the_new_stop(self):
        assert lr.apply_sl_adjustment_rule(
            self.SL_RULE, "Please move SL to 4005.50") == 4005.50

    def test_a_message_that_does_not_gate_is_ignored(self):
        """An entry signal must never be read as an SL adjustment -- that
        would move the stop on an unrelated open trade."""
        assert lr.apply_sl_adjustment_rule(self.SL_RULE, MESSAGE) is None

    def test_A_NUMBER_ALONE_DOES_NOT_MOVE_THE_STOP(self):
        """The gate is what makes this safe, and this is the case that proves
        it. "Take profit to 4005.50" matches the VALUE pattern perfectly and
        must still be refused, because it never said to move a stop.

        Found by mutation: the earlier test used a message that matched
        neither pattern, so deleting the gate check entirely still passed."""
        chatter = "Take profit to 4005.50 everyone, good trade"

        assert lr.apply_sl_adjustment_rule(self.SL_RULE, chatter) is None

    def test_a_gate_hit_with_no_value_yields_nothing(self):
        assert lr.apply_sl_adjustment_rule(
            self.SL_RULE, "Please move SL to breakeven") is None

    @pytest.mark.parametrize("field", ["gate_pattern", "sl_value_pattern"])
    def test_both_patterns_are_required(self, field):
        rule = dict(self.SL_RULE)
        rule[field] = ""
        assert lr.apply_sl_adjustment_rule(rule, "move SL to 4005.50") is None

    def test_a_two_group_value_pattern_is_refused(self):
        rule = dict(self.SL_RULE, sl_value_pattern=r"to\s+(\d{4})\.(\d{2})")
        assert lr.apply_sl_adjustment_rule(rule, "move SL to 4005.50") is None

    def test_an_uncompilable_pattern_is_refused(self):
        rule = dict(self.SL_RULE, sl_value_pattern=r"to (\d+")
        assert lr.apply_sl_adjustment_rule(rule, "move SL to 4005.50") is None

    def test_checking_a_channel_skips_corrupt_rows(self, monkeypatch):
        from backend.src.db import database as _db
        monkeypatch.setattr(
            _db, "get_learned_rules_by_type",
            lambda ch, t: [{"pattern": "{not json"},
                           {"pattern": json.dumps(self.SL_RULE)}])

        assert lr.check_sl_adjustment_rules(
            "move SL to 4005.50", "Gold Diggers VIP") == 4005.50

    def test_checking_a_channel_asks_for_the_RIGHT_RULE_TYPE(self, monkeypatch):
        """ai_derived_sl_adjust, not the parser rules. Running an entry-parse
        rule here would read a stop out of a message that never mentioned one."""
        seen = {}
        from backend.src.db import database as _db
        monkeypatch.setattr(_db, "get_learned_rules_by_type",
                            lambda ch, t: seen.setdefault("type", t) and [])

        lr.check_sl_adjustment_rules("move SL to 4005.50", "GD")

        assert seen["type"] == "ai_derived_sl_adjust"

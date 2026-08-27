"""A BUY/SELL phrase in Logic Keywords actually triggers a market entry.

Reported live 2026-08-27: GOLD DIGGERS INSTITUTIONAL sent "PREPARE FOR A
BUY", the phrase was in the BUY Orders box on Parsing > Logic Keywords, and
the app did nothing at all.

It could not have. `buy_orders` was never matched against anything as a
trigger -- it fed exactly one thing, an allow-gate in front of the AI
fallback (`should_skip_ai_fallback_for_no_signal_candidate`), so adding a
phrase only bought permission to spend an AI call. Direction detection
itself lived entirely in parser.py's per-format regexes, none of which have
ever heard of "PREPARE FOR A BUY". The box's own help text -- "phrases this
app's own parsers already treat as a BUY signal" -- was true of the shipped
defaults and false of anything the user typed into it.

There was also no SELL box at all, so the feature could only ever have
worked in one direction.

No order is placed in this file: the market-entry handler is replaced with
a recorder, so every assertion is "the trigger reached the market-entry
path", never "a trade was opened".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.signals import scan_messages as sm
from backend.src.services.telegram import keywords as lk
from backend.src.services.telegram import keyword_triggers as kt


class _FakeTgReader:
    def __init__(self, messages):
        self._messages = messages

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return "TestChannel"


def _run_scan(text, parser_fmt="gd2"):
    """Returns the (direction, price) handed to the market-entry path, or
    None if the message never reached it."""
    db.save_channel_parser_config("TestChannel", parser_fmt, "", True, True, "test")
    db.update_risk_settings({"immediate_market_entry": 1, "accept_tg_signals": 1})

    e = TradingRuntime.__new__(TradingRuntime)
    e._tg_reader = _FakeTgReader([{
        "id": "m1", "group_id": "g1", "text": text,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }])
    e._cfg = {}
    e._bridge = mock.Mock()
    e._dpm_candles = None
    e._tg_off_warn_state = {}

    fired = []

    async def _fake_instant(msg, tg_id, group_id, channel_name, txt, direction, price, *a, **k):
        fired.append((direction, price))

    with mock.patch.object(db, "should_generate_signals_here", return_value=True), \
         mock.patch.object(sm, "_process_instant_entry_impl", _fake_instant):
        asyncio.run(e._scan_messages())
    return fired[0] if fired else None


EVERY_FORMAT = pytest.mark.parametrize("parser_fmt", ["format_ab", "gd2", "auto"])


# ── The reported bug ─────────────────────────────────────────────────────────

@EVERY_FORMAT
def test_a_phrase_added_to_the_buy_box_triggers_a_market_entry(fresh_db, parser_fmt):
    lk.set_lexicon("buy_orders", lk.DEFAULT_LEXICONS["buy_orders"] + ["PREPARE FOR A BUY"])
    assert _run_scan("PREPARE FOR A BUY", parser_fmt) == ("BUY", None)


@EVERY_FORMAT
def test_the_sell_box_exists_and_works_the_same_way(fresh_db, parser_fmt):
    """Without this the feature is BUY-only -- "PREPARE FOR A SELL" would
    still silently do nothing."""
    lk.set_lexicon("sell_orders", lk.DEFAULT_LEXICONS["sell_orders"] + ["PREPARE FOR A SELL"])
    assert _run_scan("PREPARE FOR A SELL", parser_fmt) == ("SELL", None)


def test_a_phrase_removed_from_the_box_stops_triggering(fresh_db):
    """The box has to be the control, in both directions."""
    lk.set_lexicon("buy_orders", ["PREPARE FOR A BUY"])
    assert _run_scan("PREPARE FOR A BUY") == ("BUY", None)
    lk.set_lexicon("buy_orders", ["BUY NOW"])
    assert _run_scan("PREPARE FOR A BUY") is None


def test_the_match_is_case_and_decoration_insensitive(fresh_db):
    lk.set_lexicon("buy_orders", ["PREPARE FOR A BUY"])
    assert _run_scan("**🔥 Prepare for a Buy! 🔥**") == ("BUY", None)


def test_the_trigger_line_may_sit_among_other_lines(fresh_db):
    """A real heads-up is rarely one bare line."""
    lk.set_lexicon("buy_orders", ["PREPARE FOR A BUY"])
    assert _run_scan("Good morning all\n\nPREPARE FOR A BUY\n\nLevels shortly") == ("BUY", None)


# ── What must NOT fire ───────────────────────────────────────────────────────
#
# The shipped default list contains the bare word "BUY". Matched as a
# substring -- the way every other Logic Keywords lexicon is matched -- that
# would open a market order on any message mentioning buying at all. The
# match is per-line and exact for precisely this reason.

def test_a_sentence_merely_containing_a_phrase_does_not_fire(fresh_db):
    lk.set_lexicon("buy_orders", ["BUY"])
    assert _run_scan("we are watching for a buy setup later, do not enter yet") is None


def test_a_message_carrying_numbers_is_left_to_the_real_parsers(fresh_db):
    """Anything with a level in it is a signal or a fragment of one. Firing a
    market order off its heads-up line would ignore the levels it states."""
    lk.set_lexicon("buy_orders", ["PREPARE FOR A BUY"])
    assert _run_scan("PREPARE FOR A BUY\n4163.5 - 4158.5\nSL 4155") is None


def test_a_message_naming_both_directions_is_refused(fresh_db):
    lk.set_lexicon("buy_orders", ["BUY"])
    lk.set_lexicon("sell_orders", ["SELL"])
    assert _run_scan("BUY\nSELL") is None


def test_nothing_fires_when_immediate_market_entry_is_off(fresh_db):
    lk.set_lexicon("buy_orders", ["PREPARE FOR A BUY"])
    db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "test")
    db.update_risk_settings({"immediate_market_entry": 0, "accept_tg_signals": 1})

    e = TradingRuntime.__new__(TradingRuntime)
    e._tg_reader = _FakeTgReader([{
        "id": "m1", "group_id": "g1", "text": "PREPARE FOR A BUY",
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }])
    e._cfg = {}
    e._bridge = mock.Mock()
    e._dpm_candles = None
    e._tg_off_warn_state = {}
    fired = []

    async def _fake_instant(*a, **k):
        fired.append(a)

    with mock.patch.object(db, "should_generate_signals_here", return_value=True), \
         mock.patch.object(sm, "_process_instant_entry_impl", _fake_instant):
        asyncio.run(e._scan_messages())
    assert fired == []


# ── The parser itself ────────────────────────────────────────────────────────

def test_an_empty_pair_of_boxes_disables_the_trigger_rather_than_matching_everything(fresh_db):
    lk.set_lexicon("buy_orders", [])
    lk.set_lexicon("sell_orders", [])
    assert kt.parse_lexicon_direction_trigger("PREPARE FOR A BUY") is None
    assert kt.parse_lexicon_direction_trigger("") is None


def test_the_ai_fallback_gate_also_sees_the_sell_box(fresh_db):
    """The gate combines symbol/buy/limit phrases. A sell-only heads-up must
    not be pruned before the AI fallback just because the sell box is the one
    it matches."""
    lk.set_lexicon("symbol_tokens", [])
    lk.set_lexicon("buy_orders", [])
    lk.set_lexicon("limit_orders", [])
    lk.set_lexicon("sell_orders", ["PREPARE FOR A SELL"])
    assert kt.should_skip_ai_fallback_for_no_signal_candidate(
        "PREPARE FOR A SELL", {}) is None

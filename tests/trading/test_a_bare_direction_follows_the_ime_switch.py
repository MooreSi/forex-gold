"""bugs/015, the behaviour question (owner 2026-10-11): a direction-only
message ("XAU USD SELL", "Sell Gold Now") executes only when Immediate Market
Entry is on; otherwise the app waits for the follow-up signal and trades that.

This supersedes the 2026-09-07 plan in docs/simon-handover/027 to also require
a bound EA template: that gate was never built and is no longer wanted. The
switch alone decides, for every Telegram channel the app knows.

The decision point is `ime_enabled_for_channel`; scan_messages only offers a
message to the instant-entry parsers when it returns True, and otherwise the
bare direction falls through to classify_and_parse, which records nothing
and returns nothing (see test_bare_direction_rescan_work.py).

No broker is involved.
"""
from backend.src.db import database as db
from backend.src.services.trading.scan_auto_execute import ime_enabled_for_channel


def _known(name):
    db.save_channel_parser_config(name, "gd2", "", True, True, "test")


def test_off_means_a_bare_direction_does_not_execute(fresh_db):
    _known("Gold Diggers Scalping")
    assert ime_enabled_for_channel({"immediate_market_entry": 0}, "Gold Diggers Scalping") is False


def test_on_means_it_executes_on_any_known_channel(fresh_db):
    _known("Gold Diggers Scalping")
    assert ime_enabled_for_channel({"immediate_market_entry": 1}, "Gold Diggers Scalping") is True


def test_on_does_not_need_a_bound_template(fresh_db):
    """The 2026-09-07 template-binding gate is not wanted. A channel with no
    strategy override at all still enters on a bare direction when IME is on."""
    _known("A Channel With No Template")
    assert ime_enabled_for_channel({"immediate_market_entry": 1}, "A Channel With No Template") is True


def test_a_non_telegram_source_never_counts(fresh_db):
    assert ime_enabled_for_channel({"immediate_market_entry": 1}, "Reversal Engine") is False

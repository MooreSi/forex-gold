"""Parsing Settings features added 2026-07-31: Reverse/Mirror Copy, the
generic partial/follow-up parsers behind TP/SL in Second Message, and the
hold/merge/expire state machine itself.

Real-money surface: none of these place, close, or modify an MT5 order --
apply_mirror_copy rewrites a parsed dict in memory, the parsers are pure
functions, and hold_or_resolve only reads/writes vantage_second_message_holds.
The execution paths they feed are covered by their own existing suites.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.services.telegram.keyword_triggers import apply_mirror_copy
from backend.src.services.positions.core_second_message_merge import (
    attach_followup, hold_or_resolve, match_window_sec,
)
from backend.src.services.signals.parser import (
    parse_format_ab_partial, parse_gd2_signal, parse_gold_signal,
    parse_partial_any_format, parse_tp_sl_only,
)


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    os.remove(path)


# ── Reverse / Mirror Copy ────────────────────────────────────────────────

_MIRROR_ON = {"lk_enable_mirror_copy": 1}


def _buy_signal():
    return {"direction": "BUY", "entry_low": 4150.0, "entry_high": 4160.0,
            "stop_loss": 4140.0, "tp1": 4170.0, "tp2": 4180.0, "tp3": None}


def test_mirror_inverts_direction_and_reflects_levels():
    sig = _buy_signal()
    assert apply_mirror_copy(sig, _MIRROR_ON) is not None
    assert sig["direction"] == "SELL"
    # pivot is the zone midpoint 4155: SL 4140 is 15 below it, so it lands 15 above.
    assert sig["stop_loss"] == 4170.0
    assert sig["tp1"] == 4140.0
    assert sig["tp2"] == 4130.0
    assert sig["tp3"] is None


def test_mirror_keeps_the_entry_zone_and_produces_valid_sell_geometry():
    """The mirrored trade must still be executable: a SELL needs its stop
    above the zone and every target below it. A mirror that broke this would
    place a trade the broker rejects, or worse, one with an inverted stop."""
    sig = _buy_signal()
    apply_mirror_copy(sig, _MIRROR_ON)
    assert (sig["entry_low"], sig["entry_high"]) == (4150.0, 4160.0)
    assert sig["stop_loss"] > sig["entry_high"]
    assert sig["tp1"] < sig["entry_low"]
    assert sig["tp2"] < sig["tp1"]


def test_mirror_is_an_involution():
    sig = _buy_signal()
    original = dict(sig)
    apply_mirror_copy(sig, _MIRROR_ON)
    apply_mirror_copy(sig, _MIRROR_ON)
    assert sig == original


def test_mirror_off_by_default_leaves_signal_untouched():
    sig = _buy_signal()
    original = dict(sig)
    assert apply_mirror_copy(sig, {}) is None
    assert sig == original


def test_mirror_declines_a_signal_with_no_entry_zone():
    sig = {"direction": "BUY", "entry_low": None, "entry_high": None, "stop_loss": 1.0}
    assert apply_mirror_copy(sig, _MIRROR_ON) is None
    assert sig["direction"] == "BUY"


def test_mirror_does_not_touch_limit_runner_routing():
    """tp_open is what routes a signal to a resting pending order rather than
    market execution; mirroring changes direction, not order placement."""
    sig = {**_buy_signal(), "tp_open": True}
    apply_mirror_copy(sig, _MIRROR_ON)
    assert sig["tp_open"] is True


# ── Partial / follow-up parsers ──────────────────────────────────────────

_BARE_GD2 = "XAU USD SELL NOW\n4150.5 - 4155.5"
_BARE_AB = "Direction: BUY\nENTRY: 4150 - 4155"
_FULL_GD2 = "XAU USD SELL NOW\n4150.5 - 4155.5\nSL 4160\nTP1 4145\nTP2 4140"


def test_partial_detects_bare_entry_in_both_formats():
    gd2 = parse_partial_any_format(_BARE_GD2)
    assert (gd2["direction"], gd2["entry_low"], gd2["entry_high"]) == ("SELL", 4150.5, 4155.5)
    assert gd2["stop_loss"] is None and gd2["tp1"] is None

    ab = parse_partial_any_format(_BARE_AB)
    assert (ab["direction"], ab["entry_low"], ab["entry_high"]) == ("BUY", 4150.0, 4155.0)


def test_a_complete_signal_is_never_treated_as_partial():
    """The hold path must not intercept signals that are already executable --
    doing so would delay every normal signal by the match window."""
    assert parse_partial_any_format(_FULL_GD2) is None
    assert parse_gd2_signal(_FULL_GD2) is not None


def test_format_ab_partial_rejects_a_message_that_states_levels():
    assert parse_format_ab_partial(
        "Direction: BUY\nENTRY: 4150 - 4155\nStop Loss: 4140"
    ) is None
    assert parse_format_ab_partial(
        "Direction: BUY\nENTRY: 4150 - 4155\nTP1 4170"
    ) is None


def test_format_ab_partial_rejects_non_xauusd():
    assert parse_format_ab_partial(
        "Currency: EURUSD\nDirection: BUY\nENTRY: 1.05 - 1.06"
    ) is None


def test_partial_carries_through_a_stop_loss_the_bare_message_did_state():
    """parse_gd2_partial treats "SL but no TP" as partial. That SL must
    survive the hold -- executing bare later with the stop silently dropped
    would place an unprotected trade."""
    partial = parse_partial_any_format("XAU USD SELL NOW\n4150.5 - 4155.5\nSL 4160")
    assert partial is not None
    assert partial["stop_loss"] == 4160.0


def test_follow_up_parses_levels_only():
    fu = parse_tp_sl_only("SL 4160\nTP1 4145\nTP2 4140")
    assert fu["stop_loss"] == 4160.0
    assert (fu["tp1"], fu["tp2"], fu["tp3"]) == (4145.0, 4140.0, None)


def test_follow_up_refuses_anything_naming_a_direction():
    """A message with a direction is a standalone signal and must go through
    the normal parsers, not be swallowed as another signal's follow-up."""
    assert parse_tp_sl_only(_FULL_GD2) is None
    assert parse_tp_sl_only("Direction: BUY\nENTRY: 4150 - 4155\nSL 4140") is None


def test_follow_up_refuses_a_message_with_no_levels():
    assert parse_tp_sl_only("good luck everyone") is None
    assert parse_tp_sl_only("") is None


# ── Hold / merge / expire ────────────────────────────────────────────────

_ON = {"lk_enable_second_message_tp_sl": 1, "lk_second_message_match_window_sec": 120}


def test_first_sighting_holds_and_does_not_execute(fresh_db):
    partial = parse_partial_any_format(_BARE_GD2)
    assert hold_or_resolve("tg1", "Chan", partial, _ON) is None


def test_follow_up_completes_the_held_signal(fresh_db):
    partial = parse_partial_any_format(_BARE_GD2)
    hold_or_resolve("tg1", "Chan", partial, _ON)

    assert attach_followup("Chan", parse_tp_sl_only("SL 4160\nTP1 4145\nTP2 4140")) == "tg1"

    merged = hold_or_resolve("tg1", "Chan", partial, _ON)
    assert merged is not None
    assert merged["direction"] == "SELL"
    assert (merged["entry_low"], merged["entry_high"]) == (4150.5, 4155.5)
    assert merged["stop_loss"] == 4160.0
    assert (merged["tp1"], merged["tp2"]) == (4145.0, 4140.0)


def test_a_resolved_hold_does_not_fire_twice(fresh_db):
    """Re-scanning the same buffered message after it resolved must not
    re-emit the signal -- that would place the trade a second time."""
    partial = parse_partial_any_format(_BARE_GD2)
    hold_or_resolve("tg1", "Chan", partial, _ON)
    attach_followup("Chan", parse_tp_sl_only("SL 4160\nTP1 4145"))
    assert hold_or_resolve("tg1", "Chan", partial, _ON) is not None
    # Second re-scan starts a fresh hold rather than re-emitting the old one.
    assert hold_or_resolve("tg1", "Chan", partial, _ON) is None


def test_expired_window_executes_bare(fresh_db):
    partial = parse_partial_any_format(_BARE_GD2)
    rs = {"lk_enable_second_message_tp_sl": 1, "lk_second_message_match_window_sec": 1}
    hold_or_resolve("tg1", "Chan", partial, rs)

    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_second_message_holds SET first_seen_at=? WHERE tg_message_id=?",
            (time.time() - 5, "tg1"),
        )

    bare = hold_or_resolve("tg1", "Chan", partial, rs)
    assert bare is not None
    assert bare["direction"] == "SELL"
    assert bare["stop_loss"] is None
    assert bare["tp1"] is None


def test_follow_up_with_nothing_waiting_is_not_consumed(fresh_db):
    assert attach_followup("Chan", parse_tp_sl_only("SL 4160\nTP1 4145")) is None


def test_follow_up_completes_only_the_newest_hold_on_that_channel(fresh_db):
    """Two bare entries back to back: a single follow-up belongs to the most
    recent one, not to both."""
    partial = parse_partial_any_format(_BARE_GD2)
    hold_or_resolve("tg_old", "Chan", partial, _ON)
    time.sleep(0.01)
    hold_or_resolve("tg_new", "Chan", partial, _ON)

    assert attach_followup("Chan", parse_tp_sl_only("SL 4160\nTP1 4145")) == "tg_new"
    assert hold_or_resolve("tg_old", "Chan", partial, _ON) is None


def test_follow_up_does_not_cross_channels(fresh_db):
    partial = parse_partial_any_format(_BARE_GD2)
    hold_or_resolve("tg1", "ChanA", partial, _ON)
    assert attach_followup("ChanB", parse_tp_sl_only("SL 4160\nTP1 4145")) is None


def test_zero_match_window_does_not_expire_every_hold_instantly():
    """A 0 saved into the settings row would otherwise make the feature a
    silent no-op that still reads as enabled in the UI."""
    assert match_window_sec({"lk_second_message_match_window_sec": 0}) == 1
    assert match_window_sec({"lk_second_message_match_window_sec": "bad"}) == 120
    assert match_window_sec({}) == 120

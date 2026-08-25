"""REF confirmation gate: only live-execute a Reversal Engine signal when the
professional channels have just posted a matching entry.

No MT5 order is placed, modified or closed anywhere here -- the gate is a
read-only lookup against vantage_tg_signals and returns a bool.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.services.reversal_engine import ref_confirmation as rc


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
    db.save_channel_parser_config("Pro Channel", "gd2", "", False, True, "")
    yield db
    _reset_thread_local_connection()
    db._db_executor.submit(_reset_thread_local_connection).result()
    os.remove(path)


NOW = 1_785_500_000.0
ON = {"re_require_ref_confirmation": 1, "re_ref_confirmation_window_min": 60}


def _add_ref(direction="BUY", low=4050.0, high=4056.0, at=NOW,
             channel="Pro Channel", tg_id="ref1"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals "
            "(tg_message_id, group_id, group_name, sender_name, message_ts, raw_text, "
            " parsed_at, direction, entry_low, entry_high, status) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (tg_id, "g1", channel, "", "", "x", at, direction, low, high, "historical"),
        )


# ── gate off ─────────────────────────────────────────────────────────────

def test_gate_off_allows_everything(fresh_db):
    """Default state. Must not change behaviour for anyone who hasn't opted in,
    including when no REF signal exists at all."""
    ok, reason = rc.check("BUY", 4050.0, 4056.0, {}, at_ts=NOW)
    assert ok is True
    assert reason == ""


def test_gate_off_allows_even_a_signal_with_no_entry_zone(fresh_db):
    assert rc.check("BUY", None, None, {"re_require_ref_confirmation": 0})[0] is True


# ── confirmation found ───────────────────────────────────────────────────

def test_matching_recent_ref_confirms(fresh_db):
    _add_ref(at=NOW - 600)  # 10 min ago
    ok, reason = rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)
    assert ok is True
    assert "Pro Channel" in reason and "10min ago" in reason


def test_confirmation_allows_price_within_tolerance(fresh_db):
    # REF mid 4053.0, ours 4055.5 -> 2.5pts apart, inside the 3.0 tolerance.
    _add_ref(low=4050.0, high=4056.0, at=NOW - 60)
    assert rc.check("BUY", 4053.0, 4058.0, ON, at_ts=NOW)[0] is True


# ── confirmation refused ─────────────────────────────────────────────────

def test_no_ref_at_all_blocks(fresh_db):
    ok, reason = rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)
    assert ok is False
    assert "no matching BUY signal" in reason


def test_opposite_direction_does_not_confirm(fresh_db):
    _add_ref(direction="SELL", at=NOW - 60)
    assert rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)[0] is False


def test_ref_too_far_in_price_does_not_confirm(fresh_db):
    # REF mid 4053.0 vs ours 4063.0 -> 10pts, well outside tolerance.
    _add_ref(low=4050.0, high=4056.0, at=NOW - 60)
    assert rc.check("BUY", 4060.0, 4066.0, ON, at_ts=NOW)[0] is False


def test_ref_older_than_the_window_does_not_confirm(fresh_db):
    """The measured edge decays fast: a two-hour-old match at a similar price
    is coincidence, and that bucket performs no better than trading everything."""
    _add_ref(at=NOW - 3 * 3600)
    assert rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)[0] is False


def test_a_future_ref_signal_cannot_confirm(fresh_db):
    """Guards the backtest as much as the live path: counting a REF signal
    posted after the decision moment would make any measurement built on this
    function silently optimistic."""
    _add_ref(at=NOW + 300)
    assert rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)[0] is False


def test_signal_without_entry_zone_is_blocked_when_gate_is_on(fresh_db):
    _add_ref(at=NOW - 60)
    ok, reason = rc.check("BUY", None, None, ON, at_ts=NOW)
    assert ok is False
    assert "no entry zone" in reason


def test_disabled_channel_cannot_confirm(fresh_db):
    """A channel switched off in Parsing Settings must not be able to
    greenlight a live trade."""
    db.save_channel_parser_config("Muted Channel", "gd2", "", False, False, "")
    _add_ref(channel="Muted Channel", at=NOW - 60)
    assert rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)[0] is False


def test_unknown_channel_cannot_confirm(fresh_db):
    _add_ref(channel="Some Random Group", at=NOW - 60)
    assert rc.check("BUY", 4050.0, 4056.0, ON, at_ts=NOW)[0] is False


# ── window configuration ─────────────────────────────────────────────────

def test_window_setting_is_honoured(fresh_db):
    _add_ref(at=NOW - 20 * 60)  # 20 min ago
    tight = {"re_require_ref_confirmation": 1, "re_ref_confirmation_window_min": 15}
    wide = {"re_require_ref_confirmation": 1, "re_ref_confirmation_window_min": 60}
    assert rc.check("BUY", 4050.0, 4056.0, tight, at_ts=NOW)[0] is False
    assert rc.check("BUY", 4050.0, 4056.0, wide, at_ts=NOW)[0] is True


def test_window_falls_back_on_a_bad_value():
    assert rc.confirmation_window_s({"re_ref_confirmation_window_min": "nonsense"}) == 60 * 60
    assert rc.confirmation_window_s({}) == 60 * 60


def test_window_has_a_floor_so_zero_does_not_block_everything():
    """A 0 saved into settings would otherwise make the gate unsatisfiable
    while still reading as enabled."""
    assert rc.confirmation_window_s({"re_ref_confirmation_window_min": 0}) == 60

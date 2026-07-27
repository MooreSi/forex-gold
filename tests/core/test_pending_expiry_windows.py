"""Pending-signal expiry windows (core_pending_signal_activation).

Covers the 2026-07-28 fixes:
  * the 15-min GD2 window was gated on a hardcoded PRE-RENAME channel name
    ("gold diggers 2.0"), so it silently became dead code when that group's
    Telegram title changed to "GOLD DIGGERS INSTITUTIONAL" and every one of
    its zone signals dropped to the 120s default;
  * EA-Template-assigned channels had no window of their own at all, so once
    the "High Risk" dispatch fix stopped diverting their Limit-format signals
    to Limit Runner, they landed on that same 120s default and expired
    unfilled every time.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_pending_signal_activation as psa
from forex_trader.core import core_ea_templates as et


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


# ── _channel_parser_format: rename-proof channel resolution ──────────────

def test_parser_format_resolves_current_channel_name(fresh_db):
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "gd2", "", True, True, "t")
    assert psa._channel_parser_format("GOLD DIGGERS INSTITUTIONAL") == "gd2"


def test_parser_format_strips_the_telegram_auto_wrapper(fresh_db):
    # Stored signals carry the decorated form, which is what the old
    # substring test was matching against.
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "gd2", "", True, True, "t")
    assert psa._channel_parser_format(
        "Telegram Auto (GOLD DIGGERS INSTITUTIONAL)") == "gd2"


def test_parser_format_survives_the_rename_via_canonical_map(fresh_db):
    # The dead pre-rename name must still resolve to the live channel's row.
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "gd2", "", True, True, "t")
    assert psa._channel_parser_format("Gold Diggers 2.0") == "gd2"


def test_parser_format_does_not_match_an_unrelated_channel(fresh_db):
    db.save_channel_parser_config("Gold Diggers VIP", "format_ab", "", True, True, "t")
    assert psa._channel_parser_format("Telegram Auto (Gold Diggers VIP)") == "format_ab"


def test_parser_format_unknown_source_is_empty(fresh_db):
    assert psa._channel_parser_format("Reversal Engine") == ""
    assert psa._channel_parser_format("") == ""
    assert psa._channel_parser_format(None) == ""


# ── the constants the expiry branches select between ─────────────────────

def test_template_window_matches_limit_runner_ttl(fresh_db):
    # Deliberately the same 60min a resting Limit Runner order gets, so the
    # dispatch fix is a dispatch change only, not a timing change.
    from forex_trader.core import core_limit_order_signal as clos
    assert psa._TEMPLATE_PENDING_EXPIRY_SEC == clos._DEFAULT_EXPIRE_MINUTES * 60
    assert psa._TEMPLATE_PENDING_EXPIRY_SEC == 3600


def test_template_window_is_longer_than_the_default(fresh_db):
    assert psa._TEMPLATE_PENDING_EXPIRY_SEC > psa._EXPIRY


def test_a_template_override_is_recognised_as_such(fresh_db):
    # The expiry branch keys off this; if it ever stopped matching, templates
    # would silently fall back to the 120s default again.
    et.save_ea_template("T1", {"mode": "grid"})
    override = et.override_for_template("T1")
    assert et.is_template_override(override)
    assert not et.is_template_override("limit_runner")

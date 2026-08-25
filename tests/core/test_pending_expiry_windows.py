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

from backend.src.db import database as db
from backend.src.services.signals import pending_activation as psa
from backend.src.services.broker import ea_templates as et


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
    from backend.src.services.trading import limit_order_signal as clos
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


# ── IME-off window (2026-08-06) ──────────────────────────────────────────
# With Immediate Market Entry ON, a signal is taken at market the moment it
# lands and never reaches this queue. With it OFF, waiting for price to come
# back to the zone IS the behaviour, and 120s was too short for a normal
# retracement -- these cover the 3-minute window that replaces it, and the
# guarantee that it never shortens one of the longer windows above.

def _sig(signal_id="s1", source="Telegram Auto (Some Channel)"):
    return {"signal_id": signal_id, "source_name": source}


def _ime(on: bool) -> dict:
    return {"immediate_market_entry": 1 if on else 0,
            "trade_strategy": "scale_out"}


def _insert_pending_order(signal_id: str, status: str = "working"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_pending_orders "
            "(trade_id,signal_id,tg_message_id,channel_name,direction,price,"
            " stop_loss,tps_json,pcts_json,be_at_pos,tp_open,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t-" + signal_id, signal_id, "1", "Some Channel", "BUY", 4000.0,
             3990.0, "{}", "[]", 1, 0, 0.01, status, 0.0),
        )


def test_ime_off_widens_the_default_window_to_three_minutes(fresh_db):
    db.save_channel_parser_config("Some Channel", "auto", "", False, True, "t")
    assert psa._resolve_expiry_sec(_sig(), _ime(False), "scale_out") == 180


def test_ime_on_keeps_the_original_two_minute_default(fresh_db):
    # Per-channel flag on AND global on -- the signal would normally have been
    # taken at market, so nothing about its queued lifetime should change.
    db.save_channel_parser_config("Some Channel", "auto", "", True, True, "t")
    assert psa._resolve_expiry_sec(_sig(), _ime(True), "scale_out") == psa._EXPIRY


def test_global_ime_on_but_channel_flag_off_still_gets_three_minutes(fresh_db):
    # IME is only live when BOTH toggles agree, so this channel is IME-off.
    db.save_channel_parser_config("Some Channel", "auto", "", False, True, "t")
    assert psa._resolve_expiry_sec(_sig(), _ime(True), "scale_out") == 180


def test_limit_format_signals_are_excluded(fresh_db):
    # Their resting broker order is the wait, on its own 60min TTL.
    db.save_channel_parser_config("Some Channel", "auto", "", False, True, "t")
    _insert_pending_order("s1")
    assert psa._resolve_expiry_sec(_sig(), _ime(False), "scale_out") == psa._EXPIRY


def test_limit_signal_exclusion_survives_the_order_being_cancelled(fresh_db):
    # Tested by existence, not status -- what matters is the KIND of signal.
    db.save_channel_parser_config("Some Channel", "auto", "", False, True, "t")
    _insert_pending_order("s1", status="cancelled")
    assert psa._resolve_expiry_sec(_sig(), _ime(False), "scale_out") == psa._EXPIRY


def test_ime_off_never_shortens_the_template_window(fresh_db):
    db.save_channel_parser_config("Some Channel", "auto", "", False, True, "t")
    et.save_ea_template("T2", {"mode": "grid"})
    override = et.override_for_template("T2")
    assert psa._resolve_expiry_sec(_sig(), _ime(False), override) == 3600


def test_ime_off_never_shortens_the_runner_window(fresh_db):
    db.save_channel_parser_config("Some Channel", "auto", "", False, True, "t")
    assert psa._resolve_expiry_sec(
        _sig(), _ime(False), "reversal_runner") == psa._GDVR_PENDING_EXPIRY_SEC


def test_ime_off_never_shortens_the_gd2_window(fresh_db):
    db.save_channel_parser_config("GOLD DIGGERS INSTITUTIONAL", "gd2", "", False, True, "t")
    sig = _sig(source="Telegram Auto (GOLD DIGGERS INSTITUTIONAL)")
    assert psa._resolve_expiry_sec(sig, _ime(False), "scale_out") == 15 * 60


def test_ime_off_never_shortens_the_orb_window(fresh_db):
    sig = _sig(source="ORB/IVB Report (auto)")
    assert psa._resolve_expiry_sec(sig, _ime(False), "scale_out") == 60 * 60


def test_the_new_window_is_longer_than_the_default_but_still_short(fresh_db):
    assert psa._IME_OFF_EXPIRY == 180
    assert psa._EXPIRY < psa._IME_OFF_EXPIRY < 15 * 60

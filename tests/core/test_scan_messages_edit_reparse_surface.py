"""Proves backend.src.services.signals.scan_edit_reparse.handle_signal_edit
behaves identically to SimulationEngine's original, characterized in
test_scan_messages_edit_reparse_characterization.py -- see
docs/todo/refactor/core-scan-messages-edit-reparse-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. The instant-entry-flip-flatten path can close a real open position
via close_trade_fn -- always faked here.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from forex_trader.core import database as db
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.signals import scan_edit_reparse as ser


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


def _insert_tg_signal(tg_id, group_id="g1", channel_name="TestChannel", raw_text="old text",
                      direction="BUY", entry_low=2400.0, entry_high=2405.0, stop_loss=2390.0,
                      tp1=2410.0, status="new"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id, group_id, group_name, sender_name, "
            "message_ts, raw_text, parsed_at, direction, entry_low, entry_high, stop_loss, tp1, "
            "status) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (tg_id, group_id, channel_name, "sender", "", raw_text, time.time(), direction,
             entry_low, entry_high, stop_loss, tp1, status),
        )


def _insert_open_trade(trade_id, tg_source, direction="BUY"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", direction, 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, entry_low, "
            "entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, open_time, "
            "tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", direction, 2400.0, 2400.0, 2400.0, 0.10, 0.10,
             2390.0, "open", time.time(), tg_source),
        )


def _get_tg_row(tg_id):
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)).fetchone()
        )


def _existing_for(tg_id):
    with db.db() as conn:
        row = conn.execute(
            "SELECT id, direction, status, raw_text, entry_low FROM vantage_tg_signals "
            "WHERE tg_message_id=?", (tg_id,)
        ).fetchone()
    return db.row_to_dict(row)


_GD2_BUY = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"
_GD2_SELL = "XAU USD SELL NOW\n4520-4525\nTP1 4510\nSL 4530"
_GD2_BARE_SELL = "XAU USD SELL NOW"

_AI_SAME_DIR = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
                "tp1": 4537.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
                "tp6": None, "tp7": None, "tp8": None}
_AI_FLIP = {"direction": "SELL", "entry_low": 4520.0, "entry_high": 4525.0, "stop_loss": 4530.0,
            "tp1": 4510.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
            "tp6": None, "tp7": None, "tp8": None}


def _call(tg_id, text, parser_fmt="gd2", channel_name="TestChannel", group_id="g1",
          ai_fallback_return=None, close_trade_fake=None, followup_fake=None):
    existing = _existing_for(tg_id)
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    async def fake_ai(text_, channel_name_, tg_id_):
        return ai_fallback_return

    async def fake_followup(channel_name_, direction_, parsed_, tg_id_):
        return False

    async def default_close(trade_id, reason):
        return {"trade_id": trade_id}

    with mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send):
        result = asyncio.run(ser.handle_signal_edit(
            tg_id, group_id, channel_name, text, parser_fmt, existing,
            fake_ai, followup_fake or fake_followup, close_trade_fake or default_close,
        ))
    return result, alerts


def test_text_unchanged_dedup_skip(fresh_db):
    _insert_tg_signal("tg1", raw_text="SAME TEXT", status="new")
    result, alerts = _call("tg1", "SAME TEXT")
    assert result is None
    assert alerts == []


def test_full_reparse_same_direction_updates_fields_and_calls_followup(fresh_db):
    _insert_tg_signal("tg2", raw_text="old", direction="BUY", status="new")
    followup_calls = []

    async def fake_followup(channel_name, direction, parsed, tg_id):
        followup_calls.append((channel_name, direction, tg_id))
        return False

    _call("tg2", _GD2_BUY, followup_fake=fake_followup)
    row = _get_tg_row("tg2")
    assert row["entry_low"] == 4529.0
    assert followup_calls == [("TestChannel", "BUY", "tg2")]


def test_full_reparse_same_direction_pending_followup_promotes(fresh_db):
    _insert_tg_signal("tg3", raw_text="old", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="pending_followup")
    result, alerts = _call("tg3", _GD2_BUY)
    row = _get_tg_row("tg3")
    assert row["status"] == "new"
    assert row["entry_low"] == 4529.0
    assert result is not None
    assert result["direction"] == "BUY"


def test_unparseable_edit_no_instant_status_ai_fails_dropped(fresh_db):
    _insert_tg_signal("tg4", raw_text="old", direction="BUY", status="new")
    result, alerts = _call("tg4", "random commentary not a signal edit", ai_fallback_return=None)
    assert result is None
    row = _get_tg_row("tg4")
    assert row["raw_text"] == "old"


def test_direction_flip_status_new_corrects_in_place(fresh_db):
    _insert_tg_signal("tg5", raw_text="old", direction="BUY", status="new")
    result, alerts = _call("tg5", _GD2_SELL)
    row = _get_tg_row("tg5")
    assert row["direction"] == "SELL"
    assert result is None
    assert len(alerts) == 1
    assert "SIGNAL CORRECTED via edit" in alerts[0]


def test_direction_flip_already_executed_warns_only(fresh_db):
    _insert_tg_signal("tg6", raw_text="old", direction="BUY", status="activated")
    result, alerts = _call("tg6", _GD2_SELL)
    row = _get_tg_row("tg6")
    assert row["direction"] == "BUY"
    assert row["raw_text"] == _GD2_SELL
    assert len(alerts) == 1
    assert "SIGNAL EDIT WARNING" in alerts[0]


def test_instant_flip_flatten_matching_trade_closes_it(fresh_db):
    _insert_tg_signal("tg7", raw_text="old bare", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    _insert_open_trade("trade-abc", tg_source="TestChannel", direction="BUY")
    close_calls = []

    async def fake_close(trade_id, reason):
        close_calls.append((trade_id, reason))
        return {"trade_id": trade_id}

    result, alerts = _call("tg7", _GD2_BARE_SELL, close_trade_fake=fake_close)
    assert close_calls == [("trade-abc", "instant_edit_flip:BUY->SELL")]
    row = _get_tg_row("tg7")
    assert row["direction"] == "SELL"
    assert "closed trade" in alerts[0]


def test_instant_flip_flatten_no_matching_trade(fresh_db):
    _insert_tg_signal("tg8", raw_text="old bare", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    result, alerts = _call("tg8", _GD2_BARE_SELL)
    assert "no matching open trade found" in alerts[0]


def test_instant_flip_flatten_close_trade_raises(fresh_db):
    _insert_tg_signal("tg9", raw_text="old bare", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    _insert_open_trade("trade-xyz", tg_source="TestChannel", direction="BUY")

    async def raising_close(trade_id, reason):
        raise RuntimeError("mt5 close failed")

    result, alerts = _call("tg9", _GD2_BARE_SELL, close_trade_fake=raising_close)
    assert "close FAILED: mt5 close failed" in alerts[0]


def test_instant_same_direction_bare_resyncs_raw_text_only(fresh_db):
    _insert_tg_signal("tg10", raw_text="old bare", direction="SELL", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    result, alerts = _call("tg10", _GD2_BARE_SELL)
    row = _get_tg_row("tg10")
    assert row["raw_text"] == _GD2_BARE_SELL
    assert alerts == []


def test_ai_fallback_success_same_direction_updates_fields(fresh_db):
    _insert_tg_signal("tg13", raw_text="old", direction="BUY", status="new")
    _call("tg13", "AI recovered text", ai_fallback_return=_AI_SAME_DIR)
    row = _get_tg_row("tg13")
    assert row["entry_low"] == 4529.0


def test_ai_fallback_success_direction_flip_corrects_in_place(fresh_db):
    _insert_tg_signal("tg14", raw_text="old", direction="BUY", status="new")
    result, alerts = _call("tg14", "AI recovered flip text", ai_fallback_return=_AI_FLIP)
    row = _get_tg_row("tg14")
    assert row["direction"] == "SELL"
    assert "SIGNAL CORRECTED via edit" in alerts[0]

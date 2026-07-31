"""Characterizes the dedup/edit-correction state machine embedded inline in
SimulationEngine._scan_messages (core/engine.py:6330-6561) against
UNMODIFIED engine.py, ahead of extraction into
forex_trader/core/core_scan_messages_edit_reparse.py -- see
docs/todo/refactor/core-scan-messages-edit-reparse-migration/010-*.md.

The instant-entry-flip-flatten branch can close a real open position via
close_trade -- always faked here (its own behavior is already
characterized in core-close-trade-migration).
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from forex_trader.core import database as db
from backend.src.services.telegram import alerts as telegram_alerts
from forex_trader.core.engine import SimulationEngine


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
    db.update_risk_settings({"accept_tg_signals": 1})
    db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "test")
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeTgReader:
    def __init__(self, messages, slots=None, group_name="TestChannel"):
        self._messages = messages
        self._slots = slots or {}
        self._group_name = group_name

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return self._slots

    def get_group_name(self, group_id):
        return self._group_name


def _make_engine(msgs):
    e = SimulationEngine.__new__(SimulationEngine)
    e._tg_reader = _FakeTgReader(msgs)
    e._cfg = {}
    e._bridge = mock.Mock()  # Logic Keywords' RISK FREE/BE trigger check needs this attribute to exist
    return e


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


_GD2_BUY = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"
_GD2_SELL = "XAU USD SELL NOW\n4520-4525\nTP1 4510\nSL 4530"
_GD2_BARE_SELL = "XAU USD SELL NOW"

_AI_SAME_DIR = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
                "tp1": 4537.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
                "tp6": None, "tp7": None, "tp8": None}
_AI_FLIP = {"direction": "SELL", "entry_low": 4520.0, "entry_high": 4525.0, "stop_loss": 4530.0,
            "tp1": 4510.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
            "tp6": None, "tp7": None, "tp8": None}


async def _default_close(self_, trade_id, reason=None, **kw):
    return {"trade_id": trade_id}


def _run(msgs, ai_fallback_return="__unset__", close_trade_fake=None,
         followup_fake=None):
    e = _make_engine(msgs)
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    async def fake_ai(self_, text, channel_name, tg_id):
        return None if ai_fallback_return == "__unset__" else ai_fallback_return

    async def fake_followup(self_, channel_name, direction, parsed, tg_id):
        return False

    patches = [
        mock.patch.object(db, "should_generate_signals_here", return_value=True),
        mock.patch.object(SimulationEngine, "_try_ai_signal_fallback", fake_ai),
        mock.patch.object(SimulationEngine, "_find_and_apply_instant_followup", followup_fake or fake_followup),
        mock.patch.object(SimulationEngine, "close_trade", close_trade_fake or _default_close),
        mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send),
    ]
    for p in patches:
        p.start()
    try:
        result = asyncio.run(e._scan_messages())
    finally:
        for p in patches:
            p.stop()
    return result, alerts


def test_text_unchanged_dedup_skip(fresh_db):
    _insert_tg_signal("tg1", raw_text="SAME TEXT", status="new")
    msgs = [{"id": "tg1", "group_id": "g1", "text": "SAME TEXT", "timestamp": ""}]
    result, alerts = _run(msgs)
    assert result == []
    assert alerts == []


def test_full_reparse_same_direction_updates_fields_and_calls_followup(fresh_db):
    _insert_tg_signal("tg2", raw_text="old", direction="BUY", status="new")
    msgs = [{"id": "tg2", "group_id": "g1", "text": _GD2_BUY, "timestamp": ""}]
    followup_calls = []

    async def fake_followup(self_, channel_name, direction, parsed, tg_id):
        followup_calls.append((channel_name, direction, tg_id))
        return False

    _run(msgs, followup_fake=fake_followup)
    row = _get_tg_row("tg2")
    assert row["entry_low"] == 4529.0
    assert followup_calls == [("TestChannel", "BUY", "tg2")]


def test_full_reparse_same_direction_pending_followup_promotes(fresh_db):
    _insert_tg_signal("tg3", raw_text="old", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="pending_followup")
    msgs = [{"id": "tg3", "group_id": "g1", "text": _GD2_BUY, "timestamp": "2026-07-20T12:00:00Z"}]
    _run(msgs)
    row = _get_tg_row("tg3")
    assert row["status"] == "new"
    assert row["entry_low"] == 4529.0


def test_unparseable_edit_no_instant_status_ai_fails_dropped(fresh_db):
    _insert_tg_signal("tg4", raw_text="old", direction="BUY", status="new")
    msgs = [{"id": "tg4", "group_id": "g1", "text": "random commentary not a signal edit", "timestamp": ""}]
    result, alerts = _run(msgs, ai_fallback_return=None)
    assert result == []
    row = _get_tg_row("tg4")
    assert row["raw_text"] == "old"


def test_direction_flip_status_new_corrects_in_place(fresh_db):
    _insert_tg_signal("tg5", raw_text="old", direction="BUY", status="new")
    msgs = [{"id": "tg5", "group_id": "g1", "text": _GD2_SELL, "timestamp": ""}]
    result, alerts = _run(msgs)
    row = _get_tg_row("tg5")
    assert row["direction"] == "SELL"
    assert len(alerts) == 1
    assert "SIGNAL CORRECTED via edit" in alerts[0]


def test_direction_flip_already_executed_warns_only(fresh_db):
    _insert_tg_signal("tg6", raw_text="old", direction="BUY", status="activated")
    msgs = [{"id": "tg6", "group_id": "g1", "text": _GD2_SELL, "timestamp": ""}]
    result, alerts = _run(msgs)
    row = _get_tg_row("tg6")
    assert row["direction"] == "BUY"  # NOT auto-corrected
    assert row["raw_text"] == _GD2_SELL  # kept in sync so future scans converge
    assert len(alerts) == 1
    assert "SIGNAL EDIT WARNING" in alerts[0]


def test_instant_flip_flatten_matching_trade_closes_it(fresh_db):
    _insert_tg_signal("tg7", raw_text="old bare", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    _insert_open_trade("trade-abc", tg_source="TestChannel", direction="BUY")
    msgs = [{"id": "tg7", "group_id": "g1", "text": _GD2_BARE_SELL, "timestamp": ""}]
    close_calls = []

    async def fake_close(self_, trade_id, reason=None, **kw):
        close_calls.append((trade_id, reason))
        return {"trade_id": trade_id}

    result, alerts = _run(msgs, close_trade_fake=fake_close)
    assert close_calls == [("trade-abc", "instant_edit_flip:BUY->SELL")]
    row = _get_tg_row("tg7")
    assert row["direction"] == "SELL"
    assert "closed trade" in alerts[0]


def test_instant_flip_flatten_no_matching_trade(fresh_db):
    _insert_tg_signal("tg8", raw_text="old bare", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    msgs = [{"id": "tg8", "group_id": "g1", "text": _GD2_BARE_SELL, "timestamp": ""}]
    result, alerts = _run(msgs)
    assert "no matching open trade found" in alerts[0]


def test_instant_flip_flatten_close_trade_raises(fresh_db):
    _insert_tg_signal("tg9", raw_text="old bare", direction="BUY", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    _insert_open_trade("trade-xyz", tg_source="TestChannel", direction="BUY")
    msgs = [{"id": "tg9", "group_id": "g1", "text": _GD2_BARE_SELL, "timestamp": ""}]

    async def raising_close(self_, trade_id, reason=None, **kw):
        raise RuntimeError("mt5 close failed")

    result, alerts = _run(msgs, close_trade_fake=raising_close)
    assert "close FAILED: mt5 close failed" in alerts[0]


def test_instant_same_direction_bare_resyncs_raw_text_only(fresh_db):
    _insert_tg_signal("tg10", raw_text="old bare", direction="SELL", entry_low=None,
                      entry_high=None, stop_loss=None, tp1=None, status="instant_activated")
    msgs = [{"id": "tg10", "group_id": "g1", "text": _GD2_BARE_SELL, "timestamp": ""}]
    result, alerts = _run(msgs)
    row = _get_tg_row("tg10")
    assert row["raw_text"] == _GD2_BARE_SELL
    assert alerts == []


def test_ai_fallback_success_same_direction_updates_fields(fresh_db):
    _insert_tg_signal("tg13", raw_text="old", direction="BUY", status="new")
    msgs = [{"id": "tg13", "group_id": "g1", "text": "AI recovered text", "timestamp": ""}]
    _run(msgs, ai_fallback_return=_AI_SAME_DIR)
    row = _get_tg_row("tg13")
    assert row["entry_low"] == 4529.0


def test_ai_fallback_success_direction_flip_corrects_in_place(fresh_db):
    _insert_tg_signal("tg14", raw_text="old", direction="BUY", status="new")
    msgs = [{"id": "tg14", "group_id": "g1", "text": "AI recovered flip text", "timestamp": ""}]
    result, alerts = _run(msgs, ai_fallback_return=_AI_FLIP)
    row = _get_tg_row("tg14")
    assert row["direction"] == "SELL"
    assert "SIGNAL CORRECTED via edit" in alerts[0]

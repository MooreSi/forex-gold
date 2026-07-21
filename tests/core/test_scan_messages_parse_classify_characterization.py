"""Characterizes the channel-format-based signal parsing/classification
block embedded inline in SimulationEngine._scan_messages
(core/engine.py:6601-6807) against UNMODIFIED engine.py, ahead of
extraction into forex_trader/core/core_scan_messages_parse_classify.py --
see docs/todo/refactor/core-scan-messages-parse-classify-migration/010-*.md.

Parsing/classification and DB recording only -- no MT5 order is ever
placed, closed, or modified.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import telegram_alerts
from forex_trader.core import engine as engine_mod
from forex_trader.core import core_scan_messages_parse_classify as pc
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
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeTgReader:
    def __init__(self, messages):
        self._messages = messages

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return "TestChannel"


def _make_engine(msgs):
    e = SimulationEngine.__new__(SimulationEngine)
    e._tg_reader = _FakeTgReader(msgs)
    e._cfg = {}
    return e


def _setup_channel(fmt, prefix=""):
    db.save_channel_parser_config("TestChannel", fmt, prefix, True, True, "test")


def _get_tg_row(tg_id):
    with db.db() as conn:
        r = conn.execute("SELECT * FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)).fetchone()
        return db.row_to_dict(r) if r else None


_NOW_ISO = datetime.now(timezone.utc).isoformat()

_GD2_FULL = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"
_GD2_PARTIAL = "XAU USD BUY NOW\n4534 - 4529"
_GD2_BARE = "XAU USD BUY NOW"
_FORMAT_A = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
             "Sell Gold 4520 - 4512\nStop Loss 4524\nTP1 4510  TP2 4508  TP3 4506  TP4 4503  TP5 4500")

_AI_RESULT = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
              "tp1": 4537.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
              "tp6": None, "tp7": None, "tp8": None}


def _run(msgs, ai_return=None, force_gd2_all_fail=False, learned_rules_return=None):
    e = _make_engine(msgs)
    uq_calls = []
    alerts = []

    async def fake_ai(self_, text, channel_name, tg_id):
        return ai_return

    def fake_queue(self_, tg_id, channel_name, text):
        uq_calls.append((tg_id, channel_name))

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    patches = [
        mock.patch.object(db, "should_generate_signals_here", return_value=True),
        mock.patch.object(SimulationEngine, "_try_ai_signal_fallback", fake_ai),
        mock.patch.object(SimulationEngine, "_queue_unrecognised", fake_queue),
        mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send),
    ]
    if force_gd2_all_fail:
        import forex_trader.core.signal_parser as sp
        patches += [
            mock.patch.object(pc, "parse_gd2_signal", return_value=None),
            mock.patch.object(pc, "parse_gd2_partial", return_value=None),
            mock.patch.object(sp, "parse_gd2_instant_entry", return_value=None),
        ]
    if learned_rules_return is not None:
        patches.append(mock.patch.object(pc, "parse_with_learned_rules", return_value=learned_rules_return))
    for p in patches:
        p.start()
    try:
        result = asyncio.run(e._scan_messages())
    finally:
        for p in patches:
            p.stop()
    return result, uq_calls, alerts


def test_learned_rules_short_circuit(fresh_db):
    _setup_channel("format_ab")
    learned = {"direction": "SELL", "entry_low": 100.0, "entry_high": 101.0, "stop_loss": 102.0,
               "tp1": 99.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
               "tp6": None, "tp7": None, "tp8": None}
    result, uq, alerts = _run(
        [{"id": "b1", "group_id": "g1", "text": "anything at all", "timestamp": _NOW_ISO}],
        learned_rules_return=learned,
    )
    assert len(result) == 1
    assert result[0]["direction"] == "SELL"
    assert result[0]["entry_low"] == 100.0


def test_format_ab_no_match_ai_fails_dropped(fresh_db):
    _setup_channel("format_ab")
    result, uq, alerts = _run(
        [{"id": "b2", "group_id": "g1", "text": "totally unrelated chatter", "timestamp": _NOW_ISO}],
        ai_return=None,
    )
    assert result == []
    assert uq == []


def test_format_ab_no_match_ai_recovers(fresh_db):
    _setup_channel("format_ab")
    result, uq, alerts = _run(
        [{"id": "b3", "group_id": "g1", "text": "random chatter AI recovers", "timestamp": _NOW_ISO}],
        ai_return=_AI_RESULT,
    )
    assert len(result) == 1
    assert result[0]["direction"] == "BUY"


def test_format_ab_currency_mismatch_recorded_and_alerted(fresh_db):
    _setup_channel("format_ab")
    text = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Currency: EURUSD\nBuy Gold 1.1200 - 1.1180\nStop Loss 1.1150\nTP1 1.1250")
    result, uq, alerts = _run([{"id": "b4", "group_id": "g1", "text": text, "timestamp": _NOW_ISO}])
    assert result == []
    row = _get_tg_row("b4")
    assert row["status"] == "unsupported_currency"
    assert len(alerts) == 1


def test_format_ab_currency_mismatch_dedup_window_suppresses_second_alert(fresh_db):
    _setup_channel("format_ab")
    text1 = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
             "Direction  BUY\nCurrency: EURUSD\nENTRY : 1.1180-1.1200\nTP1: 1.1250\nSL: 1.1150")
    text2 = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
             "Direction  BUY\nCurrency: EURUSD\nENTRY : 1.1190-1.1210\nTP1: 1.1260\nSL: 1.1160")
    result, uq, alerts = _run([
        {"id": "dup1", "group_id": "g1", "text": text1, "timestamp": _NOW_ISO},
        {"id": "dup2", "group_id": "g1", "text": text2, "timestamp": _NOW_ISO},
    ])
    assert len(alerts) == 1


def test_format_ab_matches_parses_cleanly(fresh_db):
    _setup_channel("format_ab")
    result, uq, alerts = _run([{"id": "b5", "group_id": "g1", "text": _FORMAT_A, "timestamp": _NOW_ISO}])
    assert len(result) == 1
    assert result[0]["direction"] == "SELL"


def test_format_ab_matches_parse_fails_ai_fails_queues_unrecognised(fresh_db):
    _setup_channel("format_ab")
    bad_text = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
                "Direction BUY\nENTRY: 4510-4508\n")
    result, uq, alerts = _run(
        [{"id": "b6", "group_id": "g1", "text": bad_text, "timestamp": _NOW_ISO}], ai_return=None,
    )
    assert result == []
    assert uq == [("b6", "TestChannel")]


def test_gd2_no_match_ai_fails_dropped(fresh_db):
    _setup_channel("gd2")
    result, uq, alerts = _run(
        [{"id": "b8", "group_id": "g1", "text": "not a gold signal at all", "timestamp": _NOW_ISO}],
        ai_return=None,
    )
    assert result == []
    assert uq == []


def test_gd2_no_match_ai_recovers(fresh_db):
    _setup_channel("gd2")
    result, uq, alerts = _run(
        [{"id": "b9", "group_id": "g1", "text": "random chatter AI recovers as gd2", "timestamp": _NOW_ISO}],
        ai_return=_AI_RESULT,
    )
    assert len(result) == 1
    assert result[0]["direction"] == "BUY"


def test_gd2_full_parse_succeeds(fresh_db):
    _setup_channel("gd2")
    result, uq, alerts = _run([{"id": "b10", "group_id": "g1", "text": _GD2_FULL, "timestamp": _NOW_ISO}])
    assert len(result) == 1
    assert result[0]["direction"] == "BUY"


def test_gd2_partial_records_pending_followup(fresh_db):
    _setup_channel("gd2")
    result, uq, alerts = _run([{"id": "b11", "group_id": "g1", "text": _GD2_PARTIAL, "timestamp": _NOW_ISO}])
    assert result == []
    row = _get_tg_row("b11")
    assert row["status"] == "pending_followup"


def test_gd2_bare_instant_trigger_silently_skipped(fresh_db):
    _setup_channel("gd2")
    result, uq, alerts = _run([{"id": "b12", "group_id": "g1", "text": _GD2_BARE, "timestamp": _NOW_ISO}])
    assert result == []
    assert uq == []


def test_gd2_all_parsers_fail_ai_fails_queues_unrecognised(fresh_db):
    _setup_channel("gd2")
    text = "XAU USD BUY NOW garbled unrecognisable content"
    result, uq, alerts = _run(
        [{"id": "b13", "group_id": "g1", "text": text, "timestamp": _NOW_ISO}],
        ai_return=None, force_gd2_all_fail=True,
    )
    assert result == []
    assert uq == [("b13", "TestChannel")]


def test_gd2_all_parsers_fail_ai_recovers(fresh_db):
    _setup_channel("gd2")
    text = "XAU USD BUY NOW garbled unrecognisable content"
    result, uq, alerts = _run(
        [{"id": "b14", "group_id": "g1", "text": text, "timestamp": _NOW_ISO}],
        ai_return=_AI_RESULT, force_gd2_all_fail=True,
    )
    assert len(result) == 1
    assert result[0]["direction"] == "BUY"


def test_auto_format_ab_matches_first(fresh_db):
    _setup_channel("auto", prefix="")
    result, uq, alerts = _run([{"id": "b15", "group_id": "g1", "text": _FORMAT_A, "timestamp": _NOW_ISO}])
    assert len(result) == 1
    assert result[0]["direction"] == "SELL"


def test_auto_falls_to_gd2_full_parse(fresh_db):
    _setup_channel("auto", prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH")
    result, uq, alerts = _run([{"id": "b16", "group_id": "g1", "text": _GD2_FULL, "timestamp": _NOW_ISO}])
    assert len(result) == 1
    assert result[0]["direction"] == "BUY"


def test_auto_gd2_partial_records_pending_followup(fresh_db):
    _setup_channel("auto", prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH")
    result, uq, alerts = _run([{"id": "b17", "group_id": "g1", "text": _GD2_PARTIAL, "timestamp": _NOW_ISO}])
    assert result == []
    row = _get_tg_row("b17")
    assert row["status"] == "pending_followup"


def test_auto_neither_matches_ai_fails_silently_dropped(fresh_db):
    _setup_channel("auto", prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH")
    result, uq, alerts = _run(
        [{"id": "b18", "group_id": "g1", "text": "totally unrelated text", "timestamp": _NOW_ISO}],
        ai_return=None,
    )
    assert result == []
    assert uq == []  # NOT queued, unlike the configured-format branches


def test_auto_neither_matches_ai_recovers(fresh_db):
    _setup_channel("auto", prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH")
    result, uq, alerts = _run(
        [{"id": "b19", "group_id": "g1", "text": "totally unrelated text AI recovers", "timestamp": _NOW_ISO}],
        ai_return=_AI_RESULT,
    )
    assert len(result) == 1
    assert result[0]["direction"] == "BUY"

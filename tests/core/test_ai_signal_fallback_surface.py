"""Proves forex_trader.core.core_ai_signal_fallback's extracted functions
behave identically to SimulationEngine's originals, characterized in
test_ai_signal_fallback_characterization.py -- see
docs/todo/refactor/core-ai-signal-fallback-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified --
verified via the fake bridge's own call log.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from forex_trader.core import ai_signal_extractor
from forex_trader.core import claude_ai
from forex_trader.core import database as db
from forex_trader.core import core_ai_signal_fallback as fb


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
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self):
        self.modify_order_calls = []

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


class _RaisingBridge:
    async def modify_order(self, ticket, sl=None, tp=None):
        raise RuntimeError("mt5 unavailable")


def _insert_open_trade(trade_id="t-1", tg_source="GD VIP", mt5_ticket=555, stop_loss=2390.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-1", "BUY", 2399.0, 2401.0, 2380.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "sig-1", mt5_ticket, "BUY", 2399.0, 2401.0, 2400.0, 0.10, 0.10, stop_loss,
             "open", time.time(), tg_source),
        )


# ── try_ai_signal_fallback ─────────────────────────────────────────────────

def test_not_active_trader_node_returns_none_no_dedup_recorded(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, False, _FakeBridge(),
        ))
    assert result is None
    clf.assert_not_called()
    assert db.has_ai_fallback_check("tg-1", "hello") is False


def test_already_dedup_checked_returns_none_no_ai_call(fresh_db):
    db.record_ai_fallback_check("tg-1", "hello")
    with mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    clf.assert_not_called()


def test_non_xauusd_currency_returns_none_no_ai_call(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(fb.try_ai_signal_fallback(
            "Currency: EURUSD entering now", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    clf.assert_not_called()


def test_ai_call_raises_returns_none_dedup_not_recorded(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message", side_effect=RuntimeError("boom")):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    assert db.has_ai_fallback_check("tg-1", "hello") is False


def test_ai_call_returns_none_dedup_is_recorded(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message", return_value=None):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    assert db.has_ai_fallback_check("tg-1", "hello") is True


def test_ai_call_sl_adjustment_applies_and_returns_none(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    ai_result = {
        "kind": "sl_adjustment", "new_stop_loss": 2395.0,
        "_ai_confidence": 0.9, "_ai_reasoning": "moved SL per message",
    }
    bridge = _FakeBridge()
    with mock.patch.object(ai_signal_extractor, "classify_message", return_value=ai_result):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "adjust sl to 2395", "Chan", "tg-1", {}, True, bridge,
        ))

    assert result is None
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0
    with db.db() as conn:
        ai_row = conn.execute(
            "SELECT message_type, new_stop_loss FROM ai_recovered_signals WHERE tg_message_id=?", ("tg-1",)
        ).fetchone()
    assert ai_row[0] == "sl_adjustment"
    assert ai_row[1] == 2395.0


def test_ai_call_ordinary_signal_returns_result_dict(fresh_db):
    ai_result = {
        "direction": "BUY", "entry_low": 2399.0, "entry_high": 2401.0, "stop_loss": 2390.0,
        "tp1": 2405.0, "_ai_confidence": 0.85, "_ai_reasoning": "recovered from chatter",
    }
    with mock.patch.object(ai_signal_extractor, "classify_message", return_value=ai_result):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "buy gold now", "Chan", "tg-2", {}, True, _FakeBridge(),
        ))

    assert result == ai_result
    with db.db() as conn:
        ai_row = conn.execute(
            "SELECT direction, tg_message_id FROM ai_recovered_signals WHERE tg_message_id=?", ("tg-2",)
        ).fetchone()
    assert ai_row[0] == "BUY"


# ── apply_sl_adjustment ────────────────────────────────────────────────────────

def test_apply_sl_adjustment_no_matching_trade_is_noop(fresh_db):
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))
    assert bridge.modify_order_calls == []


def test_apply_sl_adjustment_already_matches_current_sl_is_noop(fresh_db):
    _insert_open_trade(tg_source="Chan", stop_loss=2395.005)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))
    assert bridge.modify_order_calls == []


def test_apply_sl_adjustment_applies_cleanly(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))

    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0


def test_apply_sl_adjustment_no_ticket_updates_db_skips_bridge(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=None, stop_loss=2390.0)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))

    assert bridge.modify_order_calls == []
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0


def test_apply_sl_adjustment_dedup_already_claimed_is_noop(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    db.try_claim_sl_adjustment("tg-1", "Chan", 2395.0)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))

    assert bridge.modify_order_calls == []
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2390.0


def test_apply_sl_adjustment_bridge_raises_no_db_write(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", _RaisingBridge()))

    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2390.0


# ── queue_unrecognised / analyse_unrecognised_message ─────────────────────────

async def _call_queue_unrecognised(tg_id, channel_name, text):
    with mock.patch.object(fb, "analyse_unrecognised_message", new=mock.AsyncMock()) as m:
        fb.queue_unrecognised(tg_id, channel_name, text, {})
        await asyncio.sleep(0)
        return m


def test_queue_unrecognised_new_message_saves_row_and_schedules_analysis(fresh_db):
    m = asyncio.run(_call_queue_unrecognised("tg-9", "Chan", "??? what is this"))
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM channel_unrecognised_messages WHERE tg_message_id=?", ("tg-9",)
        ).fetchone()
    assert row is not None
    m.assert_called_once()


def test_queue_unrecognised_already_queued_is_noop(fresh_db):
    db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    m = asyncio.run(_call_queue_unrecognised("tg-9", "Chan", "??? what is this"))
    with db.db() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_unrecognised_messages WHERE tg_message_id=?", ("tg-9",)
        ).fetchall()
    assert len(rows) == 1
    m.assert_not_called()


def test_analyse_unrecognised_message_success_updates_row(fresh_db):
    unrec_id = db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    analysis = {"is_signal": False, "summary": "just chatter"}
    with mock.patch.object(claude_ai, "classify_unknown_message", return_value=analysis), \
         mock.patch("forex_trader.core.telegram_alerts.send_message", new=mock.AsyncMock()):
        asyncio.run(fb.analyse_unrecognised_message(unrec_id, "Chan", "??? what is this", {}))

    with db.db() as conn:
        row = conn.execute(
            "SELECT claude_analysis FROM channel_unrecognised_messages WHERE id=?", (unrec_id,)
        ).fetchone()
    assert "just chatter" in row[0]


def test_analyse_unrecognised_message_exception_still_updates_row(fresh_db):
    unrec_id = db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    with mock.patch.object(claude_ai, "classify_unknown_message", side_effect=RuntimeError("ai down")), \
         mock.patch("forex_trader.core.telegram_alerts.send_message", new=mock.AsyncMock()):
        asyncio.run(fb.analyse_unrecognised_message(unrec_id, "Chan", "??? what is this", {}))

    with db.db() as conn:
        row = conn.execute(
            "SELECT claude_analysis FROM channel_unrecognised_messages WHERE id=?", (unrec_id,)
        ).fetchone()
    assert "ai down" in row[0]

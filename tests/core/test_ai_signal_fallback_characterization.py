"""Characterizes _try_ai_signal_fallback/_push_ai_recovered_created/
_apply_sl_adjustment/_queue_unrecognised/_analyse_unrecognised_message on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-ai-signal-fallback-migration/010-*.md.

ai_signal_extractor.classify_message/claude_ai.classify_unknown_message are
faked via unittest.mock.patch.object -- already-extracted, stable
collaborators, same treatment as dpm_engine.compute_adaptive_params in the
DPM handler pack. The Local/Remote sync mirror in
_push_ai_recovered_created is left calling the real sync.server/sync.client
modules, which gracefully no-op with no server/client running (confirmed).
NO real or demo MT5 order is ever placed, closed, or modified -- verified
via the fake bridge's own call log.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.services.ai import signal_extractor as ai_signal_extractor
from backend.src.services.ai import claude_ai as claude_ai
from forex_trader.core import core_ai_signal_fallback
from forex_trader.core import database as db
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


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._bridge = _FakeBridge()
    e._cfg = {}
    return e


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


# ── _try_ai_signal_fallback ─────────────────────────────────────────────────

def test_not_active_trader_node_returns_none_no_dedup_recorded(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=False), \
         mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(engine, "hello", "Chan", "tg-1"))
    assert result is None
    clf.assert_not_called()
    assert db.has_ai_fallback_check("tg-1", "hello") is False


def test_already_dedup_checked_returns_none_no_ai_call(fresh_db, engine):
    db.record_ai_fallback_check("tg-1", "hello")
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(engine, "hello", "Chan", "tg-1"))
    assert result is None
    clf.assert_not_called()


def test_non_xauusd_currency_returns_none_no_ai_call(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(
            engine, "Currency: EURUSD entering now", "Chan", "tg-1",
        ))
    assert result is None
    clf.assert_not_called()


def test_ai_call_raises_returns_none_dedup_not_recorded(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch.object(ai_signal_extractor, "classify_message", side_effect=RuntimeError("boom")):
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(engine, "hello", "Chan", "tg-1"))
    assert result is None
    assert db.has_ai_fallback_check("tg-1", "hello") is False


def test_ai_call_returns_none_dedup_is_recorded(fresh_db, engine):
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch.object(ai_signal_extractor, "classify_message", return_value=None):
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(engine, "hello", "Chan", "tg-1"))
    assert result is None
    assert db.has_ai_fallback_check("tg-1", "hello") is True


def test_ai_call_sl_adjustment_applies_and_returns_none(fresh_db, engine):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    ai_result = {
        "kind": "sl_adjustment", "new_stop_loss": 2395.0,
        "_ai_confidence": 0.9, "_ai_reasoning": "moved SL per message",
    }
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch.object(ai_signal_extractor, "classify_message", return_value=ai_result):
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(engine, "adjust sl to 2395", "Chan", "tg-1"))

    assert result is None
    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0
    with db.db() as conn:
        ai_row = conn.execute(
            "SELECT message_type, new_stop_loss FROM ai_recovered_signals WHERE tg_message_id=?", ("tg-1",)
        ).fetchone()
    assert ai_row[0] == "sl_adjustment"
    assert ai_row[1] == 2395.0


def test_ai_call_ordinary_signal_returns_result_dict(fresh_db, engine):
    ai_result = {
        "direction": "BUY", "entry_low": 2399.0, "entry_high": 2401.0, "stop_loss": 2390.0,
        "tp1": 2405.0, "_ai_confidence": 0.85, "_ai_reasoning": "recovered from chatter",
    }
    with mock.patch.object(SimulationEngine, "_is_active_trader_node", return_value=True), \
         mock.patch.object(ai_signal_extractor, "classify_message", return_value=ai_result):
        result = asyncio.run(SimulationEngine._try_ai_signal_fallback(engine, "buy gold now", "Chan", "tg-2"))

    assert result == ai_result
    with db.db() as conn:
        ai_row = conn.execute(
            "SELECT direction, tg_message_id FROM ai_recovered_signals WHERE tg_message_id=?", ("tg-2",)
        ).fetchone()
    assert ai_row[0] == "BUY"


# ── _apply_sl_adjustment ──────────────────────────────────────────────────────

def test_apply_sl_adjustment_no_matching_trade_is_noop(fresh_db, engine):
    asyncio.run(SimulationEngine._apply_sl_adjustment(engine, 2395.0, "Chan", "tg-1", "ai_fallback"))
    assert engine._bridge.modify_order_calls == []


def test_apply_sl_adjustment_already_matches_current_sl_is_noop(fresh_db, engine):
    _insert_open_trade(tg_source="Chan", stop_loss=2395.005)
    asyncio.run(SimulationEngine._apply_sl_adjustment(engine, 2395.0, "Chan", "tg-1", "ai_fallback"))
    assert engine._bridge.modify_order_calls == []


def test_apply_sl_adjustment_applies_cleanly(fresh_db, engine):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    asyncio.run(SimulationEngine._apply_sl_adjustment(engine, 2395.0, "Chan", "tg-1", "ai_fallback"))

    assert engine._bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0


def test_apply_sl_adjustment_no_ticket_updates_db_skips_bridge(fresh_db, engine):
    _insert_open_trade(tg_source="Chan", mt5_ticket=None, stop_loss=2390.0)
    asyncio.run(SimulationEngine._apply_sl_adjustment(engine, 2395.0, "Chan", "tg-1", "ai_fallback"))

    assert engine._bridge.modify_order_calls == []
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0


def test_apply_sl_adjustment_dedup_already_claimed_is_noop(fresh_db, engine):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    db.try_claim_sl_adjustment("tg-1", "Chan", 2395.0)  # pre-claim
    asyncio.run(SimulationEngine._apply_sl_adjustment(engine, 2395.0, "Chan", "tg-1", "ai_fallback"))

    assert engine._bridge.modify_order_calls == []
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2390.0


def test_apply_sl_adjustment_bridge_raises_no_db_write(fresh_db, engine):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    engine._bridge = _RaisingBridge()
    asyncio.run(SimulationEngine._apply_sl_adjustment(engine, 2395.0, "Chan", "tg-1", "ai_fallback"))

    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2390.0  # unchanged -- DB write happens after the bridge call


# ── _queue_unrecognised / _analyse_unrecognised_message ──────────────────────

async def _call_queue_unrecognised(engine, tg_id, channel_name, text):
    with mock.patch.object(core_ai_signal_fallback, "analyse_unrecognised_message",
                           new=mock.AsyncMock()) as m:
        SimulationEngine._queue_unrecognised(engine, tg_id, channel_name, text)
        await asyncio.sleep(0)  # let the scheduled task (if any) get created
        return m


def test_queue_unrecognised_new_message_saves_row_and_schedules_analysis(fresh_db, engine):
    m = asyncio.run(_call_queue_unrecognised(engine, "tg-9", "Chan", "??? what is this"))
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM channel_unrecognised_messages WHERE tg_message_id=?", ("tg-9",)
        ).fetchone()
    assert row is not None
    m.assert_called_once()


def test_queue_unrecognised_already_queued_is_noop(fresh_db, engine):
    db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    m = asyncio.run(_call_queue_unrecognised(engine, "tg-9", "Chan", "??? what is this"))
    with db.db() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_unrecognised_messages WHERE tg_message_id=?", ("tg-9",)
        ).fetchall()
    assert len(rows) == 1
    m.assert_not_called()


def test_analyse_unrecognised_message_success_updates_row(fresh_db, engine):
    unrec_id = db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    analysis = {"is_signal": False, "summary": "just chatter"}
    with mock.patch.object(claude_ai, "classify_unknown_message", return_value=analysis), \
         mock.patch("backend.src.services.telegram.alerts.send_message", new=mock.AsyncMock()):
        asyncio.run(SimulationEngine._analyse_unrecognised_message(engine, unrec_id, "Chan", "??? what is this"))

    with db.db() as conn:
        row = conn.execute(
            "SELECT claude_analysis FROM channel_unrecognised_messages WHERE id=?", (unrec_id,)
        ).fetchone()
    assert "just chatter" in row[0]


def test_analyse_unrecognised_message_exception_still_updates_row(fresh_db, engine):
    unrec_id = db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    with mock.patch.object(claude_ai, "classify_unknown_message", side_effect=RuntimeError("ai down")), \
         mock.patch("backend.src.services.telegram.alerts.send_message", new=mock.AsyncMock()):
        asyncio.run(SimulationEngine._analyse_unrecognised_message(engine, unrec_id, "Chan", "??? what is this"))

    with db.db() as conn:
        row = conn.execute(
            "SELECT claude_analysis FROM channel_unrecognised_messages WHERE id=?", (unrec_id,)
        ).fetchone()
    assert "ai down" in row[0]

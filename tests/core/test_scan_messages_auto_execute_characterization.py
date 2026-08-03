"""Characterizes the auto-execution flow embedded inline in
SimulationEngine._scan_messages (core/engine.py:6984-7364) against
UNMODIFIED engine.py, ahead of extraction into
forex_trader/core/core_scan_messages_auto_execute.py -- see
docs/todo/refactor/core-scan-messages-auto-execute-migration/010-*.md.

Highest real-money surface in the whole migration series -- places a real
MT5 order via open_trade. Always faked here.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.runtime import TradingRuntime


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
    db.update_risk_settings({"accept_tg_signals": 1, "auto_execute_signals": 1, "max_open_trades": 5})
    db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "test")
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


class _FakeTgReader:
    def __init__(self, messages, group_name="TestChannel"):
        self._messages = messages
        self._group_name = group_name

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return self._group_name


class _FakeBridge:
    def __init__(self, tick):
        self._tick = tick
        self.modify_calls = []

    async def get_tick(self):
        return self._tick

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_calls.append((ticket, sl, tp))
        return {"success": True}


def _make_engine(msgs, tick, group_name="TestChannel"):
    e = TradingRuntime.__new__(TradingRuntime)
    e._tg_reader = _FakeTgReader(msgs, group_name=group_name)
    e._cfg = {}
    e._bridge = _FakeBridge(tick)
    # _make_scan_ctx binds the pipeline's collaborators eagerly, so the
    # fixture has to set what the real __init__ always sets. The inline
    # body read these lazily, which is why the fixture grew one
    # attribute at a time (see _bridge above) instead of matching it.
    e._dpm_candles = None
    e._tg_off_warn_state = {}
    return e


def _get_last_signal():
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals ORDER BY created_at DESC LIMIT 1").fetchone()
        )


# Evaluated per call, not at import. As a module-level constant this was
# fixed at collection time, so by the time these tests ran near the end of
# a ~6 minute suite the "fresh" timestamp was older than the production
# staleness threshold (_MAX_SIGNAL_AGE_SECS = 4 minutes) and the signal was
# correctly rejected as stale. Passed in isolation, failed in the full run.
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
_GD2_FULL = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"
_IN_ZONE_TICK = SimpleNamespace(bid=4530.0, ask=4531.0)


async def _default_open_trade(self_, **kwargs):
    _default_open_trade.calls.append(kwargs)
    return {"trade_id": "trade-xyz", "entry_price": kwargs.get("entry_low", 4530.0),
            "mt5_ticket": 999, "managed_by": "python"}
_default_open_trade.calls = []


def _run(msgs, tick, open_trade_fn=None, open_trades=None, filter_err=None,
         followup_matched=False, group_name="TestChannel", capture_alerts=False):
    _default_open_trade.calls = []
    e = _make_engine(msgs, tick, group_name=group_name)
    alerts = []

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    with mock.patch.object(db, "should_generate_signals_here", return_value=True), \
         mock.patch.object(telegram_alerts, "send_message",
                           side_effect=fake_send if capture_alerts else mock.AsyncMock()), \
         mock.patch.object(TradingRuntime, "open_trade", open_trade_fn or _default_open_trade), \
         mock.patch.object(TradingRuntime, "get_open_trades", return_value=open_trades or []), \
         mock.patch.object(TradingRuntime, "_check_pre_trade_filters", return_value=filter_err), \
         mock.patch.object(TradingRuntime, "_find_and_apply_instant_followup",
                           new=mock.AsyncMock(return_value=followup_matched)):
        result = asyncio.run(e._scan_messages())
    return result, list(_default_open_trade.calls), alerts, e._bridge


def test_in_zone_execute_happy_path(fresh_db):
    result, calls, alerts, bridge = _run(
        [{"id": "d1", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK)
    assert result[0]["auto_executed"] is True
    assert len(calls) == 1
    assert calls[0]["strategy"] == "scale_out"
    assert calls[0]["stop_loss"] == 4527.0
    assert _get_last_signal()["status"] == "active"


def test_ime_followup_matched_skips_open_trade(fresh_db):
    db.update_risk_settings({"immediate_market_entry": 1})
    result, calls, alerts, bridge = _run(
        [{"id": "d13", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        followup_matched=True,
    )
    assert result[0]["auto_executed"] is True
    assert calls == []


def test_max_open_trades_reached_skips(fresh_db):
    db.update_risk_settings({"max_open_trades": 3})
    result, calls, alerts, bridge = _run(
        [{"id": "d2", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        open_trades=[{}] * 3,
    )
    assert result[0]["auto_executed"] is False
    assert calls == []


def test_no_tick_skips(fresh_db):
    result, calls, alerts, bridge = _run(
        [{"id": "d3", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], None)
    assert result[0]["auto_executed"] is False
    assert calls == []


def test_self_managed_conservative_uses_fixed_pre_execution_sl(fresh_db):
    db.set_channel_strategy_override("TestChannel", "conservative", auto=False)
    result, calls, alerts, bridge = _run(
        [{"id": "d4", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK)
    assert result[0]["auto_executed"] is True
    # entry_mid = 4531.5, CONSERVATIVE_SL_PT = 5.0, BUY -> entry_mid - 5
    assert calls[0]["stop_loss"] == 4526.5


def test_zone_breached_skips_outright(fresh_db):
    breached_tick = SimpleNamespace(bid=4520.0, ask=4520.5)  # below BUY zone 4529-4534
    result, calls, alerts, bridge = _run(
        [{"id": "d5", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], breached_tick)
    assert result[0]["auto_executed"] is False
    assert calls == []


def test_out_of_zone_not_breached_queues_pending(fresh_db):
    outside_tick = SimpleNamespace(bid=4538.0, ask=4538.5)  # above BUY zone, not gap-adjustable
    result, calls, alerts, bridge = _run(
        [{"id": "d6", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], outside_tick)
    assert result[0]["auto_executed"] is False
    assert calls == []
    assert _get_last_signal()["status"] == "pending"


def test_pre_trade_filter_fails_skips(fresh_db):
    result, calls, alerts, bridge = _run(
        [{"id": "d14", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        filter_err="R:R too low",
    )
    assert result[0]["auto_executed"] is False
    assert calls == []


def test_validate_signal_fails_skips(fresh_db):
    db.set_channel_strategy_override("TestChannel", "scale_out", auto=False)
    bad_text = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4520\n\nSL 4527"  # TP1 wrong side of entry
    result, calls, alerts, bridge = _run(
        [{"id": "d15", "group_id": "g1", "text": bad_text, "timestamp": _now_iso()}], _IN_ZONE_TICK)
    if result:
        assert result[0]["auto_executed"] is False
    assert calls == []


def test_open_trade_stood_down_silently_reverts_to_pending(fresh_db):
    async def raise_stood_down(self_, **kwargs):
        raise ValueError("Trading stood down — the paired node has taken control")

    result, calls, alerts, bridge = _run(
        [{"id": "d7", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        open_trade_fn=raise_stood_down, capture_alerts=True,
    )
    assert result[0]["auto_executed"] is False
    assert alerts == []
    assert _get_last_signal()["status"] == "pending"


def test_open_trade_circuit_breaker_surfaces_message_as_skip_reason(fresh_db):
    async def raise_cb(self_, **kwargs):
        raise RuntimeError("circuit breaker active, cooldown 45 min")

    result, calls, alerts, bridge = _run(
        [{"id": "d8", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        open_trade_fn=raise_cb, capture_alerts=True,
    )
    assert result[0]["auto_executed"] is False
    assert len(alerts) == 1


def test_open_trade_other_error_generic_skip_reason(fresh_db):
    async def raise_other(self_, **kwargs):
        raise RuntimeError("something unexpected broke")

    result, calls, alerts, bridge = _run(
        [{"id": "d9", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        open_trade_fn=raise_other, capture_alerts=True,
    )
    assert result[0]["auto_executed"] is False
    assert len(alerts) == 1


def test_executed_remotely_suppresses_final_alert(fresh_db):
    async def remote_open(self_, **kwargs):
        return {"trade_id": "trade-remote", "entry_price": 4530.0, "executed_remotely": True}

    result, calls, alerts, bridge = _run(
        [{"id": "d10", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
        open_trade_fn=remote_open, capture_alerts=True,
    )
    assert result[0]["auto_executed"] is True
    assert alerts == []


def test_gap_adjusted_market_entry_gd2_source(fresh_db):
    db.update_risk_settings({"immediate_market_entry": 1})
    db.save_channel_parser_config("Gold Diggers 2.0", "gd2", "", True, True, "test")
    gap_tick = SimpleNamespace(bid=4535.0, ask=4536.0)  # 2pt above BUY zone, within 10pt GD2 cap
    result, calls, alerts, bridge = _run(
        [{"id": "d11", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], gap_tick,
        group_name="Gold Diggers 2.0",
    )
    assert result[0]["auto_executed"] is True
    assert calls[0]["stop_loss"] == 4529.0  # 4527 + 2pt gap
    assert calls[0]["tp1"] == 4539.0  # 4537 + 2pt gap
    assert calls[0]["entry_low"] == 4531.0  # 4529 + 2pt gap


def test_ea_managed_conservative_post_fill_sync(fresh_db):
    db.set_channel_strategy_override("TestChannel", "conservative", auto=False)

    async def ea_open_trade(self_, **kwargs):
        return {"trade_id": "trade-xyz", "entry_price": kwargs["entry_low"],
                "mt5_ticket": 999, "managed_by": "ea"}

    ea_calls = []
    fake_ea = SimpleNamespace(update_trade=mock.AsyncMock(side_effect=lambda tid, tps: ea_calls.append((tid, tps))))
    with mock.patch.object(ea_bridge, "get_instance", return_value=fake_ea):
        result, calls, alerts, bridge = _run(
            [{"id": "d12", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], _IN_ZONE_TICK,
            open_trade_fn=ea_open_trade,
        )
    assert result[0]["auto_executed"] is True
    # fill = entry_low = 4529.0; exact_sl = 4529-5=4524.0; exact_tp1 = 4529+3=4532.0
    assert bridge.modify_calls == [(999, 4524.0, None)]
    assert ea_calls == [("trade-xyz", {1: 4532.0})]

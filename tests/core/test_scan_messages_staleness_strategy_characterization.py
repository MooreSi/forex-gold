"""Characterizes the staleness guard and per-channel strategy/skip-reason
resolution blocks embedded inline in SimulationEngine._scan_messages
(core/engine.py:6809-6982) against UNMODIFIED engine.py, ahead of
extraction into forex_trader/core/core_scan_messages_staleness_strategy.py
-- see docs/todo/refactor/core-scan-messages-staleness-strategy-migration/010-*.md.

No MT5 order is ever placed, closed, or modified -- the fake bridge's
get_tick() always returns None, keeping sub-pack D's own (not yet
extracted) auto-execution block a harmless no-op in every test here.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.channels import strategy_ai as channel_strategy_ai
from backend.src.services.ai import provider as ai_provider
from backend.src.runtime import SimulationEngine


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
    def __init__(self, messages):
        self._messages = messages

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return "TestChannel"


class _NoTickBridge:
    async def get_tick(self):
        return None


def _make_engine(msgs):
    e = SimulationEngine.__new__(SimulationEngine)
    e._tg_reader = _FakeTgReader(msgs)
    e._cfg = {}
    e._bridge = _NoTickBridge()
    # _make_scan_ctx binds the pipeline's collaborators eagerly, so the
    # fixture has to set what the real __init__ always sets. The inline
    # body read these lazily, which is why the fixture grew one
    # attribute at a time (see _bridge above) instead of matching it.
    e._dpm_candles = None
    e._tg_off_warn_state = {}
    return e


def _get_tg_row(tg_id):
    with db.db() as conn:
        r = conn.execute("SELECT * FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)).fetchone()
        return db.row_to_dict(r) if r else None


# Evaluated per call, not at import. As a module-level constant this was
# fixed at collection time, so by the time these tests ran near the end of
# a ~6 minute suite the "fresh" timestamp was older than the production
# staleness threshold (_MAX_SIGNAL_AGE_SECS = 4 minutes) and the signal was
# correctly rejected as stale. Passed in isolation, failed in the full run.
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
_STALE_ISO = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
_GD2_FULL = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"


def _run(msgs, session_allowed=(True, "london"), trading_paused=False,
         ai_configured=False, eval_return=None, eval_raises=False):
    e = _make_engine(msgs)
    fmt_calls = []
    alerts = []

    def fake_fmt_signal(parsed, channel_name, executed, exec_lot=None, exec_price=None,
                        skip_reason="", strategy_name="", **kw):
        fmt_calls.append({"executed": executed, "skip_reason": skip_reason, "strategy_name": strategy_name})
        return "alert text"

    async def fake_send(msg, *a, **k):
        alerts.append(msg)

    async def fake_eval(engine, parsed, channel_name, cfg):
        if eval_raises:
            raise RuntimeError("eval boom")
        return eval_return

    patches = [
        mock.patch.object(db, "should_generate_signals_here", return_value=True),
        mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send),
        mock.patch.object(telegram_alerts, "fmt_signal", side_effect=fake_fmt_signal),
        mock.patch.object(db, "is_session_allowed", return_value=session_allowed),
        mock.patch.object(SimulationEngine, "is_trading_paused", return_value=trading_paused),
        mock.patch.object(SimulationEngine, "get_open_trades", return_value=[]),
        mock.patch.object(ai_provider, "is_configured", return_value=ai_configured),
        mock.patch.object(channel_strategy_ai, "evaluate_signal_strategy", side_effect=fake_eval),
    ]
    for p in patches:
        p.start()
    try:
        result = asyncio.run(e._scan_messages())
    finally:
        for p in patches:
            p.stop()
    return result, fmt_calls, alerts


def test_stale_message_recorded_historical_alerted_once(fresh_db):
    result, calls, alerts = _run([{"id": "c1", "group_id": "g1", "text": _GD2_FULL, "timestamp": _STALE_ISO}])
    assert result == []
    row = _get_tg_row("c1")
    assert row["status"] == "historical"
    assert len(alerts) == 1


def test_no_timestamp_treated_as_stale(fresh_db):
    result, calls, alerts = _run([{"id": "c3", "group_id": "g1", "text": _GD2_FULL, "timestamp": ""}])
    assert result == []
    row = _get_tg_row("c3")
    assert row["status"] == "historical"


def test_fresh_message_recorded_new(fresh_db):
    result, calls, alerts = _run([{"id": "c2", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}])
    assert len(result) == 1
    row = _get_tg_row("c2")
    assert row["status"] == "new"


def test_no_override_uses_global_trade_strategy(fresh_db):
    db.update_risk_settings({"trade_strategy": "be_runner"})
    result, calls, alerts = _run([{"id": "c4", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}])
    assert calls[0]["strategy_name"] == "Breakeven Runner"


def test_auto_override_auto_execute_off_uses_channel_rec(fresh_db):
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    db.set_channel_strategy_rec("TestChannel", "trail_stop", "reasoning", 0.9)
    result, calls, alerts = _run([{"id": "c5", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}])
    assert calls[0]["strategy_name"] == "Trailing Stop"


def test_auto_override_ai_not_configured_uses_channel_rec(fresh_db):
    db.update_risk_settings({"auto_execute_signals": 1})
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    db.set_channel_strategy_rec("TestChannel", "protected_scale", "reasoning", 0.9)
    result, calls, alerts = _run(
        [{"id": "c6", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], ai_configured=False,
    )
    assert calls[0]["strategy_name"] == "Protected Scale"


def test_auto_override_ai_eval_skip(fresh_db):
    db.update_risk_settings({"auto_execute_signals": 1})
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    result, calls, alerts = _run(
        [{"id": "c7", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}],
        ai_configured=True, eval_return={"skip": True, "reasoning": "too risky"},
    )
    assert calls[0]["skip_reason"] == "Auto-eval declined signal: too risky"


def test_auto_override_ai_eval_strategy(fresh_db):
    db.update_risk_settings({"auto_execute_signals": 1})
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    result, calls, alerts = _run(
        [{"id": "c8", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}],
        ai_configured=True, eval_return={"skip": False, "strategy": "no_sl_scale", "confidence": 0.8},
    )
    assert calls[0]["strategy_name"] == "Trend Ratchet"


def test_auto_override_ai_eval_raises_falls_back_to_channel_rec(fresh_db):
    db.update_risk_settings({"auto_execute_signals": 1})
    db.set_channel_strategy_override("TestChannel", None, auto=True)
    db.set_channel_strategy_rec("TestChannel", "conservative_trial", "reasoning", 0.9)
    result, calls, alerts = _run(
        [{"id": "c9", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}],
        ai_configured=True, eval_raises=True,
    )
    assert calls[0]["strategy_name"] == "Conservative Trial"


def test_specific_channel_override_used_directly(fresh_db):
    db.set_channel_strategy_override("TestChannel", "trail_stop", auto=False)
    result, calls, alerts = _run([{"id": "c10", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}])
    assert calls[0]["strategy_name"] == "Trailing Stop"


def test_high_risk_text_forces_conservative(fresh_db):
    db.set_channel_strategy_override("TestChannel", "trail_stop", auto=False)
    text_hr = _GD2_FULL + "\nHigh Risk Trade"
    result, calls, alerts = _run([{"id": "c11", "group_id": "g1", "text": text_hr, "timestamp": _now_iso()}])
    assert calls[0]["strategy_name"] == "Conservative"


def test_dpm_enabled_overrides_displayed_name_only(fresh_db):
    db.update_risk_settings({"dpm_enabled": 1})
    result, calls, alerts = _run([{"id": "c12", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}])
    assert calls[0]["strategy_name"] == "DPM"


def test_session_not_allowed_skip_reason(fresh_db):
    result, calls, alerts = _run(
        [{"id": "c13", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}],
        session_allowed=(False, "asian"),
    )
    assert "Asian Market Turned Off" in calls[0]["skip_reason"]


def test_trading_paused_skip_reason(fresh_db):
    db.set_app_config("risk_halt_reason", "daily loss limit hit")
    db.set_app_config("trade_pause_until", str(time.time() + 3600))
    result, calls, alerts = _run(
        [{"id": "c14", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}], trading_paused=True,
    )
    assert "daily loss limit hit" in calls[0]["skip_reason"]


def test_default_skip_reason(fresh_db):
    result, calls, alerts = _run([{"id": "c15", "group_id": "g1", "text": _GD2_FULL, "timestamp": _now_iso()}])
    assert calls[0]["skip_reason"] == "Auto-execution is OFF — activate manually in the dashboard."

"""Characterizes SimulationEngine._email_scheduler_loop's per-cycle body
(core/engine.py) against UNMODIFIED engine.py, ahead of extraction into
forex_trader/core/core_email_scheduler.py -- see
docs/todo/refactor/core-email-scheduler-migration/010-*.md.

The ORB section's auto-execute path can place a real MT5 order via
_orb_auto_execute -- always faked here (its own order-placing behavior was
already characterized in core-orb-report-migration).
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime
from unittest import mock

import pytest

from forex_trader.core import database as db
from backend.src.services.notifications import email_service
from backend.src.services.ai import claude_ai as claude_ai
from backend.src.services.notifications import scheduler as core_email_scheduler
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
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _patched_now(fixed_dt):
    """Patches core_email_scheduler.datetime (where engine.py's now-wired
    _email_scheduler_loop actually computes the current time, having
    delegated to email_scheduler_sweep) while leaving direct datetime(...)
    construction working via the real class."""
    patcher = mock.patch("backend.src.services.notifications.scheduler.datetime")
    mock_dt = patcher.start()
    mock_dt.now.return_value = fixed_dt
    mock_dt.side_effect = lambda *a, **kw: datetime(*a, **kw)
    return patcher


def _make_engine():
    e = SimulationEngine.__new__(SimulationEngine)
    e._monitor_running = True
    e._cfg = {"x": 1}
    e._bridge = mock.Mock()
    return e


def _stop_after_second_sleep(engine):
    calls = {"n": 0}

    async def _sleep(*a, **k):
        calls["n"] += 1
        if calls["n"] >= 2:
            engine._monitor_running = False

    return _sleep


def _insert_closed_trade(trade_id, close_time):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 2400.0, 2400.0, 2390.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", "BUY", 2400.0, 2400.0, 2400.0, 0.1, 0.0,
             2390.0, "closed", close_time - 100, close_time),
        )


async def _fake_perf(bridge, days):
    return {"balance": 1000.0, "daily_pnl": 5.0}


def test_no_provider_configured_nothing_runs(fresh_db):
    db.save_email_config({"smtp_host": "", "resend_api_key": "", "mailjet_api_key": ""})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 8, 15, 0))  # Monday
    build_calls = []

    async def fake_build(bridge):
        build_calls.append(1)
        return {}

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "build_orb_report", fake_build):
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    assert build_calls == []


def test_orb_email_sent_with_chart_and_dedup_set(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "orb_report_enabled": 1})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 8, 15, 0))  # Monday
    report = {"ok": True}
    sent = []

    async def fake_build(bridge):
        return report

    async def fake_send(subject, html, cfg, image_bytes=None, image_cid=None):
        sent.append((subject, bool(image_bytes)))
        return True, None

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "build_orb_report", fake_build), \
             mock.patch.object(email_service, "build_orb_chart_image", return_value=b"PNG"), \
             mock.patch.object(email_service, "build_orb_html", return_value="<html>orb</html>"), \
             mock.patch.object(email_service, "send_email", side_effect=fake_send):
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    assert sent == [("FOREX Trader — London Open ORB Report — 2026-07-20", True)]
    assert db.get_app_config("email_last_orb") == "2026-07-20"


def test_orb_auto_execute_only_no_email_report_still_built(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "orb_report_enabled": 0})
    db.update_risk_settings({"orb_auto_execute_enabled": 1})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 8, 15, 0))
    report = {"ok": True}
    build_calls, auto_calls = [], []

    async def fake_build(bridge):
        build_calls.append(1)
        return report

    async def fake_auto(r, bridge, is_active_trader_node):
        auto_calls.append(r)

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "build_orb_report", fake_build), \
             mock.patch.object(core_email_scheduler, "orb_auto_execute", fake_auto), \
             mock.patch.object(email_service, "send_email") as mock_send:
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    assert len(build_calls) == 1
    assert auto_calls == [report]
    mock_send.assert_not_called()
    assert db.get_app_config("orb_auto_execute_last") == "2026-07-20"


def test_send_time_mismatch_skips_daily_and_weekly(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1,
                          "weekly_enabled": 1, "orb_report_enabled": 0, "send_time": "18:00"})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 17, 59, 0))
    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(email_service, "send_email") as mock_send:
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    mock_send.assert_not_called()


def test_daily_email_sent_with_perf_and_claude_analysis(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1, "orb_report_enabled": 0})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 18, 0, 0))  # Monday
    _insert_closed_trade("t1", time.time())
    claude_calls, sent = [], []

    async def fake_claude(closed, bal, dpnl, cfg):
        claude_calls.append((len(closed), bal, dpnl))
        return "analysis text"

    async def fake_send(subject, html, cfg):
        sent.append(subject)
        return True, None

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "compute_mt5_performance", _fake_perf), \
             mock.patch.object(SimulationEngine, "_is_active_trader_node", staticmethod(lambda: True)), \
             mock.patch.object(claude_ai, "generate_daily_analysis", side_effect=fake_claude), \
             mock.patch.object(email_service, "build_daily_html", return_value="<html>daily</html>") as mock_build, \
             mock.patch.object(email_service, "send_email", side_effect=fake_send):
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    assert claude_calls == [(1, 1000.0, 5.0)]
    assert sent == ["FOREX Trader Daily Summary — 2026-07-20"]
    assert mock_build.call_args.kwargs.get("claude_analysis") == "analysis text"
    assert db.get_app_config("email_last_daily") == "2026-07-20"


def test_daily_skipped_on_non_active_trader_node(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1, "orb_report_enabled": 0})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 18, 0, 0))
    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(SimulationEngine, "_is_active_trader_node", staticmethod(lambda: False)), \
             mock.patch.object(email_service, "send_email") as mock_send:
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    mock_send.assert_not_called()


def test_daily_claude_exception_swallowed_email_still_sent(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1, "orb_report_enabled": 0})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 20, 18, 0, 0))
    sent = []

    async def raising_claude(closed, bal, dpnl, cfg):
        raise RuntimeError("claude down")

    async def fake_send(subject, html, cfg):
        sent.append(subject)
        return True, None

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "compute_mt5_performance", _fake_perf), \
             mock.patch.object(SimulationEngine, "_is_active_trader_node", staticmethod(lambda: True)), \
             mock.patch.object(claude_ai, "generate_daily_analysis", side_effect=raising_claude), \
             mock.patch.object(email_service, "build_daily_html", return_value="<html>daily</html>") as mock_build, \
             mock.patch.object(email_service, "send_email", side_effect=fake_send):
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    assert sent == ["FOREX Trader Daily Summary — 2026-07-20"]
    assert mock_build.call_args.kwargs.get("claude_analysis") is None


def test_weekly_email_sent_on_friday_with_iso_week_dedup(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "weekly_enabled": 1, "orb_report_enabled": 0})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 24, 18, 0, 0))  # Friday
    _insert_closed_trade("t2", time.time())
    sent = []

    async def fake_send(subject, html, cfg):
        sent.append(subject)
        return True, None

    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "compute_mt5_performance", _fake_perf), \
             mock.patch.object(SimulationEngine, "_is_active_trader_node", staticmethod(lambda: True)), \
             mock.patch.object(email_service, "build_weekly_html", return_value="<html>weekly</html>"), \
             mock.patch.object(email_service, "send_email", side_effect=fake_send):
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    assert sent == ["FOREX Trader Weekly Summary — 2026-W30"]
    assert db.get_app_config("email_last_weekly") == "2026-W30"


def test_weekly_skipped_on_non_friday(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "weekly_enabled": 1, "orb_report_enabled": 0})
    e = _make_engine()
    p = _patched_now(datetime(2026, 7, 23, 18, 0, 0))  # Thursday
    try:
        with mock.patch("asyncio.sleep", new=mock.AsyncMock(side_effect=_stop_after_second_sleep(e))), \
             mock.patch.object(core_email_scheduler, "compute_mt5_performance", _fake_perf), \
             mock.patch.object(SimulationEngine, "_is_active_trader_node", staticmethod(lambda: True)), \
             mock.patch.object(email_service, "send_email") as mock_send:
            asyncio.run(e._email_scheduler_loop())
    finally:
        p.stop()
    mock_send.assert_not_called()

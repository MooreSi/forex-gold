"""Proves forex_trader.core.core_email_scheduler.email_scheduler_sweep
behaves identically to SimulationEngine's original, characterized in
test_email_scheduler_characterization.py -- see
docs/todo/refactor/core-email-scheduler-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class
(uk_now/local_now/is_active_trader_node passed explicitly instead of
mocking engine.datetime/_is_active_trader_node). No real or demo MT5 order
is ever placed, closed, or modified -- the order-placing collaborator
(orb_auto_execute) is always faked.
"""
import asyncio
import os
import tempfile
import time
from datetime import datetime
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import email_service, claude_ai
from forex_trader.core import core_email_scheduler as sched


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
    build_calls = []

    async def fake_build(bridge):
        build_calls.append(1)
        return {}

    with mock.patch.object(sched, "build_orb_report", fake_build):
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True, uk_now=datetime(2026, 7, 20, 8, 15, 0)))
    assert build_calls == []


def test_orb_email_sent_with_chart_and_dedup_set(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "orb_report_enabled": 1})
    report = {"ok": True}
    sent = []

    async def fake_build(bridge):
        return report

    async def fake_send(subject, html, cfg, image_bytes=None, image_cid=None):
        sent.append((subject, bool(image_bytes)))
        return True, None

    with mock.patch.object(sched, "build_orb_report", fake_build), \
         mock.patch.object(email_service, "build_orb_chart_image", return_value=b"PNG"), \
         mock.patch.object(email_service, "build_orb_html", return_value="<html>orb</html>"), \
         mock.patch.object(email_service, "send_email", side_effect=fake_send):
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True, uk_now=datetime(2026, 7, 20, 8, 15, 0)))
    assert sent == [("FOREX Trader — London Open ORB Report — 2026-07-20", True)]
    assert db.get_app_config("email_last_orb") == "2026-07-20"


def test_orb_auto_execute_only_no_email_report_still_built(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "orb_report_enabled": 0})
    db.update_risk_settings({"orb_auto_execute_enabled": 1})
    report = {"ok": True}
    build_calls, auto_calls = [], []

    async def fake_build(bridge):
        build_calls.append(1)
        return report

    async def fake_auto(r, bridge, is_active):
        auto_calls.append((r, is_active))

    with mock.patch.object(sched, "build_orb_report", fake_build), \
         mock.patch.object(sched, "orb_auto_execute", fake_auto), \
         mock.patch.object(email_service, "send_email") as mock_send:
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True, uk_now=datetime(2026, 7, 20, 8, 15, 0)))
    assert len(build_calls) == 1
    assert auto_calls == [(report, True)]
    mock_send.assert_not_called()
    assert db.get_app_config("orb_auto_execute_last") == "2026-07-20"


def test_send_time_mismatch_skips_daily_and_weekly(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1,
                          "weekly_enabled": 1, "orb_report_enabled": 0, "send_time": "18:00"})
    with mock.patch.object(email_service, "send_email") as mock_send:
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True,
            uk_now=datetime(2026, 7, 20, 17, 59, 0),
            local_now=datetime(2026, 7, 20, 17, 59, 0)))
    mock_send.assert_not_called()


def test_daily_email_sent_with_perf_and_claude_analysis(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1, "orb_report_enabled": 0})
    _insert_closed_trade("t1", time.time())
    claude_calls, sent = [], []

    async def fake_claude(closed, bal, dpnl, cfg):
        claude_calls.append((len(closed), bal, dpnl))
        return "analysis text"

    async def fake_send(subject, html, cfg):
        sent.append(subject)
        return True, None

    with mock.patch.object(sched, "compute_mt5_performance", _fake_perf), \
         mock.patch.object(claude_ai, "generate_daily_analysis", side_effect=fake_claude), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html>daily</html>") as mock_build, \
         mock.patch.object(email_service, "send_email", side_effect=fake_send):
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {"x": 1}, True,
            uk_now=datetime(2026, 7, 20, 8, 0, 0),
            local_now=datetime(2026, 7, 20, 18, 0, 0)))
    assert claude_calls == [(1, 1000.0, 5.0)]
    assert sent == ["FOREX Trader Daily Summary — 2026-07-20"]
    assert mock_build.call_args.kwargs.get("claude_analysis") == "analysis text"
    assert db.get_app_config("email_last_daily") == "2026-07-20"


def test_daily_skipped_on_non_active_trader_node(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1, "orb_report_enabled": 0})
    with mock.patch.object(sched, "compute_mt5_performance", _fake_perf), \
         mock.patch.object(email_service, "send_email") as mock_send:
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, False,
            uk_now=datetime(2026, 7, 20, 8, 0, 0),
            local_now=datetime(2026, 7, 20, 18, 0, 0)))
    mock_send.assert_not_called()


def test_daily_claude_exception_swallowed_email_still_sent(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1, "orb_report_enabled": 0})
    sent = []

    async def raising_claude(closed, bal, dpnl, cfg):
        raise RuntimeError("claude down")

    async def fake_send(subject, html, cfg):
        sent.append(subject)
        return True, None

    with mock.patch.object(sched, "compute_mt5_performance", _fake_perf), \
         mock.patch.object(claude_ai, "generate_daily_analysis", side_effect=raising_claude), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html>daily</html>") as mock_build, \
         mock.patch.object(email_service, "send_email", side_effect=fake_send):
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True,
            uk_now=datetime(2026, 7, 20, 8, 0, 0),
            local_now=datetime(2026, 7, 20, 18, 0, 0)))
    assert sent == ["FOREX Trader Daily Summary — 2026-07-20"]
    assert mock_build.call_args.kwargs.get("claude_analysis") is None


def test_weekly_email_sent_on_friday_with_iso_week_dedup(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "weekly_enabled": 1, "orb_report_enabled": 0})
    _insert_closed_trade("t2", time.time())
    sent = []

    async def fake_send(subject, html, cfg):
        sent.append(subject)
        return True, None

    with mock.patch.object(sched, "compute_mt5_performance", _fake_perf), \
         mock.patch.object(email_service, "build_weekly_html", return_value="<html>weekly</html>"), \
         mock.patch.object(email_service, "send_email", side_effect=fake_send):
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True,
            uk_now=datetime(2026, 7, 24, 8, 0, 0),
            local_now=datetime(2026, 7, 24, 18, 0, 0)))
    assert sent == ["FOREX Trader Weekly Summary — 2026-W30"]
    assert db.get_app_config("email_last_weekly") == "2026-W30"


def test_weekly_skipped_on_non_friday(fresh_db):
    db.save_email_config({"smtp_host": "smtp.example.com", "weekly_enabled": 1, "orb_report_enabled": 0})
    with mock.patch.object(sched, "compute_mt5_performance", _fake_perf), \
         mock.patch.object(email_service, "send_email") as mock_send:
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True,
            uk_now=datetime(2026, 7, 23, 8, 0, 0),
            local_now=datetime(2026, 7, 23, 18, 0, 0)))
    mock_send.assert_not_called()

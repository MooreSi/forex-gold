"""The daily analysis is the AI analysis, not "the Claude analysis".

Settings > AI selects the provider (Claude or DeepSeek) and the daily
analysis runs on whichever is selected. A DeepSeek user was shown a "Claude
AI Coaching" email section and "Daily Claude analysis failed" in the log for
an analysis Claude never wrote.

Only the words a person reads change. The `claude_analysis` keyword and the
`claude_ai` module name are an internal contract other tests pin and are left
alone.

No provider is reached: the analysis, the mail send and MT5 are all faked.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from unittest import mock

from backend.src.db import database as db
from backend.src.services.ai import claude_ai
from backend.src.services.notifications import email_html, email_service
from backend.src.services.notifications import scheduler as sched
from backend.src.services.trading import bot_trading

_ANALYSIS = {
    "overall_summary": "One stop-out, otherwise disciplined.",
    "overall_pnl_verdict": "negative",
    "best_strategy_today": "be_runner",
    "best_strategy_reason": "Protected the open profit.",
    "key_lesson": "Respect the stop.",
    "tomorrow_focus": ["Wait for the retest"],
    "trade_analysis": [],
}


def _warnings(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]


def test_the_email_section_is_headed_ai_coaching_and_names_no_provider():
    html = email_html.build_daily_html(
        {"balance": 1000.0, "daily_pnl": -20.2}, [], "Monday, 12 October 2026",
        claude_analysis=_ANALYSIS)

    assert ">AI Coaching</p>" in html
    assert "Claude" not in html


def test_a_failed_scheduled_analysis_is_logged_as_the_ai_analysis(fresh_db, caplog):
    db.save_email_config({"smtp_host": "smtp.example.com", "daily_enabled": 1,
                          "orb_report_enabled": 0})

    async def perf(bridge, days):
        return {"balance": 1000.0, "daily_pnl": 5.0}

    with mock.patch.object(sched, "compute_mt5_performance", perf), \
         mock.patch.object(claude_ai, "generate_daily_analysis",
                           new=mock.AsyncMock(side_effect=RuntimeError("provider down"))), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html></html>"), \
         mock.patch.object(email_service, "send_email",
                           new=mock.AsyncMock(return_value=(True, None))), \
         caplog.at_level(logging.WARNING):
        asyncio.run(sched.email_scheduler_sweep(
            "bridge", {}, True,
            uk_now=datetime(2026, 7, 20, 8, 0, 0),
            local_now=datetime(2026, 7, 20, 18, 0, 0)))

    assert "Daily AI analysis failed: provider down" in _warnings(caplog)


def test_a_failed_manual_report_analysis_is_logged_as_the_ai_analysis(fresh_db, caplog):
    ecfg = db.get_email_config()
    ecfg["to_addr"] = "ops@example.com"
    db.save_email_config(ecfg)

    with mock.patch.object(bot_trading, "compute_mt5_performance",
                           new=mock.AsyncMock(return_value={})), \
         mock.patch.object(claude_ai, "generate_daily_analysis",
                           new=mock.AsyncMock(side_effect=RuntimeError("provider down"))), \
         mock.patch.object(email_service, "build_daily_html", return_value="<html></html>"), \
         mock.patch.object(email_service, "send_email",
                           new=mock.AsyncMock(return_value=(True, None))), \
         caplog.at_level(logging.WARNING):
        asyncio.run(bot_trading.cmd_report([], "bridge", {}))

    assert "Daily AI analysis (manual report) failed: provider down" in _warnings(caplog)

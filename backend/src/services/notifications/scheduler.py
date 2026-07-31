"""Email scheduler sweep -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine._email_scheduler_loop's per-cycle body,
as part of the core/engine.py migration series. See
docs/todo/refactor/core-email-scheduler-migration/020-*.md.

No MT5 order is placed/modified/closed directly by this module, but the
ORB section's auto-execute path can trigger one via the already-extracted
core_orb_report.orb_auto_execute (its own order-placing behavior was
already characterized in core-orb-report-migration).

Reuses core_orb_report.build_orb_report/orb_auto_execute and
core_mt5_performance.compute_mt5_performance as collaborators (all three
already extracted and independently characterized) rather than re-deriving
them. `is_active_trader_node`/`cfg_obj` (the engine's own `self._cfg`,
forwarded to Claude for the daily analysis) are taken as explicit
parameters, same pattern as every other pack in this cluster.

Note: the ORB section's time gate uses Europe/London
(`datetime.now(ZoneInfo("Europe/London"))`), while the daily/weekly send-
time gate uses bare server-local `datetime.now()` -- a pre-existing
inconsistency, preserved here as `uk_now`/`local_now`, each independently
injectable for tests.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from forex_trader.core import database as db_module
from backend.src.services.ai import claude_ai as claude_ai
from backend.src.services.notifications import email_service
from backend.src.services.broker.mt5_performance import compute_mt5_performance
from backend.src.services.analytics.orb_report import build_orb_report
from forex_trader.core.core_orb_report import orb_auto_execute

log = logging.getLogger(__name__)


async def _run_orb_section(bridge: Any, cfg: dict, is_active_trader_node: bool,
                            uk_now: datetime) -> None:
    orb_email_on = bool(cfg.get("orb_report_enabled", 1))
    orb_auto_rs = await db_module.to_db_thread(db_module.get_risk_settings)
    orb_auto_on = bool(orb_auto_rs.get("orb_auto_execute_enabled", 0))
    if not (orb_email_on or orb_auto_on):
        return
    try:
        if uk_now.hour == 8 and uk_now.minute == 15 and uk_now.weekday() < 5:
            uk_date_str = uk_now.strftime("%Y-%m-%d")
            report = None
            if orb_email_on and db_module.get_app_config("email_last_orb") != uk_date_str:
                report = await build_orb_report(bridge)
                if report:
                    chart_png = email_service.build_orb_chart_image(report)
                    orb_html = email_service.build_orb_html(
                        report, uk_now.strftime("%A, %d %B %Y"),
                        has_chart=bool(chart_png),
                    )
                    ok, err = await email_service.send_email(
                        f"FOREX Trader — London Open ORB Report — {uk_date_str}",
                        orb_html, cfg,
                        image_bytes=chart_png, image_cid=email_service._ORB_CHART_CID,
                    )
                    if ok:
                        db_module.set_app_config("email_last_orb", uk_date_str)
                    log.info("ORB report email %s%s", "sent" if ok else "FAILED",
                              f": {err}" if err else "")
            if orb_auto_on and db_module.get_app_config("orb_auto_execute_last") != uk_date_str:
                if report is None:
                    report = await build_orb_report(bridge)
                if report:
                    await orb_auto_execute(report, bridge, is_active_trader_node)
                    db_module.set_app_config("orb_auto_execute_last", uk_date_str)
    except Exception as e:
        log.warning("ORB report scheduling error: %s", e)


async def _run_daily_section(bridge: Any, cfg: dict, cfg_obj: Any,
                              is_active_trader_node: bool, local_now: datetime,
                              today_str: str, perf: dict) -> None:
    if not (cfg.get("daily_enabled") and local_now.weekday() < 5 and is_active_trader_node):
        return
    last_daily = db_module.get_app_config("email_last_daily")
    if last_daily == today_str:
        return
    day_cutoff = local_now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    with db_module.db() as conn:
        closed_today = [
            db_module.row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time>? "
                "ORDER BY close_time DESC",
                (day_cutoff,),
            ).fetchall()
        ]
    _balance = float(perf.get("balance", 0) or 0)
    _dpnl = float(perf.get("daily_pnl", 0) or 0)
    try:
        claude_analysis = await claude_ai.generate_daily_analysis(
            closed_today, _balance, _dpnl, cfg_obj,
        )
    except Exception as _ae:
        log.warning("Daily Claude analysis failed: %s", _ae)
        claude_analysis = None
    html = email_service.build_daily_html(
        perf, closed_today,
        local_now.strftime("%A, %d %B %Y"),
        claude_analysis=claude_analysis,
    )
    ok, err = await email_service.send_email(
        f"FOREX Trader Daily Summary — {today_str}", html, cfg
    )
    if ok:
        db_module.set_app_config("email_last_daily", today_str)
    log.info("Daily email %s%s", "sent" if ok else "FAILED",
              f": {err}" if err else "")


async def _run_weekly_section(cfg: dict, is_active_trader_node: bool,
                               local_now: datetime, perf: dict) -> None:
    if not (cfg.get("weekly_enabled") and local_now.weekday() == 4 and is_active_trader_node):
        return
    iso = local_now.isocalendar()
    week_key = f"{iso[0]}-W{iso[1]:02d}"
    last_weekly = db_module.get_app_config("email_last_weekly")
    if last_weekly == week_key:
        return
    cutoff_week = time.time() - 7 * 86400
    with db_module.db() as conn:
        week_trades = [
            db_module.row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time>? "
                "ORDER BY close_time DESC",
                (cutoff_week,),
            ).fetchall()
        ]
    html = email_service.build_weekly_html(
        perf, week_trades,
        f"Week ending {local_now.strftime('%d %B %Y')}",
    )
    ok, err = await email_service.send_email(
        f"FOREX Trader Weekly Summary — {week_key}", html, cfg
    )
    if ok:
        db_module.set_app_config("email_last_weekly", week_key)
    log.info("Weekly email %s%s", "sent" if ok else "FAILED",
              f": {err}" if err else "")


async def email_scheduler_sweep(bridge: Any, cfg_obj: Any, is_active_trader_node: bool,
                                 uk_now: Optional[datetime] = None,
                                 local_now: Optional[datetime] = None) -> None:
    cfg = db_module.get_email_config()
    # Any one of these makes send_email() able to deliver — checking
    # smtp_host alone here (as before) silently skipped this entire
    # loop, ORB report included, on a node configured for Resend/
    # Mailjet with no SMTP host set at all (confirmed live on the
    # VPS: resend_api_key present, smtp_host empty).
    has_provider = bool(
        (cfg.get("smtp_host") or "").strip()
        or (cfg.get("resend_api_key") or "").strip()
        or (cfg.get("mailjet_api_key") or "").strip()
    )
    if not has_provider:
        return

    if uk_now is None:
        uk_now = datetime.now(ZoneInfo("Europe/London"))
    await _run_orb_section(bridge, cfg, is_active_trader_node, uk_now)

    if local_now is None:
        local_now = datetime.now()
    send_time_str = cfg.get("send_time", "18:00") or "18:00"
    try:
        sh, sm = [int(x) for x in send_time_str.split(":")]
    except Exception:
        sh, sm = 18, 0
    if local_now.hour != sh or local_now.minute != sm:
        return

    today_str = local_now.strftime("%Y-%m-%d")
    try:
        perf = await compute_mt5_performance(bridge, 90)
    except Exception:
        perf = {}

    await _run_daily_section(bridge, cfg, cfg_obj, is_active_trader_node, local_now, today_str, perf)
    await _run_weekly_section(cfg, is_active_trader_node, local_now, perf)

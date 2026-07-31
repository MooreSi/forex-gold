"""Staleness guard and per-channel strategy/skip-reason resolution --
extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine._scan_messages (lines 6809-6982), as part of the
core/engine.py migration series. See
docs/todo/refactor/core-scan-messages-staleness-strategy-migration/020-*.md.

No MT5 order is ever placed, closed, or modified by either function.

`engine_for_eval` is forwarded unchanged to
`channel_strategy_ai.evaluate_signal_strategy`, which needs the full
engine instance -- same "forward the engine through" pattern as
`core_reversal_research.reversal_engine_research_sweep`. `is_trading_paused_fn` is
a required injected async callable bound to the caller's own
`self.is_trading_paused`.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.channels import strategy_ai as channel_strategy_ai
from backend.src.services.ai import provider as ai_provider
from backend.src.utils.models import STRATEGY_CONSERVATIVE, STRATEGY_SCALE_OUT, STRATEGY_NAMES

log = logging.getLogger(__name__)

_MAX_SIGNAL_AGE_SECS = 4 * 60  # 4 minutes

_SESS_HUMAN = {
    "asian": "Asian Market",
    "london": "London Market",
    "overlap": "London & New York Market",
    "ny": "New York Market",
}


async def record_staleness_or_new(
    tg_id: str, group_id: str, channel_name: str, msg: dict, parsed: dict, source_label: str,
) -> bool:
    """Returns True if the message is fresh enough to proceed (recorded as
    'new'); False if stale (recorded as 'historical', alerted once if
    newly recorded) -- caller should skip to the next message."""
    msg_ts_str = msg.get("timestamp") or ""
    msg_age_secs = None
    if msg_ts_str:
        try:
            from datetime import datetime as _dt
            _tg_dt = _dt.fromisoformat(msg_ts_str.replace("Z", "+00:00"))
            if _tg_dt.tzinfo is None:
                from datetime import timezone as _tz
                _tg_dt = _tg_dt.replace(tzinfo=_tz.utc)
            msg_age_secs = time.time() - _tg_dt.timestamp()
        except Exception:
            pass

    is_stale = msg_age_secs is None or msg_age_secs > _MAX_SIGNAL_AGE_SECS

    if is_stale:
        with db_module.db() as conn:
            _cur = conn.execute(
                """INSERT OR IGNORE INTO vantage_tg_signals
                   (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                    direction,entry_low,entry_high,stop_loss,
                    tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (tg_id, group_id, channel_name, msg.get("sender_name", ""), msg_ts_str,
                 msg.get("text") or "", time.time(),
                 parsed["direction"], parsed["entry_low"], parsed["entry_high"],
                 parsed["stop_loss"],
                 parsed["tp1"], parsed["tp2"], parsed["tp3"], parsed["tp4"], parsed["tp5"],
                 parsed["tp6"], parsed["tp7"], parsed["tp8"],
                 "historical"),
            )
            _was_new = _cur.rowcount > 0
        age_hrs = (msg_age_secs or 0) / 3600
        log.info("[%s] Stale signal tg_id=%s age=%.1fh — recorded only",
                 source_label, tg_id, age_hrs)
        if _was_new:
            _stale_text = telegram_alerts.fmt_signal(
                parsed, channel_name,
                executed=False,
                skip_reason=f"Signal detected but NOT executed — {age_hrs:.1f}h old (app was offline)",
                strategy_name="",
            )
            asyncio.create_task(
                telegram_alerts.send_message(_stale_text, tg_id, "signal_stale")
            )
        return False

    with db_module.db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO vantage_tg_signals
               (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, group_id, channel_name, msg.get("sender_name", ""), msg_ts_str,
             msg.get("text") or "", time.time(),
             parsed["direction"], parsed["entry_low"], parsed["entry_high"], parsed["stop_loss"],
             parsed["tp1"], parsed["tp2"], parsed["tp3"], parsed["tp4"], parsed["tp5"],
             parsed["tp6"], parsed["tp7"], parsed["tp8"], "new"),
        )
    log.info("[%s] New signal tg_id=%s %s entry %s-%s SL %s",
             source_label, tg_id, parsed["direction"],
             parsed["entry_low"], parsed["entry_high"], parsed["stop_loss"])
    return True


async def resolve_strategy_and_skip_reason(
    rs: dict,
    channel_name: str,
    text: str,
    parsed: dict,
    auto_execute: bool,
    cfg_obj: Any,
    engine_for_eval: Any,
    is_trading_paused_fn: Callable[[], Awaitable[bool]],
) -> dict:
    """Returns {'strategy', 'strategy_name', 'skip_reason', 'sess_ok',
    'per_signal_skip', 'per_signal_skip_reason'}."""
    per_signal_skip = False
    per_signal_skip_rsn = ""
    strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT)
    _ch_ov_tg = db_module.get_channel_strategy_override(channel_name)
    if _ch_ov_tg == "auto":
        if auto_execute:
            if ai_provider.is_configured(cfg_obj):
                try:
                    _sig_eval = await channel_strategy_ai.evaluate_signal_strategy(
                        engine_for_eval, parsed, channel_name, cfg_obj,
                    )
                    if _sig_eval.get("skip"):
                        per_signal_skip = True
                        per_signal_skip_rsn = _sig_eval.get("reasoning", "AI rejected signal")
                        log.info("[channel_ai] per-signal SKIP %s: %s",
                                channel_name, per_signal_skip_rsn)
                    else:
                        strategy = _sig_eval.get("strategy") or strategy
                        log.info("[channel_ai] per-signal %s → %s (%.0f%%)",
                                channel_name, strategy,
                                _sig_eval.get("confidence", 0) * 100)
                except Exception as _eval_exc:
                    log.debug("per-signal eval failed: %s", _eval_exc)
                    _ch_rec_tg = db_module.get_channel_strategy_rec(channel_name)
                    strategy = _ch_rec_tg.get("strategy") or strategy
            else:
                _ch_rec_tg = db_module.get_channel_strategy_rec(channel_name)
                strategy = _ch_rec_tg.get("strategy") or strategy
        else:
            _ch_rec_tg = db_module.get_channel_strategy_rec(channel_name)
            strategy = _ch_rec_tg.get("strategy") or strategy
    elif _ch_ov_tg:
        strategy = _ch_ov_tg

    if "high risk" in text.lower():
        log.info("[%s] 'High Risk' flagged in message — using Conservative "
                 "strategy for this trade only", channel_name)
        strategy = STRATEGY_CONSERVATIVE

    if bool(rs.get("dpm_enabled", 0)):
        strategy_name = "DPM"
    elif ea_templates.is_template_override(strategy):
        strategy_name = f"Template: {ea_templates.template_name_from_override(strategy)}"
    else:
        strategy_name = STRATEGY_NAMES.get(strategy, strategy)

    sess_ok, sess_name = db_module.is_session_allowed(rs)
    market_off_msg = (
        f"\U0001f6ab {_SESS_HUMAN.get(sess_name, sess_name.title())} Turned Off"
        if not sess_ok else ""
    )
    if market_off_msg:
        skip_reason = market_off_msg + " — signal received but not executed."
    elif await is_trading_paused_fn():
        halt_reason = db_module.get_app_config("risk_halt_reason") or "risk halt active"
        pause_until = db_module.get_app_config("trade_pause_until")
        try:
            until_str = time.strftime("%H:%M UTC", time.gmtime(float(pause_until)))
            halt_reason += f", resumes ~{until_str}"
        except Exception:
            pass
        skip_reason = f"⏸️ Trading paused — {halt_reason}."
    else:
        skip_reason = "Auto-execution is OFF — activate manually in the dashboard."

    return {
        "strategy": strategy,
        "strategy_name": strategy_name,
        "skip_reason": skip_reason,
        "sess_ok": sess_ok,
        "per_signal_skip": per_signal_skip,
        "per_signal_skip_reason": per_signal_skip_rsn,
    }

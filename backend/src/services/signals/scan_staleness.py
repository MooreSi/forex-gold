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

from backend.src.db import database as db_module
from backend.src.services.signals import tg_repo
from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.channels import strategy_ai as channel_strategy_ai
from backend.src.services.ai import provider as ai_provider
from backend.src.utils.models import STRATEGY_CONSERVATIVE, STRATEGY_SCALE_OUT, STRATEGY_NAMES
from backend.src.services.risk import expert_params
from backend.src.services.telegram import alerts
from backend.src.services.ai import provider
from backend.src.services.channels import strategy_ai
from backend.src.services.risk.schedule import get_schedule_strategy_override

log = logging.getLogger(__name__)

def max_signal_age_secs() -> int:
    """Signals older than this are recorded as historical, never executed.
    Was a 4-minute constant; now Settings > Expert Tunables."""
    return expert_params.get("max_signal_age_s")

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

    is_stale = msg_age_secs is None or msg_age_secs > max_signal_age_secs()

    if is_stale:
        _was_new = tg_repo.insert_tg_signal_if_new(
            tg_id, group_id, channel_name, msg.get("sender_name", ""),
            msg_ts_str, msg.get("text") or "", parsed, "historical",
        )
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

    tg_repo.insert_tg_signal_if_new(
        tg_id, group_id, channel_name, msg.get("sender_name", ""),
        msg_ts_str, msg.get("text") or "", parsed, "new",
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

    # Trading Schedule per-window override (2026-08-06). The active window's
    # own strategy/template pick for THIS channel wins over its Channel
    # Strategy setting for as long as that window is active -- the same
    # precedence core_signal_resolution.resolve_open_trade_params has applied
    # since the feature landed (see its "Trading Schedule window override >
    # channel override" block). This path was the only strategy-resolution
    # site that never consulted it, so a template assigned per schedule window
    # was silently ignored for every Telegram signal that arrived through the
    # scan loop -- the channel-level assignment was the only one that did
    # anything, and a window configured to a DIFFERENT template than the
    # channel ran the channel's one instead.
    #
    # channel_name is always a real Telegram channel here (this is the
    # Telegram scan path), so no ENGINE_SOURCE_KEYS mapping is needed --
    # _resolve_source_gate canonicalises the name and reads the window's
    # telegram_channels entry. None means "no opinion" (schedule off, no
    # active window, channel disabled in it, or no override configured) and
    # leaves the channel-level pick untouched.
    _sched_ov_tg = get_schedule_strategy_override(channel_name)
    if _sched_ov_tg:
        _ch_ov_tg = _sched_ov_tg

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

    # "High Risk" must not override a template-assigned channel -- a
    # template fully replaces strategy dispatch by design (see
    # core_ea_templates.py's module docstring), and this override doesn't
    # just lose that intent locally: engine.py's own Limit-format dispatch
    # (the tp_open check right after this function returns) tests
    # is_template_override(strategy) on the ALREADY-mutated value, so
    # clobbering it here also defeated that check's template-wins
    # protection -- confirmed live 2026-07-27: GOLD DIGGERS INSTITUTIONAL's
    # "HIGH RISK TRADE" disclaimer appears on effectively every signal
    # (channel-wide boilerplate, not a per-signal risk flag), so every one
    # of that channel's Limit-format signals silently executed as Limit
    # Runner instead of its assigned Test Template, no grid, no fractal
    # trail, none of the template's own management.
    if "high risk" in text.lower() and not ea_templates.is_template_override(strategy):
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

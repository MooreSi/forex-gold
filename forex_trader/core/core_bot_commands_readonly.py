"""Read-only/toggle Telegram bot commands -- originally extracted verbatim
from core/engine.py's SimulationEngine._cmd_* methods as part of the
core/engine.py migration series. See
docs/todo/refactor/core-bot-commands-readonly-migration/020-*.md.

None of these commands place, close, or modify a live order -- they only
read account/trade state or flip a risk-settings/app-config flag.

Two of them have since grown past "extracted verbatim", and the detail now
lives in its own module so this one stays a thin command surface:
cmd_balance -> core_bot_balance_report, cmd_status -> core_bot_channel_status.

cmd_daily was removed (2026-08-08) along with the panel's Daily button: its
account and today's-P&L sections are what cmd_balance now opens with, so
keeping it would have meant two buttons answering the same question with
independently-drifting arithmetic.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core.core_bot_balance_report import build_balance_report
from forex_trader.core.core_bot_channel_status import (
    channel_status_lines, internal_engine_lines,
)
from forex_trader.core.core_fees_sizing import pnl
from forex_trader.core.core_sim_account import get_sim_account
from forex_trader.core.core_trade_reporting import get_open_trades
from forex_trader.core.models import (
    STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER, STRATEGY_TRAIL_STOP,
    STRATEGY_PROTECTED_SCALE, STRATEGY_CONSERVATIVE, STRATEGY_NO_SL_SCALE,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SCALP_RUNNER, STRATEGY_SIGNAL_CLIMBER,
    STRATEGY_FIXED_RR,
    STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
    STRATEGY_NAMES,
)


async def cmd_help(args: list) -> str:
    """The bot is button-driven (core_bot_panel) — this is a map of the panel,
    not a command list. The old typed commands were not removed so much as
    relocated: each line below names the button that now does that job."""
    return (
        "*FOREX Trader — Control Panel*\n\n"
        "Send /panel to open it. Everything is buttons — the old typed "
        "commands are now the buttons listed here.\n\n"
        "*Main menu*\n"
        "📊 Status / 📋 All Settings — system status, MT5 bridge & EA link, "
        "then every channel's lots, entries, TP ladders and trigger settings\n"
        "💵 Balance — balance, equity, open P&L, then realised P&L for "
        "today, this week day by day, and this month\n"
        "📜 Open Trades — all open trades with P&L\n"
        "⚙️ Channel Strategy — per-channel settings\n"
        "🎯 Channel Trades — per-channel trade operations\n"
        "🛠️ System — pause/resume, DPM, IME, restarts, demo/live\n"
        "🗓️ Trading Schedule — on/off, daily target, and per-day windows "
        "(hours, profit target, which channels/engines may trade)\n"
        "⛔ CLOSE ALL TRADES — close every open trade\n\n"
        "*Channel Strategy* — pick a channel, then:\n"
        "• EA Template channels get the full grid: anchors, layers, lots, "
        "ladder step, SL pips, risk %, TP & pcts ladders, grid/TP/BE/trail "
        "modes, harvest, anchor shave, sig guard\n"
        "• Built-in strategy channels get strategy, risk %, lot and the "
        "active toggle\n"
        "• Numbers use − / + buttons or ✏️ Set exact value (reply with the "
        "number)\n\n"
        "*Channel Trades* — BUY, SELL, delete pending, risk free "
        "(SL to entry), open-trade list with per-trade close, close all\n\n"
        "/status and /help still work as typed commands."
    )


async def cmd_balance(args: list, bridge: Any) -> str:
    """Account state plus realised P&L for today, this week (by day) and this
    month. Absorbed the old /daily summary when the panel's separate Daily
    button was removed -- see core_bot_balance_report for why they merged."""
    return await build_balance_report(bridge)


async def cmd_status(args: list, bridge: Any, tg_reader: Optional[Any] = None) -> str:
    rs         = db_module.get_risk_settings()
    auto_exec  = bool(rs.get("auto_execute_signals", 0))
    risk_pct   = float(rs.get("risk_per_trade_pct", 0.5))
    max_trades = int(rs.get("max_open_trades", 1))
    open_count = len(get_open_trades())

    pause_raw = db_module.get_app_config("trade_pause_until")
    paused    = pause_raw and float(pause_raw or 0) > time.time()
    if paused:
        until_str  = datetime.fromtimestamp(float(pause_raw)).strftime("%H:%M")
        trade_line = f"PAUSED until {until_str}"
    else:
        trade_line = "Active" if auto_exec else "Manual only"

    # MT5 bridge status — LISTEN filter ensures we detect the bridge server,
    # not our own keep-alive client connection to the bridge port.
    #
    # The port comes from mt5_bridge_url, not a hardcoded 9000: an instance
    # configured on any other port (this checkout defaults to 9010, precisely
    # so a fork cannot dial into the live app's bridge) reported "NOT running"
    # while the bridge was healthy and the EA was trading through it. A status
    # command that cries wolf about the bridge is worse than one that omits
    # the line -- it sends you diagnosing an outage that isn't happening.
    try:
        from urllib.parse import urlparse
        from forex_trader.core.platform_utils import is_port_listening as _ipl
        import forex_trader.config as _cfg_mod
        _bridge_port = urlparse(_cfg_mod.get("mt5_bridge_url", "") or "").port or 9000
        _bridge_up = _ipl(_bridge_port)
    except Exception:
        _bridge_up = False
    bridge_line = "Connected" if _bridge_up else "NOT running"

    # EA link. The MT5 bridge above and the EA are two different links and
    # fail independently -- the bridge can be up (prices, account, manual
    # orders all fine) while the EA sits on a chart dialling a port nobody
    # is listening on, which is exactly the failure core_ea_link_watchdog
    # exists to catch. get_effective_ea_status reports whichever node is
    # actually trading, so a VPS-traded setup doesn't report the laptop's
    # idle EA socket.
    try:
        from forex_trader.core.ea_bridge import get_effective_ea_status
        ea_up, ea_scope = get_effective_ea_status()
    except Exception:
        ea_up, ea_scope = False, "unknown"
    ea_line = f"{'Connected' if ea_up else 'NOT connected'} ({ea_scope})"

    schedule_line = _schedule_line()

    if tg_reader is not None:
        try:
            auth = tg_reader.get_status().get("auth_state", "disconnected")
        except Exception:
            auth = "unavailable"
    else:
        auth = "not started"

    # No global "Strategy:" line here. It named the app's Active Strategy,
    # which nothing on this account actually trades: every channel and every
    # internal generator resolves its own strategy (and a Trading Schedule
    # window can override that by time of day), so one global name at the top
    # was wrong for every block below it. Each block states its own instead.
    lines = [
        "*System Status*",
        "",
        f"Auto-execute: {'ON' if auto_exec else 'OFF'}",
        f"Risk/trade:   {risk_pct}%",
        f"Max trades:   {max_trades}",
        f"Open trades:  {open_count}",
        f"Trading:      {trade_line}",
        f"Schedule:     {schedule_line}",
        "",
        f"MT5 Bridge:   {bridge_line}",
        f"EA (MT5):     {ea_line}",
        f"Telegram:     {auth}",
    ]

    # Per-channel blocks — what each channel would actually do with its next
    # signal, replacing the old one-line-per-slot list (which said only that
    # a group was attached).
    channel_lines = channel_status_lines(tg_reader)
    if channel_lines:
        lines.append("")
        lines.extend(channel_lines)

    # The internal generators trade this account too, on their own strategies
    # and their own schedule gates -- a status that listed only the Telegram
    # channels described maybe half of what the app is doing.
    engine_lines = internal_engine_lines()
    if engine_lines:
        lines.append("")
        lines.extend(engine_lines)

    return _fit_telegram(lines)


def _schedule_line() -> str:
    """Whether the Trading Schedule is gating automated entries right now."""
    try:
        from forex_trader.core.core_trading_schedule import (
            check_trading_schedule, is_trading_schedule_enabled,
        )
        if not is_trading_schedule_enabled():
            return "OFF (always open)"
        allowed, reason = check_trading_schedule()
        return "ON — trading window open" if allowed else f"ON — blocked: {reason}"
    except Exception:
        return "unavailable"


# Telegram rejects a sendMessage over 4096 characters outright, so a status
# with enough channels to overflow would return nothing at all rather than a
# long message. Drop whole trailing lines instead, and say so.
_TG_MAX_CHARS = 4096
_TRUNCATED = "_…truncated — open the app for the full settings._"


def _fit_telegram(lines: list[str]) -> str:
    text = "\n".join(lines)
    if len(text) <= _TG_MAX_CHARS:
        return text
    budget = _TG_MAX_CHARS - len(_TRUNCATED) - 1
    kept: list[str] = []
    used = 0
    for line in lines:
        if used + len(line) + 1 > budget:
            break
        kept.append(line)
        used += len(line) + 1
    kept.append(_TRUNCATED)
    return "\n".join(kept)


async def cmd_trades(args: list, bridge: Any) -> str:
    open_trades = get_open_trades()
    if not open_trades:
        return "No open trades."

    tick = await bridge.get_tick()
    n    = len(open_trades)
    lines = [f"*Open {'Trade' if n == 1 else 'Trades'} ({n})*"]

    for i, t in enumerate(open_trades, 1):
        direction = t.get("direction", "?")
        entry     = float(t.get("entry_price", 0))
        lots      = float(t.get("remaining_lots", 0))
        sl        = t.get("stop_loss")
        tp1       = t.get("tp1")
        ticket    = t.get("mt5_ticket", "—")

        p = 0.0
        if tick:
            p = pnl(
                direction, entry,
                tick.bid if direction == "BUY" else tick.ask,
                lots,
            )

        held_secs = int(time.time() - float(t.get("open_time") or time.time()))
        if held_secs < 3600:
            held = f"{held_secs // 60}m"
        else:
            held = f"{held_secs // 3600}h {(held_secs % 3600) // 60}m"

        pnl_sign = "+" if p >= 0 else ""
        lines.append("")
        lines.append(f"*{i}. {direction} {lots} lots @ ${entry:.2f}*")
        lines.append(f"Ticket: {ticket}  |  Held: {held}")
        sl_str  = f"${float(sl):.2f}"  if sl  is not None else "—"
        tp1_str = f"${float(tp1):.2f}" if tp1 is not None else "—"
        lines.append(f"SL: {sl_str}  TP1: {tp1_str}")
        lines.append(f"P&L: {pnl_sign}${p:.2f}")

    return "\n".join(lines)


async def cmd_pause(args: list) -> str:
    duration_secs = 3600
    label         = "1h"
    if args:
        raw = args[0].lower()
        try:
            if raw.endswith("d"):
                duration_secs = int(raw[:-1]) * 86400;  label = raw
            elif raw.endswith("h"):
                duration_secs = int(raw[:-1]) * 3600;   label = raw
            elif raw.endswith("m"):
                duration_secs = int(raw[:-1]) * 60;     label = raw
            else:
                duration_secs = int(raw) * 60;           label = f"{raw}m"
        except ValueError:
            return f"Invalid duration '{args[0]}'. Examples: /pause 30m  /pause 2h  /pause 1d"

    until_ts  = time.time() + duration_secs
    until_str = datetime.fromtimestamp(until_ts).strftime("%H:%M")
    db_module.set_app_config("trade_pause_until", str(until_ts))
    return (
        f"Trading paused for {label} (until {until_str})\n"
        f"Use /resume to lift the pause early."
    )


async def cmd_resume(args: list) -> str:
    db_module.set_app_config("trade_pause_until", "0")
    # Re-arm the give-back guard from now. Without this, resuming after a
    # give-back halt is a no-op: the day's peak is already spent, so the guard
    # re-trips on the very next close.
    try:
        from forex_trader.core.core_risk_governor import rearm_giveback_guard
        rearm_giveback_guard()
    except Exception:
        pass
    rs        = db_module.get_risk_settings()
    auto_exec = bool(rs.get("auto_execute_signals", 0))
    return (
        "Trading resumed.\n"
        f"Auto-execute is {'ON' if auto_exec else 'OFF (enable it in Settings)'}."
    )


async def cmd_risk(args: list, bridge: Any) -> str:
    if not args:
        rs      = db_module.get_risk_settings()
        current = float(rs.get("risk_per_trade_pct", 0.5))
        return f"Current risk per trade: *{current}%*\nUsage: /risk 1.5"

    try:
        pct = float(args[0].rstrip("%"))
    except ValueError:
        return f"Invalid value '{args[0]}'. Usage: /risk 1.5"

    if not (0.1 <= pct <= 10):
        return f"Risk must be between 0.1% and 10%. Got {pct}%."

    db_module.update_risk_settings({"risk_per_trade_pct": pct})

    try:
        account = await bridge.get_account()
        if account and float(account.get("balance") or 0) > 0:
            bal = float(account["balance"])
        else:
            bal = float(get_sim_account().get("balance", 1000))
        risk_amt = bal * (pct / 100)
        return (
            f"Risk per trade set to *{pct}%*\n"
            f"At current balance (${bal:,.2f}) that's ~${risk_amt:.2f} per trade."
        )
    except Exception:
        return f"Risk per trade set to *{pct}%*"


_STRATEGY_ALIASES = {
    "scale_out":            STRATEGY_SCALE_OUT,
    "scale":                STRATEGY_SCALE_OUT,
    "be_runner":            STRATEGY_BE_RUNNER,
    "runner":               STRATEGY_BE_RUNNER,
    "be":                   STRATEGY_BE_RUNNER,
    "trail_stop":           STRATEGY_TRAIL_STOP,
    "trail":                STRATEGY_TRAIL_STOP,
    "trailing":             STRATEGY_TRAIL_STOP,
    "protected_scale":      STRATEGY_PROTECTED_SCALE,
    "protected":            STRATEGY_PROTECTED_SCALE,
    "ps":                   STRATEGY_PROTECTED_SCALE,
    "conservative":         STRATEGY_CONSERVATIVE,
    "cons":                 STRATEGY_CONSERVATIVE,
    "no_sl_scale":          STRATEGY_NO_SL_SCALE,
    "no_sl":                STRATEGY_NO_SL_SCALE,
    "nosl":                 STRATEGY_NO_SL_SCALE,
    "fixed_rr":             STRATEGY_FIXED_RR,
    "frr":                  STRATEGY_FIXED_RR,
    "conservative_trial":   STRATEGY_CONSERVATIVE_TRIAL,
    "cons_trial":           STRATEGY_CONSERVATIVE_TRIAL,
    "ct":                   STRATEGY_CONSERVATIVE_TRIAL,
    "trial":                STRATEGY_CONSERVATIVE_TRIAL,
    "scalp_runner":         STRATEGY_SCALP_RUNNER,
    "scalp":                STRATEGY_SCALP_RUNNER,
    "signal_climber":       STRATEGY_SIGNAL_CLIMBER,
    "climber":              STRATEGY_SIGNAL_CLIMBER,
    "ladder":               STRATEGY_SIGNAL_CLIMBER,
    "sc":                   STRATEGY_SIGNAL_CLIMBER,
    "reversal_runner":        STRATEGY_REVERSAL_RUNNER,
    "rr":                 STRATEGY_REVERSAL_RUNNER,
    "rvr":                  STRATEGY_REVERSAL_RUNNER,
    "adaptive_runner":      STRATEGY_ADAPTIVE_RUNNER,
    "adaptive":             STRATEGY_ADAPTIVE_RUNNER,
    "ar":                   STRATEGY_ADAPTIVE_RUNNER,
    "adaptive_runner_2":    STRATEGY_ADAPTIVE_RUNNER_2,
    "adaptive2":            STRATEGY_ADAPTIVE_RUNNER_2,
    "ar2":                  STRATEGY_ADAPTIVE_RUNNER_2,
}


async def cmd_strategy(args: list) -> str:
    if not args:
        rs      = db_module.get_risk_settings()
        current = STRATEGY_NAMES.get(rs.get("trade_strategy", STRATEGY_SCALE_OUT), "?")
        return (
            f"Current strategy: *{current}*\n\n"
            "Available:\n"
            "`scale_out` — Scale Out + Breakeven\n"
            "`be_runner` — Breakeven Runner\n"
            "`trail_stop` — Trailing Stop\n"
            "`protected_scale` — Protected Scale\n"
            "`conservative` — Conservative\n"
            "`no_sl_scale` — No-SL Scale Out\n"
            "`conservative_trial` — Conservative Trial (ct)\n"
            "`scalp_runner` — Scalp Runner (scalp)\n"
            "`signal_climber` — Signal Climber (climber / ladder / sc)\n"
            "`reversal_runner` — Reversal Runner (rr / rvr) — SL widened min(4×, 20pt), "
            "signal's own TP ladder\n"
            "`adaptive_runner` — Adaptive Runner (adaptive / ar) — SL widened like GD VIP "
            "Runner but capped at 50% of the signal's final TP distance; backtested "
            "+$400/PF 1.80/5.8% max DD on 226 real Gold Diggers VIP/GD2 signals "
            "(2026-07-15), lowest drawdown of any strategy tested there\n"
            "`adaptive_runner_2` — Adaptive Runner 2 (adaptive2 / ar2) — fixed 10pt SL "
            "(not derived from the signal at all), Reversal Runner's back-loaded close "
            "schedule, BE at TP2 then trails to the midpoint of the two TPs before "
            "the one just hit (not the single previous TP price) — untested judgment "
            "call, not backtested"
        )

    key      = args[0].lower()
    strategy = _STRATEGY_ALIASES.get(key)
    if not strategy:
        return (
            f"Unknown strategy `{args[0]}`.\n"
            "Options: `scale_out`  `be_runner`  `trail_stop`  `protected_scale`"
            "  `conservative`  `no_sl_scale`  `conservative_trial`  `scalp_runner`"
            "  `signal_climber`  `reversal_runner`  `adaptive_runner`  `adaptive_runner_2`"
        )
    db_module.update_risk_settings({"trade_strategy": strategy, "display_strategy_id": strategy})
    name = STRATEGY_NAMES.get(strategy, strategy)
    return f"Strategy changed to: *{name}*\nExisting open trades are not affected."


async def cmd_dpm_on(args: list) -> str:
    db_module.update_risk_settings({"dpm_enabled": 1})
    return "*DPM enabled* — Dynamic Position Management is now ON."


async def cmd_dpm_off(args: list) -> str:
    db_module.update_risk_settings({"dpm_enabled": 0})
    return "*DPM disabled* — Dynamic Position Management is now OFF."


async def cmd_ime_on(args: list) -> str:
    db_module.update_risk_settings({"immediate_market_entry": 1})
    return "*IME enabled* — Immediate Signal Entry is now ON."


async def cmd_ime_off(args: list) -> str:
    db_module.update_risk_settings({"immediate_market_entry": 0})
    return "*IME disabled* — Immediate Signal Entry is now OFF."

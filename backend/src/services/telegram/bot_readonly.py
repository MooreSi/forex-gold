"""Read-only/toggle Telegram bot commands -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._cmd_help/_cmd_balance/
_cmd_daily/_cmd_status/_cmd_trades/_cmd_pause/_cmd_resume/_cmd_risk/
_cmd_strategy/_cmd_dpm_on/_cmd_dpm_off/_cmd_ime_on/_cmd_ime_off, as part of
the core/engine.py migration series. See
docs/todo/refactor/core-bot-commands-readonly-migration/020-*.md.

None of these commands place, close, or modify a live order -- they only
read account/trade state or flip a risk-settings/app-config flag.

`_handle_bot_command` (the dispatcher) is NOT touched by this pack -- it
keeps calling `self._cmd_*` in engine.py unmodified until a future
integration pass rewires the whole dispatcher once all three bot-command
packs are done.
"""
from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core.core_fees_sizing import pnl
from forex_trader.core.core_sim_account import get_sim_account
from forex_trader.core.core_tp_trigger_tracking import last_closed_tp
from backend.src.services.analytics.reporting import get_open_trades
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER, STRATEGY_TRAIL_STOP,
    STRATEGY_PROTECTED_SCALE, STRATEGY_CONSERVATIVE, STRATEGY_NO_SL_SCALE,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SCALP_RUNNER, STRATEGY_SIGNAL_CLIMBER,
    STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
    STRATEGY_NAMES,
)


async def cmd_help(args: list) -> str:
    return (
        "*FOREX Trader — Bot Commands*\n\n"
        "/balance — Account balance, equity & open P&L\n"
        "/daily — Daily summary: P&L, closed trades & account\n"
        "/status — System status & current settings\n"
        "/trades — List all open trades\n"
        "/pause [30m|2h|1d] — Pause auto-trading\n"
        "/resume — Resume auto-trading\n"
        "/close all — Close all open trades\n"
        "/close `<ticket>` — Close a specific trade\n"
        "/strategy `<name>` — Change strategy:\n"
        "    `scale_out`  `be_runner`  `trail_stop`  `protected_scale`  `conservative`  `no_sl_scale`  `conservative_trial` (alias: `ct`)  `scalp_runner`  `signal_climber` (alias: `climber`)  `reversal_runner` (alias: `rr`)  `adaptive_runner` (alias: `adaptive`)  `adaptive_runner_2` (alias: `ar2`)\n"
        "/risk `<pct>` — Set risk per trade (e.g. `/risk 1.5`)\n"
        "/marketbuy — Buy XAUUSD at current market price\n"
        "/marketsell — Sell XAUUSD at current market price\n"
        "/dpmon / /dpmoff — Enable or disable Dynamic Position Management\n"
        "/imeon / /imeoff — Enable or disable Immediate Signal Entry\n"
        "/activate — Activate latest pending signal\n"
        "/report — Send performance report email now\n"
        "/restartbridge — Restart the MT5 bridge connection\n"
        "/restartapp — Restart the FOREX Trader app (applies code changes remotely)\n"
        "/switchlive — Switch app + MT5 bridge to live account\n"
        "/switchdemo — Switch app + MT5 bridge to demo account\n"
        "/headless on|off — Run without the web UI (or restore it) and restart to apply\n"
        "/help — Show this message"
    )


async def cmd_balance(args: list, bridge: Any) -> str:
    account = await bridge.get_account()
    if account and float(account.get("balance") or 0) > 0:
        bal  = float(account.get("balance", 0))
        eq   = float(account.get("equity", 0))
        fm   = float(account.get("margin_free", 0))
        mode = "MT5 Live" if db_module.get_app_config("account_env") == "live" else "MT5 Demo"
    else:
        acc  = get_sim_account()
        bal  = float(acc.get("balance", 0))
        eq   = bal
        fm   = bal
        mode = "Simulation"

    open_trades = get_open_trades()
    tick        = await bridge.get_tick()
    open_pnl    = 0.0
    if tick:
        for t in open_trades:
            open_pnl += pnl(
                t["direction"], float(t["entry_price"]),
                tick.bid if t["direction"] == "BUY" else tick.ask,
                float(t["remaining_lots"]),
            )

    lines = [
        f"*XAUUSD FOREX Trader — {mode}*",
        "",
        f"Balance:     ${bal:,.2f}",
        f"Equity:      ${eq:,.2f}",
        f"Free Margin: ${fm:,.2f}",
    ]
    if open_trades:
        sign = "+" if open_pnl >= 0 else ""
        n    = len(open_trades)
        lines.append(f"Open P&L:    {sign}${open_pnl:.2f}  ({n} trade{'s' if n != 1 else ''})")
    return "\n".join(lines)


async def cmd_daily(args: list, bridge: Any) -> str:
    """Send a daily summary: account state, today's closed trades and open positions."""
    today_str  = datetime.now().strftime("%a %d %b %Y")
    day_cutoff = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()

    # ── Account balance/equity ────────────────────────────────────────────
    mode = "Simulation"
    bal  = 0.0
    eq   = 0.0

    try:
        account = await bridge.get_account()
        if account and float(account.get("balance") or 0) > 0:
            bal  = float(account["balance"])
            eq   = float(account.get("equity", bal))
            env  = db_module.get_app_config("account_env") or "demo"
            mode = "MT5 Live" if env == "live" else "MT5 Demo"
        else:
            raise ValueError("no MT5 balance")
    except Exception:
        sim = get_sim_account()
        bal  = float(sim.get("balance", 0))
        eq   = bal

    # ── Live open P&L ──────────────────────────────────────────────────────
    open_trades = get_open_trades()
    tick        = await bridge.get_tick()
    open_pnl    = 0.0
    if tick and open_trades:
        open_pnl = sum(
            pnl(
                t["direction"], float(t["entry_price"]),
                tick.bid if t["direction"] == "BUY" else tick.ask,
                float(t["remaining_lots"]),
            )
            for t in open_trades
        )

    # ── Today's closed trades from DB ─────────────────────────────────────
    # close_time is always a proper UTC epoch (time.time() at moment of close)
    with db_module.db() as conn:
        today_closed = [
            db_module.row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time >= ? "
                "ORDER BY close_time DESC",
                (day_cutoff,),
            ).fetchall()
        ]

    # P&L uses mt5_profit when available (accurate broker figure), else net_pnl
    def _trade_pnl(t: dict) -> float:
        raw = t.get("mt5_profit")
        return float(raw if raw is not None else t.get("net_pnl", 0))

    daily_pnls  = [_trade_pnl(t) for t in today_closed]
    daily_pnl   = sum(daily_pnls)
    daily_n     = len(daily_pnls)
    wins        = sum(1 for p in daily_pnls if p > 0)
    losses      = sum(1 for p in daily_pnls if p < 0)
    daily_wr    = round(wins / daily_n * 100) if daily_n else 0
    daily_best  = max(daily_pnls) if daily_pnls else 0.0
    daily_worst = min(daily_pnls) if daily_pnls else 0.0

    # ── Risk settings ──────────────────────────────────────────────────────
    rs       = db_module.get_risk_settings()
    dpm_on   = bool(rs.get("dpm_enabled", 0))
    strategy = ("DPM" if dpm_on
                else STRATEGY_NAMES.get(rs.get("trade_strategy", STRATEGY_SCALE_OUT), "?"))
    risk_pct = float(rs.get("risk_per_trade_pct", 0.5))
    # Escape underscore for Telegram Markdown v1
    strategy_esc = strategy.replace("_", "\\_")

    # ── Helper: compact exit-reason label ─────────────────────────────────
    def _is_sl(exit_reason: str) -> bool:
        r = (exit_reason or "").lower()
        return r.startswith("[sl") or r == "sl" or "stop" in r

    def _reason_lbl(exit_reason: str, trade_id: str = "") -> str:
        r = (exit_reason or "").lower()
        if _is_sl(r):
            ltp = last_closed_tp(trade_id) if trade_id else None
            return f"SL+TP{ltp}" if ltp is not None else "SL"
        if (r.startswith("[tp") or r.startswith("tp")
                or r in ("all_tps_hit", "mt5_sync_tp", "profit_close_target")):
            return "TP"
        if "manual" in r or r in ("mt5_close", "mt5_import"):
            return "manual"
        if "partial" in r or "scale" in r:
            return "partial"
        # Escape underscores so Markdown v1 doesn't treat them as italics
        return (exit_reason or "?")[:10].replace("_", ".")

    # ── Assemble message ───────────────────────────────────────────────────
    op_sign  = "+" if open_pnl  >= 0 else ""
    pnl_sign = "+" if daily_pnl >= 0 else ""

    lines: list[str] = [
        f"*Daily Summary — {today_str}*",
        f"_{mode}_",
        "",
        "*Account*",
        f"Balance:  ${bal:,.2f}",
        f"Equity:   ${eq:,.2f}",
    ]
    if open_trades:
        n = len(open_trades)
        lines.append(
            f"Open P&L: {op_sign}${open_pnl:.2f}  "
            f"({n} trade{'s' if n != 1 else ''})"
        )

    lines += ["", f"*Today's Closed ({daily_n})*"]

    if daily_n == 0:
        lines.append("No closed trades today.")
    else:
        lines += [
            f"Net P&L:  {pnl_sign}${daily_pnl:.2f}",
            f"Win rate: {daily_wr}%  ({wins}W / {losses}L)",
            f"Best: {'+' if daily_best  >= 0 else ''}${daily_best:.2f}   "
            f"Worst: {'' if daily_worst >= 0 else ''}${daily_worst:.2f}",
        ]

        if today_closed:
            lines += ["", "*Trade Log*"]
            for t in today_closed[:8]:
                direction = t.get("direction", "?")
                entry_px  = float(t.get("entry_price", 0))
                close_px  = float(t.get("close_price", 0))
                lots      = float(t.get("lot_size", 0))
                pnl_t     = _trade_pnl(t)
                rlbl      = _reason_lbl(t.get("exit_reason") or "", t.get("trade_id", ""))
                emoji     = "✅" if pnl_t >= 0 else "❌"
                sign_t    = "+" if pnl_t >= 0 else ""
                lines.append(
                    f"{emoji} {direction} {lots:.2f}L  "
                    f"${entry_px:.0f}→${close_px:.0f}  "
                    f"{sign_t}${pnl_t:.2f} [{rlbl}]"
                )
            if len(today_closed) > 8:
                lines.append(f"_...and {len(today_closed) - 8} more_")

    # Open trades detail
    if open_trades:
        lines += ["", f"*Open Trades ({len(open_trades)})*"]
        for t in open_trades[:5]:
            direction = t.get("direction", "?")
            entry_px  = float(t.get("entry_price", 0))
            lots      = float(t.get("remaining_lots", 0))
            sl        = t.get("stop_loss")

            pnl_t = (pnl(direction, entry_px,
                        tick.bid if direction == "BUY" else tick.ask, lots)
                     if tick else 0.0)
            p_sign = "+" if pnl_t >= 0 else ""

            held_secs = int(time.time() - float(t.get("open_time") or time.time()))
            held = (f"{held_secs // 60}m" if held_secs < 3600
                    else f"{held_secs // 3600}h {(held_secs % 3600) // 60}m")

            sl_str = f"  SL ${float(sl):.0f}" if sl is not None else ""
            lines.append(
                f"{direction} {lots:.2f}L @ ${entry_px:.2f}  "
                f"P&L: {p_sign}${pnl_t:.2f}  [{held}]{sl_str}"
            )
        if len(open_trades) > 5:
            lines.append(f"_...and {len(open_trades) - 5} more_")

    lines += ["", f"_Strategy: {strategy_esc}  |  Risk: {risk_pct}%_"]

    return "\n".join(lines)


async def cmd_status(args: list, bridge: Any, tg_reader: Optional[Any] = None) -> str:
    rs         = db_module.get_risk_settings()
    dpm_on     = bool(rs.get("dpm_enabled", 0))
    if dpm_on:
        strategy_name = "DPM"
    else:
        strategy_name = STRATEGY_NAMES.get(rs.get("trade_strategy", STRATEGY_SCALE_OUT), "?")
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
    # not our own keep-alive client connection to port 9000
    try:
        from backend.src.utils.os_utils import is_port_listening as _ipl
        _bridge_up = _ipl(9000)
    except Exception:
        _bridge_up = False
    bridge_line = "Connected" if _bridge_up else "NOT running"

    # Telegram channel status
    tg_lines: list[str] = []
    if tg_reader is not None:
        tg_status = tg_reader.get_status()
        auth      = tg_status.get("auth_state", "disconnected")
        for slot in tg_status.get("slots", []):
            slot_num  = slot.get("slot", "?")
            name      = slot.get("group_name") or slot.get("group_id")
            active    = slot.get("listener_active") or slot.get("poller_active")
            if name:
                state = "active" if active else "idle"
                tg_lines.append(f"  Slot {slot_num}: {name} ({state})")
            else:
                tg_lines.append(f"  Slot {slot_num}: not configured")
    else:
        auth = "not started"

    lines = [
        "*System Status*",
        "",
        f"Strategy:     {strategy_name}",
        f"Auto-execute: {'ON' if auto_exec else 'OFF'}",
        f"Risk/trade:   {risk_pct}%",
        f"Max trades:   {max_trades}",
        f"Open trades:  {open_count}",
        f"Trading:      {trade_line}",
        "",
        f"MT5 Bridge:   {bridge_line}",
        f"Telegram:     {auth}",
    ]
    if tg_lines:
        lines.extend(tg_lines)

    return "\n".join(lines)


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

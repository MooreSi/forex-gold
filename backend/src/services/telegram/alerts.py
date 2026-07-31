"""
Telegram bot alert sender.
Sends trade notifications via the Telegram Bot API (not Telethon).
"""

import logging
import time
from typing import Optional

import httpx

from forex_trader.core import database as db_module
from forex_trader.core.core_ea_templates import TEMPLATE_OVERRIDE_PREFIX as _TEMPLATE_OVERRIDE_PREFIX
from backend.src.utils.models import STRATEGY_NAMES, STRATEGY_SCALE_OUT

log = logging.getLogger(__name__)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _strategy_label(trade: dict) -> str:
    """Return the strategy display string, reflecting DPM and OOH overrides —
    same precedence as _monitor_loop's dispatch."""
    try:
        rs = db_module.get_risk_settings()
        if bool(rs.get("dpm_enabled", 0)):
            return "DPM"
        _, ooh_active = db_module.get_effective_strategy(rs)
        if ooh_active:
            ooh_strat = rs.get("ooh_strategy", "conservative") or "conservative"
            return f"OOH: {STRATEGY_NAMES.get(ooh_strat, ooh_strat)}"
    except Exception:
        pass
    strategy = trade.get("strategy", STRATEGY_SCALE_OUT)
    if strategy and strategy.startswith(_TEMPLATE_OVERRIDE_PREFIX):
        return f"Template: {strategy[len(_TEMPLATE_OVERRIDE_PREFIX):]}"
    return STRATEGY_NAMES.get(strategy, strategy)


def _node_label() -> str:
    """Which machine this alert is being sent from — Local Mac or Remote
    VPS — so it's visible which node actually executed a given signal.
    Determined by role (sync_server_enabled), not active_trader: by the
    time open_trade() has succeeded, the active-trader gate has already
    confirmed this node was the legitimate one to act, so this is purely
    identifying which physical machine that was."""
    try:
        if db_module.get_app_config("sync_server_enabled") == "1":
            return "Remote"
        return "Local"
    except Exception:
        return "Local"


def _md_esc(s) -> str:
    """Escape Markdown v1 special characters in dynamic field values.

    Telegram Markdown v1 treats * _ ` [ as markup delimiters. Unmatched ones
    (e.g. the underscore in 'manual_market') cause a 400 parse error. Escape
    them in any content that comes from the DB or user input, NOT in the bold
    header markers we place deliberately.
    """
    return str(s).replace("_", r"\_").replace("*", r"\*").replace("`", r"\`").replace("[", r"\[").replace("]", r"\]")


def _held(activated_at, close_time) -> str:
    """Format a duration in seconds as '4m', '1h 22m', etc."""
    try:
        secs = int(float(close_time) - float(activated_at))
        if secs < 60:
            return f"{secs}s"
        mins = secs // 60
        if mins < 60:
            return f"{mins}m"
        return f"{mins // 60}h {mins % 60}m"
    except Exception:
        return "?"


def _tp_lines(parsed: dict, per_row: int = 3) -> str:
    """Return TP values grouped per_row per line, e.g. 'TP1: X  TP2: Y  TP3: Z'."""
    tps = [(i, parsed.get(f"tp{i}")) for i in range(1, 9) if parsed.get(f"tp{i}") is not None]
    if not tps:
        return ""
    rows = []
    for start in range(0, len(tps), per_row):
        chunk = tps[start:start + per_row]
        rows.append("  ".join(f"TP{n}: {v}" for n, v in chunk))
    return "\n".join(rows)


def _tp_lines_single(parsed: dict) -> str:
    """Return each TP on its own line."""
    lines = []
    for i in range(1, 9):
        v = parsed.get(f"tp{i}")
        if v is not None:
            lines.append(f"TP{i}: {v}")
    return "\n".join(lines)


def _close_label(reason: str) -> str:
    labels = {
        "SL":                  "🛑 Stop Loss Hit",
        "TP":                  "🎯 Take Profit Hit",
        "manual_close":        "🖐 Manually Closed",
        "profit_close_target": "🎯 Profit Target Hit",
        "all_tps_hit":         "🏆 All TPs Hit",
        "MT5_close":           "⚙️ MT5 Auto-Close",
        "MT5_sync_TP":         "🎯 MT5 TP (synced)",
    }
    return labels.get(reason, _md_esc(reason.replace("_", " ").title()))


BOT_COMMANDS = [
    ("balance",           "Account balance, equity & open P&L"),
    ("daily",             "Daily summary: P&L, trades & account"),
    ("status",            "System status, strategy & settings"),
    ("trades",            "List all open trades with P&L"),
    ("pause",             "Pause auto-trading — e.g. /pause 30m"),
    ("resume",            "Resume auto-trading"),
    ("close",             "Close trade — /close all  or  /close <ticket>"),
    ("strategy",          "Change strategy — e.g. /strategy trail_stop"),
    ("risk",              "Set risk per trade — e.g. /risk 1.5"),
    ("marketbuy",         "Buy XAUUSD at current market price"),
    ("marketsell",        "Sell XAUUSD at current market price"),
    ("dpmon",             "Enable Dynamic Position Management"),
    ("dpmoff",            "Disable Dynamic Position Management"),
    ("imeon",             "Enable Immediate Signal Entry"),
    ("imeoff",            "Disable Immediate Signal Entry"),
    ("activate",          "Activate latest pending signal"),
    ("report",            "Send a performance report email now"),
    ("restartbridge",     "Restart the MT5 bridge connection"),
    ("help",              "Show all available commands"),
]


async def register_commands(token: str) -> None:
    """Register bot commands with Telegram so they appear in the slash-command menu."""
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(
                f"https://api.telegram.org/bot{token}/setMyCommands",
                json={"commands": [{"command": c, "description": d} for c, d in BOT_COMMANDS]},
            )
    except Exception as e:
        log.warning("Failed to register bot commands: %s", e)


async def send_message(text: str, trade_id: Optional[str] = None, event_type: str = "") -> bool:
    if not text:
        # fmt_sl_moved() returns "" for a non-breakeven trail move (noise the
        # user doesn't want) — nothing to send, and not worth logging as an
        # attempt.
        return False
    cfg = db_module.get_telegram_config()
    if not cfg.get("enabled") or not cfg.get("bot_token_enc") or not cfg.get("chat_id"):
        return False
    token   = cfg["bot_token_enc"]
    chat_id = cfg["chat_id"]
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json={
                "chat_id":    chat_id,
                "text":       text,
                "parse_mode": "Markdown",
            })
            ok = r.status_code == 200
            db_module.log_telegram_event(
                event_type, trade_id, "sent" if ok else "failed",
                None if ok else r.text[:200],
            )
            return ok
    except Exception as e:
        log.warning("Telegram send failed: %s", e)
        db_module.log_telegram_event(event_type, trade_id, "error", str(e)[:200])
        return False


# ── Formatters ────────────────────────────────────────────────────────────────

def fmt_signal(
    parsed: dict,
    channel_name: str,
    executed: bool,
    exec_lot=None,
    exec_price=None,
    skip_reason: str = "",
    strategy_name: str = "",
    ai_confidence: Optional[float] = None,
    ai_reasoning: str = "",
) -> str:
    direction = parsed.get("direction", "?")
    header    = "🤖 AUTO-EXECUTED" if executed else "📩 Signal Detected"

    tp_row = _tp_lines(parsed, per_row=3)

    lines = [
        f"*{header}: XAUUSD {direction}*",
        f"Channel: {_md_esc(channel_name)}",
        f"Entry: {parsed.get('entry_low')} – {parsed.get('entry_high')}",
        f"SL: {parsed.get('stop_loss')}",
    ]
    if tp_row:
        lines.append(tp_row)

    if executed and exec_lot is not None:
        price_str = f"~{exec_price}" if exec_price else "market"
        lines.append(f"✅ AUTO-EXECUTED {direction} {exec_lot} lots @ {price_str}  (MT5 order placed)")
        if strategy_name:
            lines.append(f"Strategy: {_md_esc(strategy_name)}")
            lines.append(f"Node: {_node_label()}")
    else:
        reason_text = skip_reason or "Auto-execution is OFF — activate manually in the dashboard."
        lines.append(_md_esc(reason_text))

    # AI-fallback note: included inline so the user receives ONE message, not two.
    if ai_confidence is not None:
        lines.append(
            f"_⚠️ AI-recovered (confidence {ai_confidence:.0%}) — "
            f"deterministic parser missed this format_"
        )
        if ai_reasoning:
            lines.append(f"_{_md_esc(ai_reasoning)}_")

    return "\n".join(lines)


def fmt_trade_open(trade: dict, tick, commentary: dict) -> str:
    direction     = trade.get("direction", "?")
    strategy_name = _strategy_label(trade)
    tg_source     = trade.get("tg_source", "")
    entry         = trade.get("entry_price")
    tp_block      = _tp_lines_single(trade)
    spread_line   = f"Spread: {tick.spread_points:.0f} pts" if tick else ""

    lines = [
        "XAUUSD — Trade Opened",
        f"Direction: {direction}",
        f"MT5 Ticket: {trade.get('mt5_ticket', '—')}",
        f"Entry: {entry}  (range {trade.get('entry_low')}–{trade.get('entry_high')})",
        f"Lot: {trade.get('lot_size')}",
        f"SL: {trade.get('stop_loss')}",
    ]
    if tp_block:
        lines.append(tp_block)
    if spread_line:
        lines.append(spread_line)
    lines.append(f"Strategy: {_md_esc(strategy_name)}")
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    # Executed by the companion MQL5 EA (native OnTick management inside the
    # MT5 terminal, no polling) vs this app's own Python-side polling — worth
    # surfacing per-trade since only portable strategies with a healthy,
    # connected EA ever get handed off; everything else stays Python-managed.
    lines.append(f"Executed via: {'EA' if trade.get('managed_by') == 'ea' else 'Python'}")
    lines.append(f"Node: {_node_label()}")
    return "\n".join(lines)


def fmt_instant_followup(instant_trade: dict, parsed: dict, channel_name: str) -> str:
    """Format a 'Trade Updated' notification for an instant entry that received full SL/TP."""
    direction     = instant_trade.get("direction", "?")
    ticket        = instant_trade.get("mt5_ticket", "—")
    entry         = instant_trade.get("entry_price", "?")
    lot           = instant_trade.get("lot_size", "?")
    strategy_name = _strategy_label(instant_trade)
    tp_block      = _tp_lines_single(parsed)
    sl_px         = float(parsed.get("stop_loss", 0))
    entry_low     = parsed.get("entry_low", entry)
    entry_high    = parsed.get("entry_high", entry)

    lines = [
        "*XAUUSD — Trade Updated*",
        f"Direction: {direction}",
        f"MT5 Ticket: {ticket}",
        f"Entry: {entry}  (range {entry_low}–{entry_high})",
        f"Lot: {lot}",
        f"SL: ${sl_px:.2f}",
    ]
    if tp_block:
        lines.append(tp_block)
    lines.append(f"Strategy: {_md_esc(strategy_name)}")
    lines.append(f"Channel: {_md_esc(channel_name)}")
    # Same "Executed via" line as fmt_trade_open() — worth knowing on a
    # follow-up too, since the SL/TP just applied here either went straight
    # into the EA's own on-tick management or is now polled by Python,
    # exactly the same portable-strategy/healthy-EA condition as at open time.
    lines.append(f"Executed via: {'EA' if instant_trade.get('managed_by') == 'ea' else 'Python'}")
    lines.append(f"Node: {_node_label()}")
    return "\n".join(lines)


def fmt_sl_moved(trade: dict, tp_cleared_num: int, new_sl: float) -> str:
    direction     = trade.get("direction", "?")
    entry_price   = float(trade.get("entry_price", 0))
    strategy_name = _strategy_label(trade)
    tg_source     = trade.get("tg_source", "")
    ticket        = trade.get("mt5_ticket", "—")

    is_breakeven = abs(new_sl - entry_price) < 0.01
    if not is_breakeven:
        # Only the move-to-entry (breakeven) event is worth a message — every
        # later trail step is noise the user doesn't want repeated per-trade.
        return ""
    header = f"Breakeven Locked — XAUUSD {direction}"
    # tp_cleared_num <= 0 means the caller couldn't identify which TP
    # triggered this (e.g. a source that never reports one) — omit the
    # "TPn cleared" phrase rather than showing a misleading "TP0".
    move_label = (
        f"TP{tp_cleared_num} cleared → SL moved to entry {new_sl} (breakeven)"
        if tp_cleared_num > 0
        else f"SL moved to entry {new_sl} (breakeven)"
    )

    lines = [
        f"*{header}*",
        f"MT5 Ticket: {ticket}",
        move_label,
        f"Strategy: {_md_esc(strategy_name)}",
    ]
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    lines.append(f"Node: {_node_label()}")
    return "\n".join(lines)


def fmt_tp_hit(
    trade: dict,
    tp_num: int,
    tp_price: float,
    lots_closed: float,
    partial_pnl: float,
) -> str:
    direction     = trade.get("direction", "?")
    strategy_name = _strategy_label(trade)
    tg_source     = trade.get("tg_source", "")
    ticket        = trade.get("mt5_ticket", "—")
    remaining     = max(0.0, float(trade.get("remaining_lots", 0)) - lots_closed)
    pnl_sign      = "+" if partial_pnl >= 0 else ""

    lines = [
        f"*TP{tp_num} Hit — XAUUSD {direction}*",
        f"MT5 Ticket: {ticket}",
        f"TP{tp_num} price: ${tp_price:.2f}",
        f"Lots closed: {lots_closed:.2f}  |  Remaining: {remaining:.2f}",
        f"Partial P&L: ${pnl_sign}{partial_pnl:.2f}",
        f"Strategy: {_md_esc(strategy_name)}",
    ]
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    lines.append(f"Node: {_node_label()}")
    return "\n".join(lines)


def fmt_mt5_partial_close(
    trade: dict,
    lots_closed: float,
    close_price: float,
    remaining_lots: float,
    partial_profit: float,
    reason: str,
) -> str:
    direction     = trade.get("direction", "?")
    strategy_name = _strategy_label(trade)
    tg_source     = trade.get("tg_source", "")
    ticket        = trade.get("mt5_ticket", "—")
    pnl_sign      = "+" if partial_profit >= 0 else ""
    reason_label  = _close_label(reason)

    lines = [
        f"*XAUUSD Partial Close — {direction}*",
        reason_label,
        f"MT5 Ticket: {ticket}",
        f"Lots closed: {lots_closed:.2f}  |  Remaining: {remaining_lots:.2f}",
        f"Close price: ${close_price:.2f}",
        f"Partial P&L: ${pnl_sign}{partial_profit:.2f}",
        f"Strategy: {_md_esc(strategy_name)}",
    ]
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    lines.append(f"Node: {_node_label()}")
    return "\n".join(lines)


def fmt_trade_close(
    trade: dict,
    result: dict,
    commentary: dict,
    account: Optional[dict] = None,
    last_tp: Optional[int] = None,
) -> str:
    direction     = trade.get("direction", "?")
    strategy_name = _strategy_label(trade)
    tg_source     = trade.get("tg_source", "")
    ticket        = trade.get("mt5_ticket", "—")
    lot_size      = trade.get("lot_size", "?")
    entry_price   = float(trade.get("entry_price", 0))
    close_price   = float(result.get("close_price") or trade.get("close_price") or 0)
    _mt5_p = trade.get("mt5_profit")
    profit = float(_mt5_p if _mt5_p is not None else (trade.get("net_pnl") or result.get("net_pnl") or 0))
    reason        = result.get("reason", trade.get("exit_reason", "?"))
    reason_label  = _close_label(reason)
    if reason == "SL" and last_tp is not None:
        reason_label = f"🛑 Stop Loss Hit (after TP{last_tp})"
    pnl_sign      = "+" if profit >= 0 else ""
    held_str      = _held(trade.get("activated_at"), trade.get("close_time") or time.time())
    pnl_emoji     = "✅" if profit >= 0 else "❌"

    lines = [
        f"*XAUUSD Trade Closed {pnl_emoji}*",
        reason_label,
        "",
        f"Direction: {direction}  |  Lots: {lot_size}  |  Held: {held_str}",
        f"MT5 Ticket: {ticket}",
        f"Entry: ${entry_price:.2f}  →  Exit: ${close_price:.2f}",
        "",
        f"Profit: ${pnl_sign}{profit:.2f}",
    ]

    if account:
        bal  = float(account.get("balance",     0) or 0)
        eq   = float(account.get("equity",      0) or 0)
        free = float(account.get("margin_free", 0) or 0)
        lines += [
            "",
            f"Balance:     ${bal:,.2f}",
            f"Equity:      ${eq:,.2f}",
            f"Free Margin: ${free:,.2f}",
        ]

    lines += [
        "",
        f"Strategy: {_md_esc(strategy_name)}",
    ]
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    lines.append(f"Node: {_node_label()}")

    return "\n".join(lines)

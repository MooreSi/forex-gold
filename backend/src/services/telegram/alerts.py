"""
Telegram bot alert sender.
Sends trade notifications via the Telegram Bot API (not Telethon).
"""

import logging
import time
from typing import Optional

import httpx

from backend.src.config import is_debug as _is_debug
from backend.src.db import database as db_module
from backend.src.services.broker.ea_templates import TEMPLATE_OVERRIDE_PREFIX as _TEMPLATE_OVERRIDE_PREFIX
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


# The bot is button-driven now (core_bot_panel): the previous 19-command
# list is all still reachable, but as panel buttons rather than typed
# commands, so only these three are registered in Telegram's slash menu.
BOT_COMMANDS = [
    ("panel",             "Open the control panel"),
    ("status",            "System status, strategy & settings"),
    ("help",              "How to use the bot"),
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


async def send_message(text: str, trade_id: Optional[str] = None, event_type: str = "",
                       reply_markup: Optional[dict] = None) -> bool:
    if not text:
        # fmt_sl_moved() returns "" for a non-breakeven trail move (noise the
        # user doesn't want) — nothing to send, and not worth logging as an
        # attempt.
        return False
    if _is_debug():
        # Debug mode makes zero outbound requests; the alert is still visible
        # in the log so the demo shows what WOULD have been sent.
        log.info("[debug] telegram alert suppressed (%s): %.200s", event_type or "-", text)
        return True
    cfg = db_module.get_telegram_config()
    if not cfg.get("enabled") or not cfg.get("bot_token_enc") or not cfg.get("chat_id"):
        return False
    token   = cfg["bot_token_enc"]
    chat_id = cfg["chat_id"]
    url     = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id":    chat_id,
        "text":       text,
        "parse_mode": "Markdown",
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await client.post(url, json=payload)
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
    mt5_ticket=None,
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
        # mt5_ticket=0 is a genuine value, not "missing" -- an EA Template
        # grid parent row is a deliberate placeholder (real fills land as
        # separate per-leg tickets, see fmt_grid_leg_fill below), so it's
        # shown as such rather than silently omitted.
        if mt5_ticket is not None:
            lines.append(
                f"MT5 Ticket: {mt5_ticket}" if mt5_ticket else
                "MT5 Ticket: pending (grid parent — legs report their own ticket on fill)"
            )
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

    # An EA Template trade's row is INSERTed as a placeholder (mt5_ticket=0,
    # entry_price=0.0) because the EA only stages the legs at open time -- the
    # real ticket and fill price arrive later, when a leg actually fills and
    # promotes the row. Printing those raw read as "MT5 Ticket: 0 / Entry: 0.0",
    # which looks like a broken trade rather than a staged one, so say what is
    # actually true and where the real numbers will come from. Same convention
    # as fmt_signal()'s grid-parent ticket line.
    ticket = trade.get("mt5_ticket")
    lines = [
        "XAUUSD — Trade Opened",
        f"Direction: {direction}",
        f"MT5 Ticket: {ticket}" if ticket else
        "MT5 Ticket: pending (template legs report their own ticket on fill)",
        f"Entry: {entry}  (range {trade.get('entry_low')}–{trade.get('entry_high')})"
        if entry else
        f"Entry: pending — legs staged across {trade.get('entry_low')}–{trade.get('entry_high')}",
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


def fmt_leg_fill(row: dict, leg_label: str, ticket, fill_price: float,
                 lots, is_first: bool) -> str:
    """A single leg of an EA Template trade went live at the broker — either
    an Anchor leg (immediate market fill, "-a<N>") or a Grid leg (a resting
    limit that has now filled, "-g<N>").

    Deliberately the same shape and detail as fmt_trade_open() (ticket,
    entry, lot, SL, TP ladder, strategy, channel, node) — a leg going live
    IS a trade execution and gets a full execution message, with
    `leg_label` ("Grid Leg 2" / "Anchor Leg 1") naming which leg it was.

    `is_first` distinguishes the leg that promoted the DB placeholder row
    (mt5_ticket=0 -> this ticket) from a later leg filling on the same
    original trade_id when cancel_pending is off. Only one DB row exists
    per template trade, so a later leg's fill is real (a genuine second
    broker position) but cannot be separately tracked here -- reported
    for visibility, with that limitation stated rather than hidden."""
    direction     = row.get("direction", "?")
    strategy_name = _strategy_label(row)
    tg_source     = row.get("tg_source", "")
    tp_block      = _tp_lines_single(row)

    lines = [
        f"*XAUUSD — Trade Opened ({leg_label})*" if is_first
        else f"*XAUUSD — Trade Opened ({leg_label}, additional leg)*",
        f"Direction: {direction}",
        f"MT5 Ticket: {ticket}",
        f"Entry: {fill_price}",
        f"Lot: {lots if lots is not None else row.get('lot_size')}",
        f"SL: {row.get('stop_loss')}",
    ]
    if tp_block:
        lines.append(tp_block)
    lines.append(f"Strategy: {_md_esc(strategy_name)}")
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    lines.append("Executed via: EA")  # template legs are only ever EA-managed
    lines.append(f"Node: {_node_label()}")
    if not is_first:
        lines.append(
            "_Note: a second concurrent broker position -- only the first "
            "leg to fill is tracked as this trade's DB row, so this "
            "leg is reported for visibility only._"
        )
    return "\n".join(lines)


def fmt_template_leg_note(row: dict, leg_label: str, title: str,
                          detail_lines: list) -> str:
    """A lifecycle event (TP hit / SL move / close) reported by the EA for a
    template leg that is NOT the broker position this trade's DB row tracks.

    Only one vantage_simulated_trades row exists per template trade, so
    nothing about such a leg can be written to it without corrupting the
    tracked position's lots or close state. The event is still real money
    at the broker, so it is reported rather than dropped -- clearly marked
    as an untracked sibling leg."""
    strategy_name = _strategy_label(row)
    tg_source     = row.get("tg_source", "")
    lines = [f"*XAUUSD — {leg_label} {title}*"]
    lines += [str(l) for l in detail_lines if l]
    lines.append(f"Strategy: {_md_esc(strategy_name)}")
    if tg_source:
        lines.append(f"Channel: {_md_esc(tg_source)}")
    lines.append(f"Node: {_node_label()}")
    lines.append(
        "_Note: a sibling template leg, not the position tracked by this "
        "trade's row -- reported for visibility, nothing recorded against "
        "the trade._"
    )
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


def _pips_and_rr(trade: dict, profit: float) -> tuple[Optional[float], Optional[float]]:
    """(total_pips, r_multiple) for a FINISHED trade, or (None, None) when
    the row can't support an honest figure. Final-close only -- deliberately
    not used by fmt_mt5_partial_close, where "total" isn't known yet.

    Pips are derived from realised MONEY, not from (exit - entry). That
    difference is load-bearing: a laddered trade banks most of its move in
    partial closes at prices the final exit never returns to, so the raw
    price move understates -- or inverts -- what was actually achieved.
    Confirmed on a live SELL that closed its last portion at exactly its
    entry (0 pips by price) having already realised +$43.29 through
    partials. Money already sums every partial and, for an EA Template
    grid, every leg.

    initial_risk is the preferred denominator because core_profit_sync
    recomputes it per FILLED leg (see its own comment) -- so it stays
    correct for a multi-leg grid whose row-level lot_size is only the
    promoting leg's, where profit / (lot_size * CONTRACT_SIZE) would
    overstate the move by roughly the leg count. Pips are then expressed in
    that same risk unit, which keeps the two numbers mutually consistent:
    R x risk_pips == total_pips, always.
    """
    from backend.src.utils.models import CONTRACT_SIZE
    from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD

    entry      = float(trade.get("entry_price") or 0)
    initial_sl = trade.get("initial_sl")
    init_risk  = trade.get("initial_risk")

    risk_pips = None
    if entry > 0 and initial_sl:
        risk_pips = abs(entry - float(initial_sl)) / PIPS_TO_PRICE_XAUUSD

    # Preferred path: dollar R against the per-leg initial risk, with pips
    # rendered in the same unit.
    if init_risk and float(init_risk) > 0:
        r = profit / float(init_risk)
        return ((r * risk_pips) if risk_pips else None), r

    # Fallback for rows predating initial_sl/initial_risk: money over the
    # row's own size. Correct for the single-leg trades that make up
    # essentially all of that history; R is only offered when a stop
    # distance is actually recorded.
    lots = float(trade.get("lot_size") or 0)
    if lots > 0:
        pips = (profit / (lots * CONTRACT_SIZE)) / PIPS_TO_PRICE_XAUUSD
        return pips, ((pips / risk_pips) if risk_pips else None)
    return None, None


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
    ticket        = trade.get("mt5_ticket") or None
    lot_size      = trade.get("lot_size", "?")
    entry_price   = float(trade.get("entry_price") or 0)
    close_price   = float(result.get("close_price") or trade.get("close_price") or 0)
    _mt5_p = trade.get("mt5_profit")
    profit = float(_mt5_p if _mt5_p is not None else (trade.get("net_pnl") or result.get("net_pnl") or 0))
    # entry_price == 0 means this row never got a real fill recorded against
    # it (an EA Template placeholder whose leg fill was never promoted, see
    # ea_bridge._promote_leg_fill). Reporting "Entry: $0.00" plus a P&L
    # derived from it produced wildly wrong figures in the close message
    # (confirmed live 2026-07-29: "Entry $0.00 -> Exit $4021.50, Profit
    # $-16086.00" on a 0.03-lot trade whose real loss was $15.63). Say the
    # entry is unknown and don't quote a P&L we can't stand behind.
    entry_known   = entry_price > 0
    profit_known  = entry_known or _mt5_p is not None
    reason        = result.get("reason", trade.get("exit_reason", "?"))
    reason_label  = _close_label(reason)
    if reason == "SL" and last_tp is not None:
        reason_label = f"🛑 Stop Loss Hit (after TP{last_tp})"
    pnl_sign      = "+" if profit >= 0 else ""
    held_str      = _held(trade.get("activated_at"), trade.get("close_time") or time.time())
    pnl_emoji     = ("✅" if profit >= 0 else "❌") if profit_known else "⚠️"

    lines = [
        f"*XAUUSD Trade Closed {pnl_emoji}*",
        reason_label,
        "",
        f"Direction: {direction}  |  Lots: {lot_size}  |  Held: {held_str}",
        f"MT5 Ticket: {ticket if ticket else 'unknown'}",
        (f"Entry: ${entry_price:.2f}  →  Exit: ${close_price:.2f}" if entry_known
         else f"Entry: unknown  →  Exit: ${close_price:.2f}"),
        "",
        (f"Profit: ${pnl_sign}{profit:.2f}" if profit_known
         else "Profit: unknown (no entry price recorded for this trade)"),
    ]

    # Total pips + realised risk:reward. Final close only -- the partial-close
    # message deliberately has no equivalent, since neither figure is settled
    # until the position is fully out. Only shown when profit itself is
    # trustworthy: with no entry price the same arithmetic that produced a
    # $-16086 "profit" on a 0.03-lot trade would produce a nonsense pip count.
    if profit_known:
        _pips, _r = _pips_and_rr(trade, profit)
        if _pips is not None:
            lines.append(f"Total pips: {'+' if _pips >= 0 else ''}{_pips:.1f}")
        if _r is not None:
            # Measured against the risk actually taken at open, so a full stop
            # reads ~-1R, a loss cut early reads better than -1R, and a winner
            # reads as its true multiple of the risk. Conventional "1 : X"
            # notation only for gains -- a ratio is meaningless on a loss, so
            # those show the signed R alone.
            _rr = (f"1 : {_r:.2f}  ({_r:.2f}R)" if _r >= 0 else f"{_r:.2f}R")
            lines.append(f"Risk:Reward: {_rr}")

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

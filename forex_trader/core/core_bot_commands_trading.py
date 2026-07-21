"""Trading-action Telegram bot commands -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._cmd_close/_cmd_activate/
_cmd_market_price_buy/_cmd_market_price_sell/_cmd_report, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-bot-commands-trading-migration/020-*.md.

Calls close_trade/open_manual_market_order/open_trade -- real MT5 order-
close/placement calls, unchanged from the original. This module places,
closes, or modifies no order itself; it only calls whatever `bridge` its
caller supplies, via those already-extracted functions.

`_handle_bot_command` (the dispatcher) is NOT touched by this pack -- it
keeps calling `self._cmd_*` in engine.py unmodified until a future
integration pass rewires the whole dispatcher once all three bot-command
packs are done.
"""
from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime
from typing import Any

from forex_trader.core import claude_ai
from forex_trader.core import database as db_module
from forex_trader.core import email_service
from forex_trader.core.core_close_trade import CloseTradeContext, close_trade, get_trading_balance
from forex_trader.core.core_fees_sizing import suggest_lot_size
from forex_trader.core.core_manual_market_order import open_manual_market_order
from forex_trader.core.core_mt5_performance import compute_mt5_performance
from forex_trader.core.core_open_trade import open_trade
from forex_trader.core.core_risk_governor import price_in_entry_range
from forex_trader.core.core_trade_reporting import get_open_trades
from forex_trader.core.models import STRATEGY_SCALE_OUT
from forex_trader.core.signal_parser import validate_signal

log = logging.getLogger(__name__)


async def cmd_close(args: list, bridge: Any, starting_balance: float = 1000.0) -> str:
    open_trades = get_open_trades()
    if not open_trades:
        return "No open trades to close."

    if not args:
        return "Usage: /close all  or  /close `<ticket>`"

    ctx = CloseTradeContext(bridge, starting_balance=starting_balance)

    if args[0].lower() == "all":
        n     = len(open_trades)
        lines = [f"Closing {n} open trade{'s' if n != 1 else ''}..."]
        total = 0.0
        for t in open_trades:
            try:
                result = await close_trade(t["trade_id"], "manual_close", ctx)
                pnl    = float(result.get("net_pnl", 0))
                cp     = float(result.get("close_price", 0))
                total += pnl
                sign   = "+" if pnl >= 0 else ""
                lines.append(
                    f"Closed {t['direction']} {t['lot_size']} lots @ ${cp:.2f}  P&L: {sign}${pnl:.2f}"
                )
            except Exception as e:
                lines.append(f"Failed {t.get('mt5_ticket', t['trade_id'][:8])}: {e}")
        sign = "+" if total >= 0 else ""
        lines.append(f"Done. Total P&L: {sign}${total:.2f}")
        return "\n".join(lines)

    # Close by MT5 ticket number
    try:
        ticket = int(args[0])
    except ValueError:
        return f"Invalid ticket '{args[0]}'. Use /close all  or  /close `<ticket number>`"

    trade = next((t for t in open_trades if t.get("mt5_ticket") == ticket), None)
    if not trade:
        return f"No open trade with ticket {ticket}."
    result = await close_trade(trade["trade_id"], "manual_close", ctx)
    pnl    = float(result.get("net_pnl", 0))
    cp     = float(result.get("close_price", 0))
    sign   = "+" if pnl >= 0 else ""
    return (
        f"Closed: {trade['direction']} {trade['lot_size']} lots @ ${cp:.2f}\n"
        f"P&L: {sign}${pnl:.2f}"
    )


async def cmd_activate(args: list, bridge: Any, starting_balance: float = 1000.0) -> str:
    with db_module.db() as conn:
        tg_sig = db_module.row_to_dict(conn.execute(
            "SELECT * FROM vantage_tg_signals WHERE status='new' ORDER BY parsed_at DESC LIMIT 1"
        ).fetchone())
    if not tg_sig:
        return "No pending signals to activate."

    errors = validate_signal(
        tg_sig["direction"], tg_sig["entry_low"], tg_sig["entry_high"],
        tg_sig["stop_loss"],
        tg_sig["tp1"], tg_sig["tp2"], tg_sig["tp3"], tg_sig["tp4"], tg_sig["tp5"],
        tg_sig.get("tp6"), tg_sig.get("tp7"), tg_sig.get("tp8"),
    )
    if errors:
        return f"Signal validation failed: {'; '.join(errors)}"

    rs        = db_module.get_risk_settings()
    strategy  = rs.get("trade_strategy", STRATEGY_SCALE_OUT)
    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "Telegram Bot", tg_sig["direction"],
             tg_sig["entry_low"], tg_sig["entry_high"], tg_sig["stop_loss"],
             tg_sig["tp1"], tg_sig["tp2"], tg_sig["tp3"], tg_sig["tp4"], tg_sig["tp5"],
             tg_sig.get("tp6"), tg_sig.get("tp7"), tg_sig.get("tp8"),
             f"Bot-activated from Telegram {tg_sig['tg_message_id']}",
             "active", time.time()),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='activated',signal_id=? WHERE tg_message_id=?",
            (signal_id, tg_sig["tg_message_id"]),
        )

    tick = await bridge.get_tick()
    if not tick:
        return f"Signal created — no live price available. Open the trade manually in the dashboard."

    # Gate: only enter within the entry zone
    _el, _eh = float(tg_sig["entry_low"]), float(tg_sig["entry_high"])
    _dir = tg_sig["direction"].upper()
    if not price_in_entry_range(_dir, _el, _eh, tick):
        cur_px = tick.ask if _dir == "BUY" else tick.bid
        _side  = "above" if _dir == "BUY" else "below"
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_signals SET status='pending' WHERE signal_id=?",
                (signal_id,),
            )
        return (
            f"Signal saved as pending — {_dir} price ${cur_px:.2f} is {_side} "
            f"the entry zone ${_el:.2f}–${_eh:.2f}. "
            f"The app will auto-activate when price returns to zone."
        )

    balance      = await get_trading_balance(bridge, starting_balance)
    entry_mid    = (tg_sig["entry_low"] + tg_sig["entry_high"]) / 2
    lot          = suggest_lot_size(entry_mid, tg_sig["stop_loss"], balance,
                                    float(rs.get("risk_per_trade_pct", 0.5)))
    strategy_lot = float(rs.get("strategy_lot_size", 0))
    if strategy_lot > 0:
        lot = strategy_lot

    result = await open_trade(
        bridge, signal_id=signal_id, direction=tg_sig["direction"],
        entry_low=tg_sig["entry_low"], entry_high=tg_sig["entry_high"],
        stop_loss=tg_sig["stop_loss"],
        tp1=tg_sig["tp1"], tp2=tg_sig["tp2"], tp3=tg_sig["tp3"],
        tp4=tg_sig["tp4"], tp5=tg_sig["tp5"],
        tp6=tg_sig.get("tp6"), tp7=tg_sig.get("tp7"), tp8=tg_sig.get("tp8"),
        lot_size=lot, tick=tick, strategy=strategy,
    )
    return (
        f"Activated!\n"
        f"{tg_sig['direction']} {lot} lots @ ~${result['entry_price']:.2f}\n"
        f"Trade ID: {result['trade_id']}"
    )


async def cmd_market_price_buy(args: list, bridge: Any, starting_balance: float = 1000.0) -> str:
    try:
        result = await open_manual_market_order(bridge, "BUY", starting_balance=starting_balance)
        ticket = result.get("mt5_ticket", "—")
        entry  = float(result.get("entry_price", 0))
        lot    = result.get("lot_size") or result.get("remaining_lots", "?")
        return (
            f"*BUY order placed*\n"
            f"Entry: ${entry:.2f}  |  Lots: {lot}\n"
            f"MT5 Ticket: {ticket}"
        )
    except Exception as e:
        from forex_trader.core import telegram_alerts
        return f"Order failed: {telegram_alerts._md_esc(str(e))}"


async def cmd_market_price_sell(args: list, bridge: Any, starting_balance: float = 1000.0) -> str:
    try:
        result = await open_manual_market_order(bridge, "SELL", starting_balance=starting_balance)
        ticket = result.get("mt5_ticket", "—")
        entry  = float(result.get("entry_price", 0))
        lot    = result.get("lot_size") or result.get("remaining_lots", "?")
        return (
            f"*SELL order placed*\n"
            f"Entry: ${entry:.2f}  |  Lots: {lot}\n"
            f"MT5 Ticket: {ticket}"
        )
    except Exception as e:
        from forex_trader.core import telegram_alerts
        return f"Order failed: {telegram_alerts._md_esc(str(e))}"


async def cmd_report(args: list, bridge: Any, cfg: dict) -> str:
    ecfg    = db_module.get_email_config()
    to_addr = ecfg.get("to_addr") or ecfg.get("smtp_user") or ""
    if not to_addr:
        return "No recipient email configured. Set it in Settings → Email Reports."

    try:
        perf = await compute_mt5_performance(bridge, 90)
    except Exception:
        perf = {}

    day_cutoff = datetime.now().replace(
        hour=0, minute=0, second=0, microsecond=0
    ).timestamp()
    with db_module.db() as conn:
        closed_today = [
            db_module.row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time>? ORDER BY close_time DESC",
                (day_cutoff,),
            ).fetchall()
        ]

    today_str   = datetime.now().strftime("%A, %d %B %Y")
    _balance    = float(perf.get("balance", 0) or 0)
    _dpnl       = float(perf.get("daily_pnl", 0) or 0)
    try:
        claude_analysis = await claude_ai.generate_daily_analysis(
            closed_today, _balance, _dpnl, cfg,
        )
    except Exception as _ae:
        log.warning("Daily Claude analysis (manual report) failed: %s", _ae)
        claude_analysis = None
    html    = email_service.build_daily_html(perf, closed_today, today_str,
                                              claude_analysis=claude_analysis)
    ok, err = await email_service.send_email(
        f"FOREX Trader Daily Report — {datetime.now().strftime('%Y-%m-%d')}", html, ecfg
    )
    if ok:
        return f"Report sent to {to_addr}."
    return f"Failed to send report: {err}"

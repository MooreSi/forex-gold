"""Trail Stop TP/SL strategy handler -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._handle_trail_stop, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-trail-stop-handler-migration/020-*.md.

Calls bridge.modify_order -- a real MT5 order-modification call, unchanged
from the original. This module modifies no order itself; it only calls
whatever `bridge` its caller supplies.

Takes `bridge` and a TPCache (pack 5) explicitly instead of self._bridge/
self._tp_cache. Reuses core_tp_trigger_tracking.get_triggered_tps (pack 5)
for the TP2-5 marker scan. No partial closes anywhere in this handler.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.positions.tp_tracking import TPCache, get_triggered_tps
from backend.src.utils.models import MAX_TP, Tick

log = logging.getLogger(__name__)


async def handle_trail_stop(trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache) -> None:
    """
    Trailing Stop strategy.

    Activation: price reaches TP1 (or sl_moved_to_be is already set from a
    previous loop cycle — the in-memory dict lags the DB by one cycle).

    On activation:
      - SL is IMMEDIATELY moved to entry price (breakeven).  This is the critical
        fix: previously, if bid - trail_dist < original_SL, nothing happened and
        the trade remained at full risk despite TP1 being cleared.

    Once active, every tick:
      - BUY:  new_sl = max(bid - trail_dist, entry_price)  — floor at breakeven
      - SELL: new_sl = min(ask + trail_dist, entry_price)  — ceiling at breakeven
      Only ever moves in the profitable direction (no reversal).

    TP2-TP5 markers are written so the TP chips in the UI reflect the actual
    price path, even though no partial closes happen.
    """
    direction     = trade["direction"].upper()
    current_sl    = float(trade["stop_loss"])
    mt5_ticket    = trade.get("mt5_ticket")
    trade_id      = trade["trade_id"]
    entry_price   = float(trade["entry_price"])
    tp1           = trade.get("tp1")

    # sl_moved_to_be may be stale if the DB was updated this same loop cycle;
    # also check the DB directly to avoid a one-cycle miss on first activation.
    trailing_active = bool(trade.get("sl_moved_to_be"))
    if not trailing_active:
        def _fetch_be_flag():
            with db_module.db() as conn:
                return conn.execute(
                    "SELECT sl_moved_to_be FROM vantage_simulated_trades WHERE trade_id=?",
                    (trade_id,)
                ).fetchone()
        row = await db_module.to_db_thread(_fetch_be_flag)
        if row and row[0]:
            trailing_active = True

    if tp1:
        tp1_f = float(tp1)
        tp1_cleared = (
            (direction == "BUY"  and tp1_f > entry_price and tick.bid >= tp1_f) or
            (direction == "SELL" and tp1_f < entry_price and tick.ask <= tp1_f)
        )
    else:
        tp1_cleared = False

    if not tp1_cleared and not trailing_active:
        return

    rs         = await db_module.to_db_thread(db_module.get_risk_settings)
    trail_dist = float(rs.get("trailing_stop_distance", 5.0))

    # ── First activation: immediately lock SL at entry (breakeven) ────────
    if not trailing_active:
        new_sl = entry_price          # risk-free from this tick onward
        should_update = True          # always write on first activation
        def _apply_activation():
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET sl_moved_to_be=1 WHERE trade_id=?",
                    (trade_id,),
                )
                conn.execute(
                    "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
                    " VALUES (?,?,?,?,?,?)",
                    (trade_id, time.time(), 0.0, float(tp1 or entry_price), 0.0, "TP1_TRAIL_START"),
                )
        await db_module.to_db_thread(_apply_activation)
        log.info("[trail_stop] %s TP1 cleared — SL locked at entry %.2f (trail_dist=%.2f)",
                 trade_id[:8], entry_price, trail_dist)
        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_sl_moved(trade, 1, entry_price),
            trade_id, "sl_moved_be",
        ))

    # ── Active trailing: follow price, floored at breakeven ───────────────
    else:
        if direction == "BUY":
            trail_sl  = round(tick.bid - trail_dist, 2)
            new_sl    = max(trail_sl, entry_price)   # never below entry
            should_update = new_sl > current_sl
        else:
            trail_sl  = round(tick.ask + trail_dist, 2)
            new_sl    = min(trail_sl, entry_price)   # never above entry (for SELL)
            should_update = new_sl < current_sl

    # ── Move SL in MT5 + local DB ─────────────────────────────────────────
    if should_update:
        if mt5_ticket:
            await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
        def _apply_sl():
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                    (new_sl, trade_id),
                )
        await db_module.to_db_thread(_apply_sl)
        log.debug("[trail_stop] %s SL → %.2f (bid=%.2f)", trade_id[:8], new_sl, tick.bid)

    # ── Record TP2-TP5 markers so UI chips reflect price path ─────────────
    triggered = await get_triggered_tps(tp_cache, trade_id)
    for tp_num in range(2, MAX_TP + 1):
        if tp_num in triggered:
            continue
        tp_val = trade.get(f"tp{tp_num}")
        if tp_val is None:
            continue
        tp_val_f = float(tp_val)
        if direction == "BUY"  and tp_val_f <= entry_price:
            continue
        if direction == "SELL" and tp_val_f >= entry_price:
            continue
        tp_hit = (direction == "BUY"  and tick.bid >= tp_val_f) or \
                 (direction == "SELL" and tick.ask <= tp_val_f)
        if tp_hit:
            def _mark_tp(tp_num=tp_num, tp_val_f=tp_val_f):
                with db_module.db() as conn:
                    conn.execute(
                        "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
                        " VALUES (?,?,?,?,?,?)",
                        (trade_id, time.time(), 0.0, tp_val_f, 0.0, f"TP{tp_num}_TRAIL_MARKER"),
                    )
            await db_module.to_db_thread(_mark_tp)
            log.info("[trail_stop] %s TP%d marked at %.2f (trailing)", trade_id[:8], tp_num, tp_val_f)

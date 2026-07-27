"""Scalp Runner TP/SL strategy handler -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._handle_scalp_runner, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-scalp-runner-handler-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

Takes `bridge`, a TPCache (pack 5), and `close_full_after_tps` (optional
injected callable, same deferred-dependency pattern as packs 17/19/21/22)
explicitly. Reuses core_tp_trigger_tracking.get_triggered_tps/
log_tp_wait_diagnostic/get_remaining_lots (pack 5), core_partial_close.
partial_close_trade (pack 9) -- note partial_close_trade has its own
independent "move SL to breakeven on TP1" logic (see 010's Notes), which
fires here too even though this handler's own phase 1 has no SL-move logic
(Scalp Runner deliberately waits for TP2 to move SL) -- so the DB row's SL
ends up at breakeven one phase earlier than this handler's own code implies.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from forex_trader.core.core_tp_trigger_tracking import (
    TPCache, get_triggered_tps, log_tp_wait_diagnostic, get_remaining_lots,
)
from backend.src.utils.models import Tick

log = logging.getLogger(__name__)

_CONSERVATIVE_TRAIL_PT = 3.0    # trailing stop distance after TP2
_SCALP_RUNNER_CLOSE_PCT = 0.50  # fraction of position closed at TP1


async def handle_scalp_runner(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Scalp Runner strategy -- three phases:

      SL   = fill ∓ 10 pts
      TP1  = fill ± 3 pts  →  close 50 % of position (SL untouched)
      TP2  = fill ± 4 pts  →  move SL to entry (breakeven); trailing starts
      After TP2: trail remaining 50 % with a fixed 3-pt stop,
                 floored at entry price (breakeven).
    """
    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"]) if trade.get("stop_loss") is not None else None
    mt5_ticket  = trade.get("mt5_ticket")
    trade_id    = trade["trade_id"]
    lot_size    = float(trade["lot_size"])
    triggered   = await get_triggered_tps(tp_cache, trade_id)

    _TRAIL_DIST = _CONSERVATIVE_TRAIL_PT
    _close_pct  = _SCALP_RUNNER_CLOSE_PCT  # 50%

    # ── Phase 1: wait for TP1 — partial close only, SL untouched ────────
    if 1 not in triggered:
        tp1_val = trade.get("tp1")
        if tp1_val is None:
            return
        tp1_f = float(tp1_val)
        _cur_px = tick.bid if direction == "BUY" else tick.ask
        tp1_hit = (direction == "BUY"  and tick.bid >= tp1_f) or \
                  (direction == "SELL" and tick.ask <= tp1_f)
        log_tp_wait_diagnostic(tp_cache, trade_id, "scalp_runner", direction, _cur_px, tp1_f, tp1_hit)
        if not tp1_hit:
            return

        log.info("[scalp_runner] %s TP1 @ %.2f — %.0f%% close",
                 trade_id[:8], tp1_f, _close_pct * 100)

        remaining     = await db_module.to_db_thread(get_remaining_lots, trade_id)
        lots_to_close = min(round(lot_size * _close_pct, 4), remaining)
        actual_price  = tp1_f
        if mt5_ticket and lots_to_close > 0:
            mt5_res = await bridge.partial_close(int(mt5_ticket), lots_to_close)
            if mt5_res.get("success"):
                actual_price = float(mt5_res.get("close_price", tp1_f))
                lots_to_close = float(mt5_res.get("lots_closed", lots_to_close))
            elif mt5_res.get("error") or mt5_res.get("success") is False:
                err_msg = mt5_res.get("error") or "partial close rejected"
                log.warning("[scalp_runner] MT5 TP1 partial close failed ticket=%s: %s",
                            mt5_ticket, mt5_res)
                asyncio.create_task(telegram_alerts.send_message(
                    f"*TP1 Partial Close Failed*\n"
                    f"Scalp Runner detected TP1 @ {tp1_f:.2f} "
                    f"but MT5 rejected the partial close.\n"
                    f"Error: {err_msg}\n"
                    f"Trade: {trade_id[:8]} | Ticket: {mt5_ticket}",
                    trade_id, "tp1_fail",
                ))
                return

        try:
            res = await partial_close_trade(trade_id, lots_to_close, actual_price, "TP1")
            triggered.add(1)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, 1, actual_price, lots_to_close,
                                           res.get("partial_pnl", 0)),
                trade_id, "tp1_hit",
            ))
            if res.get("auto_closed") and mt5_ticket:
                if close_full_after_tps:
                    asyncio.create_task(close_full_after_tps(trade_id, mt5_ticket, actual_price))
                return
        except Exception as exc:
            log.warning("[scalp_runner] TP1 close failed: %s", exc)
            return

        # No SL move here — breakeven now waits for TP2 (see Phase 2).
        return

    # ── Phase 2: TP1 hit, wait for TP2 — move SL to entry, start trail ──
    if 2 not in triggered:
        tp2_val = trade.get("tp2")
        if tp2_val is None:
            return
        tp2_f = float(tp2_val)
        _cur_px = tick.bid if direction == "BUY" else tick.ask
        tp2_hit = (direction == "BUY"  and tick.bid >= tp2_f) or \
                  (direction == "SELL" and tick.ask <= tp2_f)
        log_tp_wait_diagnostic(tp_cache, trade_id, "scalp_runner", direction, _cur_px, tp2_f, tp2_hit)
        if not tp2_hit:
            return

        triggered.add(2)
        log.info("[scalp_runner] %s TP2 @ %.2f — SL → entry, trailing starts",
                 trade_id[:8], tp2_f)

        if current_sl is not None:
            should_move = (direction == "BUY"  and entry_price > current_sl) or \
                          (direction == "SELL" and entry_price < current_sl)
            if should_move:
                if mt5_ticket:
                    await bridge.modify_order(int(mt5_ticket), sl=entry_price, tp=None)
                def _apply_be_sl():
                    with db_module.db() as conn:
                        conn.execute(
                            "UPDATE vantage_simulated_trades "
                            "SET stop_loss=?, sl_moved_to_be=1 WHERE trade_id=?",
                            (entry_price, trade_id),
                        )
                await db_module.to_db_thread(_apply_be_sl)
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_sl_moved(trade, 2, entry_price),
                    trade_id, "sl_moved_be",
                ))
        return

    # ── Phase 3: TP2 hit — trail remaining 50 % with fixed 3-pt stop ────
    if current_sl is None:
        return

    if direction == "BUY":
        trail_sl      = round(tick.bid - _TRAIL_DIST, 2)
        new_sl        = max(trail_sl, entry_price)
        should_update = new_sl > current_sl
    else:
        trail_sl      = round(tick.ask + _TRAIL_DIST, 2)
        new_sl        = min(trail_sl, entry_price)
        should_update = new_sl < current_sl

    if should_update:
        if mt5_ticket:
            await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
        def _apply_trail_sl():
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                    (new_sl, trade_id),
                )
        await db_module.to_db_thread(_apply_trail_sl)
        log.debug("[scalp_runner] %s trail SL → %.2f (bid=%.2f)",
                  trade_id[:8], new_sl, tick.bid)

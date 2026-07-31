"""Protected Scale TP/SL strategy handler -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._handle_protected_scale,
as part of the core/engine.py migration series. See
docs/todo/refactor/core-protected-scale-handler-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

Takes `bridge`, a TPCache (pack 5), and `close_full_after_tps` (optional
injected callable, same deferred-dependency pattern as packs 17/19)
explicitly. Reuses core_tp_trigger_tracking.get_triggered_tps/
get_remaining_lots (pack 5), core_partial_close.partial_close_trade
(pack 9).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from backend.src.services.risk.strategy_params import get_strategy_params
from backend.src.services.positions.tp_tracking import TPCache, get_triggered_tps, get_remaining_lots
from backend.src.utils.models import STRATEGY_PROTECTED_SCALE, Tick

log = logging.getLogger(__name__)


async def handle_protected_scale(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"])
    mt5_ticket  = trade.get("mt5_ticket")
    trade_id    = trade["trade_id"]
    triggered   = await get_triggered_tps(tp_cache, trade_id)

    if 1 not in triggered:
        tp1_val = trade.get("tp1")
        tp1_vf  = float(tp1_val) if tp1_val else None
        if tp1_vf and (
            (direction == "BUY"  and tp1_vf > entry_price and tick.bid >= tp1_vf) or
            (direction == "SELL" and tp1_vf < entry_price and tick.ask <= tp1_vf)
        ):
            def _mark_tp1_skipped():
                with db_module.db() as conn:
                    conn.execute(
                        "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
                        " VALUES (?,?,?,?,?,?)",
                        (trade_id, time.time(), 0.0, tp1_vf, 0.0, "TP1_SKIPPED"),
                    )
            await db_module.to_db_thread(_mark_tp1_skipped)
            triggered.add(1)

    if 2 not in triggered:
        tp2_val = trade.get("tp2")
        tp2_vf  = float(tp2_val) if tp2_val else None
        if tp2_vf and (
            (direction == "BUY"  and tp2_vf > entry_price and tick.bid >= tp2_vf) or
            (direction == "SELL" and tp2_vf < entry_price and tick.ask <= tp2_vf)
        ):
            def _mark_tp2_be():
                with db_module.db() as conn:
                    conn.execute(
                        "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
                        " VALUES (?,?,?,?,?,?)",
                        (trade_id, time.time(), 0.0, tp2_vf, 0.0, "TP2_BE_LOCKED"),
                    )
            await db_module.to_db_thread(_mark_tp2_be)
            triggered.add(2)
            should_update = (direction == "BUY" and entry_price > current_sl) or \
                            (direction == "SELL" and entry_price < current_sl)
            if should_update:
                if mt5_ticket:
                    await bridge.modify_order(int(mt5_ticket), sl=entry_price, tp=None)
                def _apply_be_sl():
                    with db_module.db() as conn:
                        conn.execute(
                            "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
                            (entry_price, trade_id),
                        )
                await db_module.to_db_thread(_apply_be_sl)
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_sl_moved(trade, 2, entry_price),
                    trade_id, "sl_moved_be",
                ))

    close_pct = get_strategy_params(STRATEGY_PROTECTED_SCALE)["mid_tp_close_pct"] / 100.0
    for tp_num in range(3, 6):
        if tp_num in triggered:
            continue
        tp_val = trade.get(f"tp{tp_num}")
        if tp_val is None:
            continue
        tp_vf = float(tp_val)
        if direction == "BUY"  and tp_vf <= entry_price:
            continue
        if direction == "SELL" and tp_vf >= entry_price:
            continue
        tp_cleared = (direction == "BUY" and tick.bid >= tp_vf) or \
                     (direction == "SELL" and tick.ask <= tp_vf)
        if not tp_cleared:
            break
        remaining = await db_module.to_db_thread(get_remaining_lots, trade_id)
        if remaining <= 0:
            break
        lots_to_close = round(float(trade["lot_size"]) * close_pct, 4)
        lots_to_close = min(lots_to_close, remaining)
        if lots_to_close <= 0:
            continue
        actual_price = tp_vf
        if mt5_ticket:
            mt5_res = await bridge.partial_close(int(mt5_ticket), lots_to_close)
            if mt5_res.get("success"):
                actual_price = float(mt5_res.get("close_price", tp_vf))
                lots_to_close = float(mt5_res.get("lots_closed", lots_to_close))
            elif mt5_res.get("error") or mt5_res.get("success") is False:
                log.warning("[protected_scale] MT5 partial close failed ticket=%s tp=%d: %s",
                            mt5_ticket, tp_num, mt5_res)
                continue
        try:
            res = await partial_close_trade(trade_id, lots_to_close, actual_price, f"TP{tp_num}")
            triggered.add(tp_num)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, actual_price, lots_to_close,
                                           res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
            if res.get("auto_closed"):
                if mt5_ticket and close_full_after_tps:
                    asyncio.create_task(close_full_after_tps(trade_id, mt5_ticket, actual_price))
                break
        except Exception as exc:
            log.warning("[protected_scale] TP%d failed: %s", tp_num, exc)

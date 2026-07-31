"""Conservative Trial TP/SL strategy handler -- extracted verbatim (no
logic changes) from core/engine.py's SimulationEngine._handle_conservative_trial,
as part of the core/engine.py migration series. See
docs/todo/refactor/core-conservative-trial-handler-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

Takes `bridge`, a TPCache (pack 5), and `close_full_after_tps` (optional
injected callable, same deferred-dependency pattern as packs 17/19/21/22/23)
explicitly. Reuses core_tp_trigger_tracking.get_triggered_tps/get_remaining_lots
(pack 5), core_partial_close.partial_close_trade (pack 9) -- note
partial_close_trade has its own independent "move SL to breakeven on TP1"
logic (see 010's Notes), which fires on this handler's own TP1 branch too,
one tier earlier than the handler's own code implies. See 010's Notes for a
second, related quirk: TP2/TP4's own SL-move guards read the `trade` dict
captured once at function entry (never refreshed mid-call), so a same-tick
cascade through TP1+TP2 re-issues a redundant (but harmless) broker sync and
duplicate Telegram alert.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from backend.src.services.risk.strategy_params import get_strategy_params
from forex_trader.core.core_tp_trigger_tracking import (
    TPCache, get_triggered_tps, get_remaining_lots,
)
from backend.src.utils.models import STRATEGY_CONSERVATIVE_TRIAL, Tick

log = logging.getLogger(__name__)


async def handle_conservative_trial(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Conservative Trial strategy:
      All TPs and SL are set from the actual fill price at trade open —
      signal SL/TP are ignored.

      TP1  +5 pts  →  5% close (early insurance lock-in)
      TP2 +10 pts  → 30% close; SL moves to entry (breakeven)
      TP3 +14 pts  → 20% close
      TP4 +20 pts  → 40% close; SL steps to TP2 level
      TP5 +27 pts  →  5% close
      TP6 +35 pts  → close all remaining
    """
    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"]) if trade.get("stop_loss") is not None else None
    mt5_ticket  = trade.get("mt5_ticket")
    trade_id    = trade["trade_id"]
    lot_size    = float(trade["lot_size"])
    triggered   = await get_triggered_tps(tp_cache, trade_id)
    ct_p        = get_strategy_params(STRATEGY_CONSERVATIVE_TRIAL)

    async def _partial(tp_num: int, tp_val: float, pct: float) -> bool:
        """Close pct% of original lot at tp_val. Returns True if trade auto-closed."""
        remaining = await db_module.to_db_thread(get_remaining_lots, trade_id)
        lots_to_close = min(round(lot_size * pct, 4), remaining)
        if lots_to_close <= 0:
            return False
        actual_price = tp_val
        if mt5_ticket:
            mt5_res = await bridge.partial_close(int(mt5_ticket), lots_to_close)
            if mt5_res.get("success"):
                actual_price = float(mt5_res.get("close_price", tp_val))
                lots_to_close = float(mt5_res.get("lots_closed", lots_to_close))
            elif mt5_res.get("error") or mt5_res.get("success") is False:
                err_msg = mt5_res.get("error") or "partial close rejected"
                log.warning("[conservative_trial] MT5 partial close failed ticket=%s tp=%d: %s",
                            mt5_ticket, tp_num, mt5_res)
                asyncio.create_task(telegram_alerts.send_message(
                    f"*TP{tp_num} Partial Close Failed*\n"
                    f"Conservative Trial detected TP{tp_num} @ {tp_val:.2f} "
                    f"but MT5 rejected the partial close.\n"
                    f"Error: {err_msg}\n"
                    f"Trade: {trade_id[:8]} | Ticket: {mt5_ticket}",
                    trade_id, f"tp{tp_num}_fail",
                ))
                return False
        try:
            res = await partial_close_trade(trade_id, lots_to_close, actual_price, f"TP{tp_num}")
            triggered.add(tp_num)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, actual_price, lots_to_close,
                                           res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
            if res.get("auto_closed") and mt5_ticket:
                if close_full_after_tps:
                    asyncio.create_task(close_full_after_tps(trade_id, mt5_ticket, actual_price))
                return True
        except Exception as exc:
            log.warning("[conservative_trial] TP%d close failed: %s", tp_num, exc)
        return False

    def _tp_cleared(tp_num: int) -> Optional[float]:
        val = trade.get(f"tp{tp_num}")
        if val is None:
            return None
        val_f = float(val)
        # Guard: TP must be in the profitable direction from entry
        if direction == "BUY"  and val_f <= entry_price:
            return None
        if direction == "SELL" and val_f >= entry_price:
            return None
        if (direction == "BUY" and tick.bid >= val_f) or \
           (direction == "SELL" and tick.ask <= val_f):
            return val_f
        return None

    async def _move_sl(new_sl: float, reason_tag: str, tp_num: int) -> None:
        """Update SL in DB and MT5, then send alert."""
        if mt5_ticket:
            try:
                await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
            except Exception as _e:
                log.warning("[conservative_trial] modify_order failed (%s): %s", reason_tag, _e)
        def _apply_ct_sl():
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET stop_loss=?, sl_moved_to_be=1 "
                    "WHERE trade_id=?",
                    (new_sl, trade_id),
                )
        await db_module.to_db_thread(_apply_ct_sl)
        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_sl_moved(trade, tp_num, new_sl),
            trade_id, f"sl_moved_{reason_tag}",
        ))

    # ── TP1 — 5% close (early insurance) ─────────────────────────────────
    if 1 not in triggered:
        tp1_f = _tp_cleared(1)
        if tp1_f is not None:
            log.info("[conservative_trial] %s TP1 @ %.2f — 5%% insurance close",
                     trade_id[:8], tp1_f)
            if await _partial(1, tp1_f, ct_p["tp1_pct"] / 100.0):
                return

    # ── TP2 — 30% close + SL to breakeven ────────────────────────────────
    if 2 not in triggered:
        tp2_f = _tp_cleared(2)
        if tp2_f is not None:
            log.info("[conservative_trial] %s TP2 @ %.2f — 30%% close + SL to BE",
                     trade_id[:8], tp2_f)
            await _partial(2, tp2_f, ct_p["tp2_pct"] / 100.0)
            if current_sl is not None and not trade.get("sl_moved_to_be"):
                should_move = (direction == "BUY" and entry_price > current_sl) or \
                              (direction == "SELL" and entry_price < current_sl)
                if should_move:
                    await _move_sl(entry_price, "be", 2)
            return

    # ── TP3 — 20% close ───────────────────────────────────────────────────
    if 3 not in triggered:
        tp3_f = _tp_cleared(3)
        if tp3_f is not None:
            log.info("[conservative_trial] %s TP3 @ %.2f — 20%% close",
                     trade_id[:8], tp3_f)
            if await _partial(3, tp3_f, ct_p["tp3_pct"] / 100.0):
                return

    # ── TP4 — 40% close + SL steps to TP2 level ─────────────────────────
    if 4 not in triggered:
        tp4_f = _tp_cleared(4)
        if tp4_f is not None:
            log.info("[conservative_trial] %s TP4 @ %.2f — 40%% close + SL to TP2",
                     trade_id[:8], tp4_f)
            await _partial(4, tp4_f, ct_p["tp4_pct"] / 100.0)
            tp2_val = trade.get("tp2")
            if tp2_val is not None:
                tp2_sl = float(tp2_val)
                db_sl  = float(trade.get("stop_loss") or current_sl or 0)
                step_up = (direction == "BUY" and tp2_sl > db_sl) or \
                          (direction == "SELL" and tp2_sl < db_sl)
                if step_up:
                    await _move_sl(tp2_sl, "tp2", 4)
            return

    # ── TP5 — 5% close ───────────────────────────────────────────────────
    if 5 not in triggered:
        tp5_f = _tp_cleared(5)
        if tp5_f is not None:
            log.info("[conservative_trial] %s TP5 @ %.2f — 5%% close",
                     trade_id[:8], tp5_f)
            if await _partial(5, tp5_f, ct_p["tp5_pct"] / 100.0):
                return

    # ── TP6 — close all remaining ────────────────────────────────────────
    if 6 not in triggered:
        tp6_f = _tp_cleared(6)
        if tp6_f is not None:
            remaining = await db_module.to_db_thread(get_remaining_lots, trade_id)
            if remaining > 0:
                actual_price = tp6_f
                if mt5_ticket:
                    mt5_res = await bridge.partial_close(int(mt5_ticket), remaining)
                    if mt5_res.get("success"):
                        actual_price = float(mt5_res.get("close_price", tp6_f))
                        remaining = float(mt5_res.get("lots_closed", remaining))
                    elif mt5_res.get("error") or mt5_res.get("success") is False:
                        log.warning(
                            "[conservative_trial] MT5 TP6 final close failed ticket=%s: %s",
                            mt5_ticket, mt5_res,
                        )
                        return
                try:
                    res = await partial_close_trade(trade_id, remaining, actual_price, "TP6")
                    triggered.add(6)
                    asyncio.create_task(telegram_alerts.send_message(
                        telegram_alerts.fmt_tp_hit(
                            trade, 6, actual_price, remaining, res.get("partial_pnl", 0)
                        ),
                        trade_id, "tp6_hit",
                    ))
                    if res.get("auto_closed") and mt5_ticket:
                        if close_full_after_tps:
                            asyncio.create_task(
                                close_full_after_tps(trade_id, mt5_ticket, actual_price)
                            )
                except Exception as exc:
                    log.warning("[conservative_trial] TP6 close failed: %s", exc)

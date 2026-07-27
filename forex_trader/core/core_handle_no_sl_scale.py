"""Trend Ratchet (formerly No-SL Scale Out) TP/SL strategy handler --
extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine._handle_no_sl_scale, as part of the core/engine.py
migration series. See
docs/todo/refactor/core-no-sl-scale-handler-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

Takes `bridge`, a TPCache (pack 5), and `close_full_after_tps` (optional
injected callable, same deferred-dependency pattern as packs 17/19/21/22/
23/24) explicitly. Reuses core_tp_trigger_tracking.get_triggered_tps/
get_remaining_lots (pack 5), core_partial_close.partial_close_trade
(pack 9) -- note partial_close_trade has its own independent "move SL to
breakeven on TP1" logic (see 010's Notes), which fires on this handler's
TP1 branch too even though it never touches SL itself. See 010's Notes for
a second quirk: TP3's own SL-move compares against `current_sl`, a local
captured once at function entry, never refreshed after TP1's side effect
writes a fresh value to the DB earlier in the same call (relevant when
TP1-TP3 all cross within one tick).
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from forex_trader.core.core_tp_trigger_tracking import (
    TPCache, get_triggered_tps, get_remaining_lots,
)
from backend.src.utils.models import MAX_TP, Tick

log = logging.getLogger(__name__)


async def handle_no_sl_scale(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Trend Ratchet strategy (formerly No-SL Scale Out):
      Emergency SL at 1.5× signal SL distance (set at trade open; ADX>30 required at entry)
      TP1          → 20% partial close
      TP2          → skip (mark for UI)
      TP3          → 20% partial close + SL → TP1 level
      TP4          → SL → TP2 level (skip, mark for UI)
      TP5          → SL → TP3 level (skip, mark for UI)
      TP6          → SL → TP4 level (skip, mark for UI)
      TP7          → SL → TP5 level (skip, mark for UI)
      TP8 (or last TP) → close all remaining lots
    """
    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"])
    mt5_ticket  = trade.get("mt5_ticket")
    trade_id    = trade["trade_id"]
    lot_size    = float(trade["lot_size"])
    triggered   = await get_triggered_tps(tp_cache, trade_id)

    # Find last defined TP (TP1–TP8)
    last_tp_num = max(
        (n for n in range(1, MAX_TP + 1) if trade.get(f"tp{n}") is not None),
        default=None,
    )
    if last_tp_num is None:
        return

    def _tp_cleared(tp_num: int) -> Optional[float]:
        val = trade.get(f"tp{tp_num}")
        if val is None:
            return None
        val_f = float(val)
        if direction == "BUY"  and val_f <= entry_price:
            return None
        if direction == "SELL" and val_f >= entry_price:
            return None
        if (direction == "BUY" and tick.bid >= val_f) or \
           (direction == "SELL" and tick.ask <= val_f):
            return val_f
        return None

    async def _partial(tp_num: int, tp_val: float, pct: float) -> bool:
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
                log.warning("[no_sl_scale] MT5 partial close failed ticket=%s tp=%d: %s",
                            mt5_ticket, tp_num, mt5_res)
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
            log.warning("[no_sl_scale] TP%d close failed: %s", tp_num, exc)
        return False

    async def _close_all(tp_num: int, tp_val: float) -> None:
        remaining = await db_module.to_db_thread(get_remaining_lots, trade_id)
        if remaining <= 0:
            return
        actual_price = tp_val
        if mt5_ticket:
            mt5_res = await bridge.partial_close(int(mt5_ticket), remaining)
            if mt5_res.get("success"):
                actual_price = float(mt5_res.get("close_price", tp_val))
                remaining = float(mt5_res.get("lots_closed", remaining))
            elif mt5_res.get("error") or mt5_res.get("success") is False:
                log.warning("[no_sl_scale] MT5 close-all failed ticket=%s tp=%d: %s",
                            mt5_ticket, tp_num, mt5_res)
                return
        try:
            res = await partial_close_trade(trade_id, remaining, actual_price, f"TP{tp_num}")
            triggered.add(tp_num)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, actual_price, remaining,
                                           res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
            if res.get("auto_closed") and mt5_ticket:
                if close_full_after_tps:
                    asyncio.create_task(close_full_after_tps(trade_id, mt5_ticket, actual_price))
        except Exception as exc:
            log.warning("[no_sl_scale] TP%d (close-all) failed: %s", tp_num, exc)

    async def _mark_skipped(tp_num: int, tp_val: float) -> None:
        def _apply():
            with db_module.db() as conn:
                conn.execute(
                    "INSERT INTO vantage_partial_closes "
                    "(trade_id,ts,lots_closed,close_price,pnl,reason) VALUES (?,?,?,?,?,?)",
                    (trade_id, time.time(), 0.0, tp_val, 0.0, f"TP{tp_num}_SKIPPED"),
                )
        await db_module.to_db_thread(_apply)
        triggered.add(tp_num)

    async def _move_sl(tp_num: int, new_sl: float, label: str) -> None:
        should_update = (direction == "BUY" and new_sl > current_sl) or \
                        (direction == "SELL" and new_sl < current_sl)
        if not should_update:
            return
        if mt5_ticket:
            await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
        def _apply():
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                    (new_sl, trade_id),
                )
        await db_module.to_db_thread(_apply)
        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_sl_moved(trade, tp_num, new_sl),
            trade_id, label,
        ))

    # ── TP1 — 20% close ───────────────────────────────────────────────────
    if 1 not in triggered:
        tp1_f = _tp_cleared(1)
        if tp1_f is not None:
            if last_tp_num == 1:
                await _close_all(1, tp1_f)
                return
            if await _partial(1, tp1_f, 0.20):
                return
        else:
            # TP1 is already behind the fill price (instant entry inside signal zone).
            # Mark it skipped so TP3+ can still fire; only close early if the
            # current market is at or better than entry (no net loss on the lot).
            _tp1_raw = trade.get("tp1")
            if _tp1_raw is not None:
                _tp1_level = float(_tp1_raw)
                _behind = (direction == "BUY"  and _tp1_level < entry_price) or \
                          (direction == "SELL" and _tp1_level > entry_price)
                if _behind:
                    _mkt_px = tick.bid if direction == "BUY" else tick.ask
                    _at_be   = (direction == "BUY"  and _mkt_px >= entry_price) or \
                               (direction == "SELL" and _mkt_px <= entry_price)
                    if _at_be:
                        if last_tp_num == 1:
                            await _close_all(1, _mkt_px)
                            return
                        if await _partial(1, _mkt_px, 0.20):
                            return
                    else:
                        # Price has moved against the trade — mark TP1 skipped and
                        # let TP3+ handle recovery rather than locking in a loss.
                        await _mark_skipped(1, _tp1_level)

    # ── TP2 — skip; mark for UI ───────────────────────────────────────────
    if last_tp_num >= 2 and 2 not in triggered:
        tp2_f = _tp_cleared(2)
        if tp2_f is not None:
            if last_tp_num == 2:
                await _close_all(2, tp2_f)
                return
            await _mark_skipped(2, tp2_f)

    # ── TP3 — 20% close + SL → TP1 level ─────────────────────────────────
    if last_tp_num >= 3 and 3 not in triggered:
        tp3_f = _tp_cleared(3)
        if tp3_f is not None:
            if last_tp_num == 3:
                await _close_all(3, tp3_f)
                return
            if await _partial(3, tp3_f, 0.20):
                return
            tp1_val = trade.get("tp1")
            if tp1_val is not None:
                _sl_target = float(tp1_val)
                # When TP1 is behind the fill price, locking SL at TP1 still
                # leaves risk on the table. Use entry price (true BE) instead.
                if (direction == "BUY"  and _sl_target < entry_price) or \
                   (direction == "SELL" and _sl_target > entry_price):
                    _sl_target = entry_price
                await _move_sl(3, _sl_target, "sl_moved_tp3")

    # ── TP4–TP7 — SL steps to TP{n-2}; last TP closes all ────────────────
    for n in range(4, min(last_tp_num + 1, MAX_TP)):
        if n not in triggered:
            tpn_f = _tp_cleared(n)
            if tpn_f is not None:
                if n == last_tp_num:
                    await _close_all(n, tpn_f)
                    return
                sl_ref_val = trade.get(f"tp{n - 2}")
                if sl_ref_val is not None:
                    await _move_sl(n, float(sl_ref_val), f"sl_moved_tp{n}")
                await _mark_skipped(n, tpn_f)

    # ── TP8 (or last TP) — close all remaining ────────────────────────────
    if last_tp_num == MAX_TP and MAX_TP not in triggered:
        tp8_f = _tp_cleared(MAX_TP)
        if tp8_f is not None:
            await _close_all(MAX_TP, tp8_f)

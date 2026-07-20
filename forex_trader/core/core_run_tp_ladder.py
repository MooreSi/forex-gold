"""TP-ladder walk engine + its three thin strategy wrappers -- extracted
verbatim (no logic changes) from core/engine.py's SimulationEngine.
_run_tp_ladder/_handle_signal_climber/_handle_gd_vip_runner/
_handle_adaptive_runner, as part of the core/engine.py migration series.
See docs/todo/refactor/core-tp-ladder-handlers-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

Takes `bridge`, a TPCache (pack 5), and `close_full_after_tps` (optional
injected callable, same deferred-dependency pattern as pack 17) explicitly.
Reuses core_tp_trigger_tracking.get_triggered_tps/log_tp_wait_diagnostic/
get_remaining_lots (pack 5), core_partial_close.partial_close_trade
(pack 9), and pack 11's already-ported _CLIMBER_PCTS/_GDVR_PCTS tables
(core_open_trade.py, ported there for the EA-ladder lookup) rather than
duplicating them a third time.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from forex_trader.core.core_open_trade import _CLIMBER_PCTS, _GDVR_PCTS
from forex_trader.core.core_tp_trigger_tracking import (
    TPCache, get_triggered_tps, log_tp_wait_diagnostic, get_remaining_lots,
)
from forex_trader.core.models import MAX_TP, Tick

log = logging.getLogger(__name__)


async def run_tp_ladder(
    trade: dict,
    tick: Tick,
    pcts_table: dict[int, list[float]],
    log_tag: str,
    bridge: Any,
    tp_cache: TPCache,
    be_at_pos: int = 0,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """Shared TP-ladder walk used by Signal Climber, GD VIP Runner, and
    Adaptive Runner.

    Closes fractions of the original lot at each signal TP (per pcts_table,
    keyed by TP count). SL is left untouched until the TP at index
    `be_at_pos` (0 = TP1, 1 = TP2, ...) is hit, at which point it moves to
    breakeven; every subsequent TP after that trails SL to the previous
    TP's price.
    """
    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"]) if trade.get("stop_loss") is not None else None
    mt5_ticket  = trade.get("mt5_ticket")
    trade_id    = trade["trade_id"]
    lot_size    = float(trade["lot_size"])
    triggered   = await get_triggered_tps(tp_cache, trade_id)

    # Build ordered list of (tp_num, tp_price) for TPs on the correct side of entry.
    # A gap (e.g. tp2 NULL but tp3-tp8 populated — a real shape produced by
    # some follow-up-signal edits) must not truncate the ladder: `continue`
    # past the gap rather than `break`, or every level beyond the first
    # None becomes permanently invisible to this trade for its entire life.
    all_tps: list[tuple[int, float]] = []
    for i in range(1, MAX_TP + 1):
        v = trade.get(f"tp{i}")
        if v is None:
            continue
        tp_f = float(v)
        if (direction == "BUY" and tp_f > entry_price) or \
           (direction == "SELL" and tp_f < entry_price):
            all_tps.append((i, tp_f))

    n = len(all_tps)
    if n == 0:
        return

    pcts = pcts_table.get(n, pcts_table[max(pcts_table)])

    for pos, (tp_num, tp_price) in enumerate(all_tps):
        if tp_num in triggered:
            continue

        tp_hit = (direction == "BUY"  and tick.bid >= tp_price) or \
                 (direction == "SELL" and tick.ask <= tp_price)
        _cur_px = tick.bid if direction == "BUY" else tick.ask
        log_tp_wait_diagnostic(
            tp_cache, trade_id, f"{log_tag}:TP{tp_num}", direction, _cur_px, tp_price, tp_hit,
        )
        if not tp_hit:
            break  # TPs are ordered — stop at first miss

        remaining = await db_module.to_db_thread(get_remaining_lots, trade_id)
        if remaining <= 0:
            break

        is_last = (pos == n - 1)
        if is_last:
            lots_to_close = remaining
        else:
            lots_to_close = min(round(lot_size * pcts[pos], 4), remaining)

        if lots_to_close <= 0:
            continue

        actual_price = tp_price
        if mt5_ticket:
            mt5_res = await bridge.partial_close(int(mt5_ticket), lots_to_close)
            if mt5_res.get("success"):
                actual_price = float(mt5_res.get("close_price", tp_price))
                lots_to_close = float(mt5_res.get("lots_closed", lots_to_close))
            elif mt5_res.get("error") or mt5_res.get("success") is False:
                log.warning("[%s] MT5 partial close failed ticket=%s tp=%d: %s",
                            log_tag, mt5_ticket, tp_num, mt5_res)
                continue

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
                return
        except Exception as exc:
            log.warning("[%s] TP%d partial close failed: %s", log_tag, tp_num, exc)
            continue

        # Trail SL: stays untouched (at its original, wider level) until the
        # TP at index be_at_pos is hit → BE; every TP after that trails SL
        # to the previous TP's price. be_at_pos=0 means TP1 (Signal Climber);
        # GD VIP Runner uses be_at_pos=1 so the wider entry SL isn't given
        # up until TP2.
        if current_sl is None:
            continue
        if pos < be_at_pos:
            continue
        if pos == be_at_pos:
            new_sl = entry_price
            sl_moved_be = 1
        else:
            new_sl = all_tps[pos - 1][1]  # previous TP price
            sl_moved_be = trade.get("sl_moved_to_be", 0)

        should_update = (direction == "BUY" and new_sl > current_sl) or \
                        (direction == "SELL" and new_sl < current_sl)
        if should_update:
            if mt5_ticket:
                await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
            def _apply_ladder_sl(new_sl=new_sl, sl_moved_be=sl_moved_be):
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=? WHERE trade_id=?",
                        (new_sl, sl_moved_be, trade_id),
                    )
            await db_module.to_db_thread(_apply_ladder_sl)


async def handle_signal_climber(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Signal Climber: rides the signal's own TP ladder with progressive exits.

    Uses signal's SL and TP levels exactly — no fixed-offset overrides.
    Exit fractions are determined by TP count (_CLIMBER_PCTS).

      TP1: 20% close → SL → entry (BE)
      TP2: 15% close → SL → TP1 price
      TP3: 15% close → SL → TP2 price
      ...each subsequent TP: SL trails to previous TP price
      Last TP: close all remaining lots

    Designed for professional multi-TP signals (GD2, GDV) where TP5/6 is
    the intended target and dumping 80% at TP1 destroys the expected value.
    """
    await run_tp_ladder(trade, tick, _CLIMBER_PCTS, "signal_climber", bridge, tp_cache,
                       be_at_pos=0, close_full_after_tps=close_full_after_tps)


async def handle_gd_vip_runner(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    GD VIP Runner: same trail-to-prior-TP ladder mechanism as Signal Climber,
    but with a back-loaded close schedule (_GDVR_PCTS) — see
    STRATEGY_DESCRIPTIONS[STRATEGY_GD_VIP_RUNNER] for the backtest this is
    derived from. The SL itself is widened at open time (see
    _gdvr_sl_dist()); this handler only manages TP-ladder exits and SL trail.

    Unlike Signal Climber, SL does not move to breakeven at TP1 — the
    wider entry SL is intentional (see _gdvr_sl_dist()) and moving to BE
    that early would defeat it. BE happens at TP2 instead (be_at_pos=1).
    """
    await run_tp_ladder(trade, tick, _GDVR_PCTS, "gd_vip_runner", bridge, tp_cache,
                       be_at_pos=1, close_full_after_tps=close_full_after_tps)


async def handle_adaptive_runner(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Adaptive Runner: same back-loaded ladder mechanism as GD VIP Runner
    (_GDVR_PCTS), but the SL widened at open time is capped at 50% of the
    distance to the signal's own final TP (see _adaptive_sl_dist()) —
    never wider than that, and never tightened below the signal's own
    stated SL. See STRATEGY_DESCRIPTIONS[STRATEGY_ADAPTIVE_RUNNER] in
    core/models.py for why GD VIP Runner's flat 4x/20pt widening is wrong
    for signals with a short TP ladder.

    Unlike GD VIP Runner, SL moves to breakeven at TP1 (be_at_pos=0) —
    since the stop is already proportionate to the reachable reward,
    there's no need to keep full risk on the table past the first target.
    """
    await run_tp_ladder(trade, tick, _GDVR_PCTS, "adaptive_runner", bridge, tp_cache,
                       be_at_pos=0, close_full_after_tps=close_full_after_tps)

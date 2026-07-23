"""TP-ladder walk engine + its three thin strategy wrappers -- extracted
verbatim (no logic changes) from core/engine.py's SimulationEngine.
_run_tp_ladder/_handle_signal_climber/_handle_reversal_runner/
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
    sl_rule: Optional[Callable[[int, list[tuple[int, float]]], float]] = None,
    close_full_on_last: bool = True,
) -> None:
    """Shared TP-ladder walk used by Signal Climber, Reversal Runner,
    Adaptive Runner, Adaptive Runner 2, and Limit Runner.

    Closes fractions of the original lot at each signal TP (per pcts_table,
    keyed by TP count). SL is left untouched until the TP at index
    `be_at_pos` (0 = TP1, 1 = TP2, ...) is hit, at which point it moves to
    breakeven; every TP after that trails SL according to `sl_rule(pos,
    all_tps)` -- pos is the 0-indexed compacted TP position just hit,
    all_tps the compacted list of (tp_num, tp_price) tuples. Defaults to
    the original trail-to-previous-TP rule (`all_tps[pos - 1][1]`) when
    `sl_rule` is None, so Signal Climber/Reversal Runner/Adaptive Runner are
    unaffected -- Adaptive Runner 2 is the only caller that passes a
    different rule (trail to the midpoint of the two TPs before this one).

    close_full_on_last: when True (every caller before Limit Runner), the
    last TP in the ladder always closes 100% of whatever lots remain,
    regardless of its own pcts_table entry -- there is never anything left
    open once the signal's final TP is hit. Limit Runner passes False for
    signals with a literal "TP OPEN" line: the last TP then only closes its
    own pcts_table share like every earlier TP, and the remainder is left
    open indefinitely (no further TP to trigger a close) -- riding purely on
    the SL trail until stopped out or manually closed. See
    core_limit_order_signal.py for how the pcts_table itself is computed in
    that case (100% minus the configured runner reserve, split across the
    signal's own numeric TPs).
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
        if is_last and close_full_on_last:
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
        # Reversal Runner uses be_at_pos=1 so the wider entry SL isn't given
        # up until TP2.
        if current_sl is None:
            continue
        if pos < be_at_pos:
            continue
        if pos == be_at_pos:
            new_sl = entry_price
            sl_moved_be = 1
        else:
            new_sl = sl_rule(pos, all_tps) if sl_rule else all_tps[pos - 1][1]
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
            current_sl = new_sl
            if pos == be_at_pos:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_sl_moved(trade, tp_num, new_sl),
                    trade_id, "sl_moved_be",
                ))
            else:
                log.info("[%s] %s SL trailed to %.2f after TP%d",
                         log_tag, trade_id[:8], new_sl, tp_num)


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


async def handle_reversal_runner(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Reversal Runner: same trail-to-prior-TP ladder mechanism as Signal Climber,
    but with a back-loaded close schedule (_GDVR_PCTS) — see
    STRATEGY_DESCRIPTIONS[STRATEGY_REVERSAL_RUNNER] for the backtest this is
    derived from. The SL itself is widened at open time (see
    _rr_sl_dist()); this handler only manages TP-ladder exits and SL trail.

    Unlike Signal Climber, SL does not move to breakeven at TP1 — the
    wider entry SL is intentional (see _rr_sl_dist()) and moving to BE
    that early would defeat it. BE happens at TP2 instead (be_at_pos=1).
    """
    await run_tp_ladder(trade, tick, _GDVR_PCTS, "reversal_runner", bridge, tp_cache,
                       be_at_pos=1, close_full_after_tps=close_full_after_tps)


async def handle_adaptive_runner(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Adaptive Runner: same back-loaded ladder mechanism as Reversal Runner
    (_GDVR_PCTS), but the SL widened at open time is capped at 50% of the
    distance to the signal's own final TP (see _adaptive_sl_dist()) —
    never wider than that, and never tightened below the signal's own
    stated SL. See STRATEGY_DESCRIPTIONS[STRATEGY_ADAPTIVE_RUNNER] in
    core/models.py for why Reversal Runner's flat 4x/20pt widening is wrong
    for signals with a short TP ladder.

    Unlike Reversal Runner, SL moves to breakeven at TP1 (be_at_pos=0) —
    since the stop is already proportionate to the reachable reward,
    there's no need to keep full risk on the table past the first target.
    """
    await run_tp_ladder(trade, tick, _GDVR_PCTS, "adaptive_runner", bridge, tp_cache,
                       be_at_pos=0, close_full_after_tps=close_full_after_tps)


def _adaptive_runner_2_sl_rule(pos: int, all_tps: list[tuple[int, float]]) -> float:
    """Adaptive Runner 2's trail rule: SL steps to the midpoint of the two
    TPs before the one just hit (TP3 -> mid(TP1,TP2), TP4 -> mid(TP2,TP3),
    ...), instead of the other ladder strategies' single-previous-TP trail.
    Only ever called by run_tp_ladder() for pos > be_at_pos (=1 for this
    strategy, see handle_adaptive_runner_2), so pos is always >= 2 here and
    pos - 2 never goes negative."""
    return (all_tps[pos - 2][1] + all_tps[pos - 1][1]) / 2


async def handle_adaptive_runner_2(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Adaptive Runner 2: fixed 10pt SL (see _ADAPTIVE2_SL_PT in
    core_signal_resolution.py -- not derived from the signal at all, unlike
    every other ladder strategy), Reversal Runner's back-loaded close
    schedule (_GDVR_PCTS -- an exact match for this strategy's own stated
    5/5/10/10/15/15/15/25 split), and a different SL trail: breakeven at
    TP2 (be_at_pos=1), then from TP3 onward the SL steps to the midpoint of
    the two TPs before the one just cleared instead of snapping to the
    single immediately-previous TP price (_adaptive_runner_2_sl_rule) --
    see STRATEGY_DESCRIPTIONS[STRATEGY_ADAPTIVE_RUNNER_2] in core/models.py.
    """
    await run_tp_ladder(trade, tick, _GDVR_PCTS, "adaptive_runner_2", bridge, tp_cache,
                       be_at_pos=1, close_full_after_tps=close_full_after_tps,
                       sl_rule=_adaptive_runner_2_sl_rule)


def _limit_runner_pcts_table(trade: dict) -> tuple[dict[int, list[float]], bool]:
    """Dynamic pcts_table for a single Limit Runner trade -- unlike every
    other ladder strategy's fixed 8-TP-count table, the split depends on how
    many numeric TPs this specific signal actually had and whether it
    carried a "TP OPEN" runner leg (trade['tp_open'], set at fill time from
    the parsed signal -- see core_limit_order_signal.py). Returns
    (pcts_table, close_full_on_last); a single-entry table since n is
    already fixed for this trade -- run_tp_ladder's own n-keyed lookup
    resolves straight to it."""
    from forex_trader.core.core_strategy_params import get_strategy_params
    from forex_trader.core.models import STRATEGY_LIMIT_RUNNER
    n = sum(1 for i in range(1, MAX_TP + 1) if trade.get(f"tp{i}") is not None)
    if n == 0:
        return {1: [1.0]}, True
    if trade.get("tp_open"):
        reserve = get_strategy_params(STRATEGY_LIMIT_RUNNER)["runner_reserve_pct"] / 100.0
        reserve = min(max(reserve, 0.0), 0.95)
        each = (1.0 - reserve) / n
        return {n: [each] * n}, False
    return {n: [1.0 / n] * n}, True


async def handle_limit_runner(
    trade: dict, tick: Tick, bridge: Any, tp_cache: TPCache,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    """
    Limit Runner: the Python-side fallback ladder handler for a Limit Runner
    trade if the EA goes unhealthy mid-trade (every such trade starts out
    EA-managed -- core_limit_order_signal.py only ever places the pending
    order when the EA is connected, so this is purely a reclaim path, same
    as every other EA-portable ladder strategy). Splits the position evenly
    across whatever numeric TPs the originating signal actually had (see
    _limit_runner_pcts_table) rather than a fixed 8-TP table, since the
    signal's own TP count varies message to message. SL moves to breakeven
    at the configured be_at_pos (Strategy Parameters: 1-based "TP#",
    converted here to run_tp_ladder's 0-indexed compacted position), then
    trails to the previous TP price each level after -- same trail rule as
    Reversal Runner (sl_rule=None). If the signal carried a "TP OPEN" line,
    the last numeric TP only closes its own share instead of everything
    remaining (close_full_on_last=False) -- whatever's left keeps riding on
    the trailing SL with no further target.
    """
    from forex_trader.core.core_strategy_params import get_strategy_params
    from forex_trader.core.models import STRATEGY_LIMIT_RUNNER
    pcts_table, close_full_on_last = _limit_runner_pcts_table(trade)
    be_at_pos_1based = int(get_strategy_params(STRATEGY_LIMIT_RUNNER).get("be_at_pos", 1))
    be_at_pos = max(be_at_pos_1based - 1, 0)
    await run_tp_ladder(trade, tick, pcts_table, "limit_runner", bridge, tp_cache,
                       be_at_pos=be_at_pos, close_full_after_tps=close_full_after_tps,
                       close_full_on_last=close_full_on_last)

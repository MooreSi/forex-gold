"""Sub-second TP-ladder polling (M4 B9e).

This was SimulationEngine._tp_ladder_fast_loop. It is the sole owner of
TP-crossing detection for the ladder strategies when DPM is off, polling
far faster than the monitor loop because gold TP levels can sit ~1pt apart
and a spike-and-reverse can cross several tiers between two samples.

Moved verbatim. is_running is a callable so the loop notices shutdown
while awaiting, and the poll interval and strategy set are carried on the
context rather than read off a class the service should not know about.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Iterable, Optional

import asyncio

from backend.src.db import database as db_module
from backend.src.services.broker.mt5_native import NativeMT5Bridge
from backend.src.services.positions.tp_ladder import handle_adaptive_runner as _handle_adaptive_runner_impl
from backend.src.services.positions.tp_ladder import handle_adaptive_runner_2 as _handle_adaptive_runner_2_impl
from backend.src.services.positions.tp_ladder import handle_limit_runner as _handle_limit_runner_impl
from backend.src.services.positions.tp_ladder import handle_reversal_runner as _handle_reversal_runner_impl
from backend.src.services.positions.tp_ladder import handle_signal_climber as _handle_signal_climber_impl
from backend.src.utils.models import STRATEGY_ADAPTIVE_RUNNER_2
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER
from backend.src.utils.models import STRATEGY_REVERSAL_RUNNER
from backend.src.utils.models import STRATEGY_SIGNAL_CLIMBER


log = logging.getLogger(__name__)


@dataclass
class TPLadderCtx:
    """Everything the fast loop reached for through `self`."""
    is_running: Optional[Callable[[], bool]] = None
    poll_interval: float = 0.25
    ladder_strategies: Iterable[str] = ()
    bridge: Any = None
    tp_trigger_cache: Any = None
    get_fresh_tick: Optional[Callable[[], Awaitable[Any]]] = None
    get_open_trades: Optional[Callable[[], list]] = None
    close_full_after_tps: Optional[Callable[..., Awaitable[None]]] = None


async def tp_ladder_fast_loop(ctx: TPLadderCtx) -> None:
    """
    Sub-second polling for the three strategies that manage a signal's
    TP1-TPn levels as sequential partial closes on ONE MT5 ticket
    (_run_tp_ladder), rather than resting broker-side TP orders on N
    separate tickets. _monitor_loop's own ~1-5s cadence (and
    get_tick()'s 1s TICK_CACHE_TTL) is too coarse for this: gold TP
    levels are sometimes only ~1pt apart, and a fast spike-and-reverse
    can cross several tiers and fall back below all of them between two
    samples, banking nothing despite price genuinely touching TP5-TP8
    (confirmed via M1-candle-derived max_tp_hit vs. actual banked P&L,
    2026-07-17: ~$1,847 missed over 2 days on a 50-trade sample). This
    loop exists to close that gap without the N-tickets-per-signal
    design tried and reverted the same day (too much MT5/UI clutter
    for one logical trade) — same underlying detection problem, faster
    sampling instead of broker-side execution as the fix.

    Sole owner of TP-ladder crossing detection for these three
    strategies when DPM is off — _monitor_loop explicitly no-ops for
    them in that case (see its own dispatch) so the two loops never
    race on the same trade. DPM still takes priority over everything
    but ORB_FIXED when enabled (checked here too, so both loops agree
    on who owns a given trade at any moment).

    Fetches one fresh tick per cycle and checks every eligible open
    trade against it, rather than a tick-per-trade, so load on the
    shared bridge call lock (NativeMT5Bridge._call serializes every
    MT5 IPC call app-wide) scales with polling frequency, not with
    open-trade count.
    """
    while ctx.is_running():
        try:
            rs = await db_module.to_db_thread(db_module.get_risk_settings)
            if not bool(rs.get("dpm_enabled", 0)):
                open_trades = await db_module.to_db_thread(ctx.get_open_trades)
                ladder_trades = [t for t in open_trades
                                 if t.get("strategy") in ctx.ladder_strategies]
                # EA handoff: skip any trade the EA currently owns and is
                # still healthy for — same reclaim logic as _monitor_loop's
                # own dispatch (see its comment). Missing this let this
                # loop potentially double-manage a trade already being
                # handled natively inside MT5 by the EA — caught before it
                # ever fired in practice (all adaptive_runner trades today
                # happened to be Python-managed already), but a real gap
                # regardless once EA handoff is working for these strategies.
                if ladder_trades:
                    try:
                        from backend.src.services.broker import ea_bridge as _ea_mod
                        _ea = _ea_mod.get_instance()
                        _ea_healthy = _ea is not None and _ea.is_ea_healthy()
                    except ImportError:
                        _ea_healthy = False
                    if _ea_healthy:
                        ladder_trades = [t for t in ladder_trades if t.get("managed_by") != "ea"]
                if ladder_trades:
                    tick = await ctx.get_fresh_tick()
                    if tick:
                        for trade in ladder_trades:
                            try:
                                strat = trade["strategy"]
                                if strat == STRATEGY_SIGNAL_CLIMBER:
                                    await _handle_signal_climber_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                                elif strat == STRATEGY_REVERSAL_RUNNER:
                                    await _handle_reversal_runner_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                                elif strat == STRATEGY_ADAPTIVE_RUNNER_2:
                                    await _handle_adaptive_runner_2_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                                elif strat == STRATEGY_LIMIT_RUNNER:
                                    await _handle_limit_runner_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                                else:
                                    await _handle_adaptive_runner_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                            except Exception as exc:
                                log.warning("[TP-ladder-fast] handler error trade=%s: %s",
                                           trade.get("trade_id"), exc)
        except Exception as exc:
            log.warning("[TP-ladder-fast] loop error: %s", exc)
        await asyncio.sleep(ctx.poll_interval)

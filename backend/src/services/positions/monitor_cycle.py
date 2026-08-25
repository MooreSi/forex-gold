"""One pass of the position-monitoring loop (M4 B9d).

This was the inside of SimulationEngine._monitor_loop: read a tick,
dispatch every open trade to its strategy handler, run the pending-signal
watcher and the IME timeout watchdog, then tick four counters that fire
MT5 reconciliation, the profit sweep and DPM self-calibration on their own
cadences. It returns whether the next sleep should be the fast one.

The `while` shell stays on the runtime, which owns the task's lifetime.
Only the cycle lives here.

Why MonitorState is a shared object and not copied values: this cycle
mutates state that must survive into the NEXT cycle. The four counters
exist precisely to count cycles -- reset them each pass and MT5
reconciliation, the profit sweep and DPM calibration never fire again.
has_open_trades and has_pending_signals drive the 1s-vs-5s adaptive sleep,
and deliberately keep their previous value when a tick comes back empty.
A context of copied values would review cleanly, pass a naive wiring test,
and silently disable three background jobs.

dpm_candles is the exception that stays on the runtime, reached through
get/set callbacks: it is read outside this loop too -- by
open_trade_from_signal and by the scan context -- so the cycle has to
write the runtime's own attribute rather than a private copy.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import asyncio
import re
import time

from backend.src.utils.models import STRATEGY_ADAPTIVE_RUNNER
from backend.src.utils.models import STRATEGY_ADAPTIVE_RUNNER_2
from backend.src.utils.models import STRATEGY_BE_RUNNER
from backend.src.utils.models import STRATEGY_CONSERVATIVE
from backend.src.utils.models import STRATEGY_CONSERVATIVE_TRIAL
from backend.src.utils.models import STRATEGY_FIXED_RR
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER
from backend.src.utils.models import STRATEGY_NO_SL_SCALE
from backend.src.utils.models import STRATEGY_ORB_FIXED
from backend.src.utils.models import STRATEGY_PROTECTED_SCALE
from backend.src.utils.models import STRATEGY_REVERSAL_RUNNER
from backend.src.utils.models import STRATEGY_SCALE_OUT
from backend.src.utils.models import STRATEGY_SCALP_RUNNER
from backend.src.utils.models import STRATEGY_SIGNAL_CLIMBER
from backend.src.utils.models import STRATEGY_TRAIL_STOP
from backend.src.services.positions.monitor_loop import check_profit_close_target as _check_profit_close_target_impl
from backend.src.services.positions.monitor_loop import check_sl as _check_sl_impl
from backend.src.services.positions.handle_be_runner import handle_be_runner as _handle_be_runner_impl
from backend.src.services.positions.handle_conservative import handle_conservative as _handle_conservative_impl
from backend.src.services.positions.handle_conservative_trial import handle_conservative_trial as _handle_conservative_trial_impl
from backend.src.services.dpm.handler import handle_dynamic_position_management as _handle_dynamic_position_management_impl
from backend.src.services.positions.handle_no_sl_scale import handle_no_sl_scale as _handle_no_sl_scale_impl
from backend.src.services.positions.handle_orb_fixed import handle_orb_fixed as _handle_orb_fixed_impl
from backend.src.services.positions.handle_protected_scale import handle_protected_scale as _handle_protected_scale_impl
from backend.src.services.positions.handle_scale_out import handle_scale_out as _handle_scale_out_impl
from backend.src.services.positions.handle_scalp_runner import handle_scalp_runner as _handle_scalp_runner_impl
from backend.src.services.positions.handle_trail_stop import handle_trail_stop as _handle_trail_stop_impl
from backend.src.services.trading.instant_followup import ime_timeout_watchdog as _ime_timeout_watchdog_impl
from backend.src.services.trading.profit_sync import profit_sweep as _profit_sweep_impl
# Wired by the 2026-08-25 merge. Upstream calls all four from its monitor loop;
# that loop is this module here, so the call sites did not come across with the
# engine hunks and the three modules sat unreachable -- the orphan gate caught
# it. See tests/refactor/test_orphan_modules.py.
from backend.src.services.positions.core_equity_protect import (
    check_equity_protect as _check_equity_protect_impl,
    check_basket_harvest as _check_basket_harvest_impl,
)
from backend.src.services.positions.core_orphan_reconcile import (
    reconcile_orphaned_trades as _reconcile_orphaned_trades_impl,
)
from backend.src.services.positions.core_template_placeholder_repair import (
    repair_template_placeholders as _repair_template_placeholders_impl,
)
from backend.src.services.positions.monitor_loop import reclaim_ea_managed_trade as _reclaim_ea_managed_trade_impl
from backend.src.services.positions.monitor_loop import reconcile_sl_hit as _reconcile_sl_hit_impl
from backend.src.services.dpm.handler import run_dpm_calibration as _run_dpm_calibration_impl
from backend.src.services.signals.pending_activation import try_activate_pending_signals as _try_activate_pending_signals_impl
from backend.src.db import database as db_module


log = logging.getLogger(__name__)


@dataclass
class MonitorState:
    """State that outlives a single cycle. Shared by reference."""
    # Adaptive-poll flags. Keep their previous value on an empty tick.
    has_open_trades: bool = False
    has_pending_signals: bool = False
    # Cadence counters. Were __init__ attributes on the runtime.
    sync_cycle: int = 0        # every 6  -> reconcile closed MT5 positions
    profit_cycle: int = 0      # every 24 -> profit sweep
    cal_cycle: int = 0         # ~hourly  -> DPM self-calibration
    dxy_cycle: int = 0         # every 12 -> refresh DXY candles
    # Added by the 2026-08-25 upstream merge, with _revalidate_pending_orders.
    pending_revalidate_cycle: int = 0
    # Orphan reconcile is throttled to once a minute, not once a cycle:
    # it costs a /positions read plus a history lookup per suspect row,
    # and a stranded row has usually been stranded for hours.
    last_orphan_sweep: float = 0.0
    dpm_dxy_candles: list = field(default_factory=list)


@dataclass
class MonitorCtx:
    """Everything one cycle reached for through `self`."""
    state: MonitorState = field(default_factory=MonitorState)
    bridge: Any = None
    cfg: Optional[dict] = None
    tp_trigger_cache: Any = None
    dpm_cache: Any = None
    scale_out_last_fail: Optional[dict] = None
    pending_activation_retry_after: Optional[dict] = None
    # dpm_candles lives on the runtime -- see the module docstring.
    get_dpm_candles: Optional[Callable[[], list]] = None
    set_dpm_candles: Optional[Callable[[list], None]] = None
    # Bound runtime methods.
    get_tick: Optional[Callable[[], Awaitable[Any]]] = None
    get_open_trades: Optional[Callable[[], list]] = None
    get_candles: Optional[Callable[..., Awaitable[list]]] = None
    is_trading_paused: Optional[Callable[[], bool]] = None
    background_open_commentary: Optional[Callable[..., Awaitable[None]]] = None
    close_full_after_tps: Optional[Callable[..., Awaitable[None]]] = None
    make_close_trade_ctx: Optional[Callable[[], Any]] = None
    sync_closed_mt5_positions: Optional[Callable[[], Awaitable[None]]] = None
    # Close-path operation, injected as the runtime's own bound method and
    # passed through untouched -- these collaborators decide WHETHER to
    # close; they never reshape HOW.
    close_trade: Optional[Callable[..., Awaitable[dict]]] = None


async def run_monitor_cycle(ctx: MonitorCtx) -> bool:
    """Run one cycle. Returns True when the caller should poll fast (1s)."""
    try:
        tick = await ctx.get_tick()
        if tick:
            open_trades = await db_module.to_db_thread(ctx.get_open_trades)
            ctx.state.has_open_trades = bool(open_trades)
            if open_trades and ctx.close_trade is not None:
                try:
                    await _check_equity_protect_impl(open_trades, ctx.bridge, ctx.close_trade)
                except Exception:
                    log.debug("Equity Protect check failed", exc_info=True)
                try:
                    await _check_basket_harvest_impl(open_trades, ctx.bridge, ctx.close_trade)
                except Exception:
                    log.debug("Basket Harvest check failed", exc_info=True)
                # Repair rows the broker has already closed but the app never
                # heard about (see core_orphan_reconcile).
                _now_orph = time.time()
                if _now_orph - ctx.state.last_orphan_sweep > 60.0:
                    ctx.state.last_orphan_sweep = _now_orph
                    try:
                        await _reconcile_orphaned_trades_impl(ctx.bridge, ctx.close_trade)
                    except Exception:
                        log.debug("Orphan reconcile failed", exc_info=True)
            rs = await db_module.to_db_thread(db_module.get_risk_settings)
            profit_close_usd = float(rs.get("profit_close_usd", 0.0) or 0.0)
            # Refresh candle cache once per cycle (shared by all DPM trade handlers)
            if bool(rs.get("dpm_enabled", 0)) and open_trades:
                try:
                    ctx.set_dpm_candles(await ctx.get_candles("M5", 30))
                except Exception:
                    pass
                # DXY candles refreshed every ~60s (12 cycles × 5s)
                ctx.state.dxy_cycle += 1
                if ctx.state.dxy_cycle >= 12:
                    ctx.state.dxy_cycle = 0
                    try:
                        dxy_sym = await db_module.to_db_thread(db_module.get_app_config, "dxy_symbol") or "USDX"
                        fetched = await ctx.bridge.get_candles_for_symbol(
                            dxy_sym, "M5", 20
                        )
                        if fetched:
                            ctx.state.dpm_dxy_candles = fetched
                    except Exception:
                        pass
            _eff_strategy, _ooh_active = db_module.get_effective_strategy(rs)
            for trade in open_trades:
                # OOH overrides the per-trade strategy when the window is active.
                strategy = _eff_strategy if _ooh_active else trade.get("strategy", STRATEGY_SCALE_OUT)
                hit = _check_sl_impl(trade, tick)
                if hit:
                    trade_id, price, reason = hit
                    await _reconcile_sl_hit_impl(
                        trade, tick, price, reason, ctx.bridge, ctx.make_close_trade_ctx(),
                    )
                    continue
                # Profit-close target check — cumulative (partials taken + unrealised).
                if await _check_profit_close_target_impl(
                    trade, tick, profit_close_usd, ctx.bridge, ctx.make_close_trade_ctx(),
                ):
                    continue
                # EA handoff: this trade's SL/TP/partial-close ladder is being
                # managed tick-by-tick inside the MT5 terminal itself, not by
                # the handlers below — skip them entirely while the EA is
                # healthy. If it's gone silent (crashed, chart removed, socket
                # dropped), reclaim management here rather than leaving the
                # trade with no one watching it — see ea_bridge.py's module
                # docstring for why Python must always be able to take back
                # over instead of just trusting the EA blindly forever.
                if trade.get("managed_by") == "ea":
                    if await _reclaim_ea_managed_trade_impl(trade, strategy):
                        continue

                dpm_enabled = bool(rs.get("dpm_enabled", 0))
                try:
                    # ORB/IVB trades take priority over DPM and every
                    # other handler — the whole point of this strategy
                    # is "exactly the setup the report computed,
                    # nothing recalculated," so it must never be swept
                    # into DPM just because the global DPM toggle
                    # happens to be on for everything else.
                    if strategy == STRATEGY_ORB_FIXED:
                        await _handle_orb_fixed_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache)
                    elif dpm_enabled and not _ooh_active:
                        await _handle_dynamic_position_management_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, ctx.dpm_cache, ctx.get_dpm_candles(), ctx.state.dpm_dxy_candles)
                    elif strategy == STRATEGY_BE_RUNNER:
                        await _handle_be_runner_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, ctx.scale_out_last_fail, dpm_candles=ctx.get_dpm_candles(), close_full_after_tps=ctx.close_full_after_tps)
                    elif strategy == STRATEGY_TRAIL_STOP:
                        await _handle_trail_stop_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache)
                    elif strategy == STRATEGY_PROTECTED_SCALE:
                        await _handle_protected_scale_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                    elif strategy == STRATEGY_CONSERVATIVE:
                        await _handle_conservative_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                    elif strategy == STRATEGY_SCALP_RUNNER:
                        await _handle_scalp_runner_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                    elif strategy in (STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER,
                                      STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
                                      STRATEGY_LIMIT_RUNNER):
                        # TP-crossing detection for these five moved to
                        # _tp_ladder_fast_loop (sub-second polling instead
                        # of this loop's 1-5s cadence — see that method's
                        # docstring). DPM still takes priority when
                        # enabled (handled above, same as every other
                        # strategy) — this branch only reached with DPM
                        # off, where the fast loop is the sole owner.
                        # Explicit no-op instead of falling through to
                        # _handle_scale_out below, which would be wrong.
                        pass
                    elif strategy == STRATEGY_NO_SL_SCALE:
                        await _handle_no_sl_scale_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                    elif strategy == STRATEGY_CONSERVATIVE_TRIAL:
                        await _handle_conservative_trial_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, close_full_after_tps=ctx.close_full_after_tps)
                    elif strategy == STRATEGY_FIXED_RR:
                        # Nothing to do -- both the stop and the target are
                        # real broker orders, so MT5 closes this trade itself
                        # and the reconciliation poller picks it up. An
                        # explicit branch is REQUIRED: the else below is
                        # handle_scale_out, which would otherwise partial-close
                        # against tp1 and fabricate PnL. Ported from upstream
                        # engine.py by the 2026-08-25 merge -- the strategy did
                        # not exist at the fork point, so this dispatch had no
                        # branch for it and a FIXED_RR trade fell straight
                        # through. Guarded by tests/core/test_fixed_rr_strategy.
                        pass
                    else:
                        await _handle_scale_out_impl(trade, tick, ctx.bridge, ctx.tp_trigger_cache, ctx.scale_out_last_fail, close_full_after_tps=ctx.close_full_after_tps)
                except Exception as exc:
                    log.warning("Strategy handler [%s] error: %s", strategy, exc)

            # ── Pending signal watcher ───────────────────────────────
            if bool(rs.get("auto_execute_signals", 0)) and not await db_module.to_db_thread(ctx.is_trading_paused):
                try:
                    ctx.state.has_pending_signals = await _try_activate_pending_signals_impl(tick, rs, ctx.bridge, ctx.pending_activation_retry_after, ctx.get_dpm_candles(), starting_balance=ctx.cfg.get('starting_balance', 1000.0), background_open_commentary=ctx.background_open_commentary)
                except Exception as exc:
                    log.debug("[PendingWatcher] Error: %s", exc)
                    ctx.state.has_pending_signals = False
            else:
                ctx.state.has_pending_signals = False

            # ── IME timeout watchdog ──────────────────────────────────
            # Auto-assigns TP/SL to IME trades with no follow-up after 3 min
            try:
                await _ime_timeout_watchdog_impl(tick, ctx.bridge)
            except Exception as exc:
                log.debug("[IME-timeout] Watchdog error: %s", exc)
    except Exception as exc:
        log.warning("Monitor loop error: %s", exc)

    ctx.state.sync_cycle += 1
    if ctx.state.sync_cycle >= 6:
        ctx.state.sync_cycle = 0
        try:
            await ctx.sync_closed_mt5_positions()
        except Exception as e:
            log.debug("MT5 sync error: %s", e)
        # EA Template placeholder rows are excluded from the sync above
        # (managed_by='ea', and they have no ticket to look up anyway), so they
        # need their own reconciliation: a leg-fill event that never reached
        # this node otherwise leaves the row a permanent $0-entry ghost in
        # Active Trades. See core_template_placeholder_repair.py.
        try:
            await _repair_template_placeholders_impl(ctx.bridge)
        except Exception as e:
            log.debug("Template placeholder repair error: %s", e)

    ctx.state.profit_cycle += 1
    if ctx.state.profit_cycle >= 24:
        ctx.state.profit_cycle = 0
        try:
            await _profit_sweep_impl(ctx.bridge)
        except Exception as e:
            log.debug("Profit sweep error: %s", e)

    # DPM self-calibration: attempt once per hour.
    # Cal_cycle threshold adapts to the current sleep interval:
    # 720 × 5s = 3600s idle, 3600 × 1s = 3600s active.
    _fast_poll = ctx.state.has_open_trades or ctx.state.has_pending_signals
    ctx.state.cal_cycle += 1
    _cal_threshold = 720 if not _fast_poll else 3600
    if ctx.state.cal_cycle >= _cal_threshold:
        ctx.state.cal_cycle = 0
        try:
            rs_cal = await db_module.to_db_thread(db_module.get_risk_settings)
            if bool(rs_cal.get("dpm_enabled", 0)):
                asyncio.create_task(_run_dpm_calibration_impl(ctx.dpm_cache))
        except Exception as e:
            log.debug("DPM calibration trigger error: %s", e)

    # Fast poll (1s) when trades are open OR a queued signal is waiting
    # for its zone to be re-entered — a GD2-style queued signal with no
    # other trade open would otherwise only get checked every 5s, adding
    # up to ~5s of pure polling latency on top of everything else once
    # price actually returns to the zone. Slow poll (5s) only when
    # there is truly nothing time-sensitive to watch.

    # The shell turns this into 1s vs 5s. Computed above, before the
    # calibration threshold that also depends on it.
    return _fast_poll

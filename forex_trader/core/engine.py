"""
SimulationEngine — core trading logic.
Extracted from vantage_mt5/service.py with FastAPI layer removed.
Callers invoke methods directly; no HTTP indirection.
"""

import asyncio
import json
import logging
import math
import re
import time
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, TYPE_CHECKING

from forex_trader.core import database as db_module
from backend.src.utils.models import (
    Tick, CONTRACT_SIZE, POINT_SIZE,
    STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER, STRATEGY_TRAIL_STOP, STRATEGY_PROTECTED_SCALE,
    STRATEGY_CONSERVATIVE, STRATEGY_NO_SL_SCALE, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_SCALP_RUNNER, STRATEGY_SIGNAL_CLIMBER,
    STRATEGY_REVERSAL_RUNNER, STRATEGY_ORB_FIXED, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2, STRATEGY_LIMIT_RUNNER,
    STRATEGY_NAMES, MAX_TP,
)
from forex_trader.core.mt5_bridge import MT5BridgeClient
from forex_trader.core.mt5_bridge_native import NativeMT5Bridge, is_available as _native_bridge_available
from forex_trader.core.signal_parser import (
    parse_instant_entry, parse_gd2_instant_entry, is_gd2_message,
    SIGNAL_PREFIX, check_sl_adjustment_rules,
)
from forex_trader.core import telegram_alerts
from backend.src.services.ai import claude_ai as claude_ai
from backend.src.services.ai import provider as ai_provider
from forex_trader.core import dpm_engine
from forex_trader.core import core_ea_templates as _ea_templates
from forex_trader.core.core_monitor_loop import (
    check_sl as _check_sl_impl,
    reconcile_sl_hit as _reconcile_sl_hit_impl,
    check_profit_close_target as _check_profit_close_target_impl,
    reclaim_ea_managed_trade as _reclaim_ea_managed_trade_impl,
)
from forex_trader.core.core_scan_messages_auto_execute import (
    price_in_entry_range as _price_in_entry_range_impl,
    execute_auto_signal as _execute_auto_signal_impl,
)
from forex_trader.core.core_limit_order_signal import (
    handle_limit_order_signal as _handle_limit_order_signal_impl,
)
from forex_trader.core.core_logic_keyword_triggers import (
    should_skip_media_or_forwarded,
    should_skip_for_exclusion,
    try_handle_close_all_trigger,
    try_handle_risk_free_be_trigger,
    try_handle_tp_hit_trigger,
)
from forex_trader.core.core_scan_messages_edit_reparse import (
    handle_signal_edit as _handle_signal_edit_impl,
)
from forex_trader.core.core_scan_messages_parse_classify import (
    classify_and_parse as _classify_and_parse_impl,
)
from forex_trader.core.core_scan_messages_staleness_strategy import (
    record_staleness_or_new as _record_staleness_or_new_impl,
    resolve_strategy_and_skip_reason as _resolve_strategy_and_skip_reason_impl,
)
from forex_trader.core.core_max_tp_hit import (
    _tp_level_from_extreme,
    max_tp_checker_sweep as _max_tp_checker_sweep_impl,
    backfill_max_tp_hit_corrected as _backfill_max_tp_hit_corrected_impl,
)
from forex_trader.core.core_fees_sizing import (
    pnl as _pnl_impl, suggest_lot_size as _suggest_lot_size_impl,
    calculate_fees as _calculate_fees_impl)
from forex_trader.core.core_mt5_performance import (
    compute_mt5_performance as _compute_mt5_performance_impl,
    _platform_fee_rate, _apply_fee,
)
from forex_trader.core.core_total_deposits import get_total_deposits as _get_total_deposits_impl
from forex_trader.core.core_sim_account import (
    get_sim_account as _get_sim_account_impl,
    update_sim_balance as _update_sim_balance_impl,
    reset_simulation as _reset_simulation_impl,
)
from backend.src.services.analytics.reporting import (
    get_open_trades as _get_open_trades_impl,
    get_all_trades as _get_all_trades_impl,
    compute_performance as _compute_performance_impl,
)
from forex_trader.core.core_mt5_import import import_mt5_history as _import_mt5_history_impl
from forex_trader.core.core_tg_signals import get_tg_signals as _get_tg_signals_impl
from forex_trader.core.core_tp_trigger_tracking import (
    TPCache as _TPCache,
    get_triggered_tps as _get_triggered_tps_impl,
    last_closed_tp as _last_closed_tp_impl,
    log_tp_wait_diagnostic as _log_tp_wait_diagnostic_impl,
    check_tp_hits as _check_tp_hits_impl,
    get_remaining_lots as _get_remaining_lots_impl,
)
from forex_trader.core.core_signals import (
    create_signal as _create_signal_impl,
    get_signals as _get_signals_impl,
    activate_signal as _activate_signal_impl,
    cancel_signal as _cancel_signal_impl,
)
from forex_trader.core.core_reversal_research import reversal_engine_research_sweep as _reversal_engine_research_sweep_impl
from backend.src.services.notifications.scheduler import email_scheduler_sweep as _email_scheduler_sweep_impl
from forex_trader.core.core_bot_commands_readonly import (
    cmd_help as _cmd_help_impl,
    cmd_balance as _cmd_balance_impl,
    cmd_daily as _cmd_daily_impl,
    cmd_status as _cmd_status_impl,
    cmd_trades as _cmd_trades_impl,
    cmd_pause as _cmd_pause_impl,
    cmd_resume as _cmd_resume_impl,
    cmd_risk as _cmd_risk_impl,
    cmd_strategy as _cmd_strategy_impl,
    cmd_dpm_on as _cmd_dpm_on_impl,
    cmd_dpm_off as _cmd_dpm_off_impl,
    cmd_ime_on as _cmd_ime_on_impl,
    cmd_ime_off as _cmd_ime_off_impl,
)
from forex_trader.core.core_bot_commands_infra import (
    cmd_restart_bridge as _cmd_restart_bridge_impl,
    cmd_restart_app as _cmd_restart_app_impl,
    cmd_headless as _cmd_headless_impl,
    cmd_switch_live as _cmd_switch_live_impl,
    cmd_switch_demo as _cmd_switch_demo_impl,
    cmd_switch_env as _cmd_switch_env_impl,
)
from forex_trader.core.core_bot_commands_trading import (
    cmd_activate as _cmd_activate_impl,
    cmd_report as _cmd_report_impl,
)
from forex_trader.core.core_bridge_watchdog import bridge_watchdog_check as _bridge_watchdog_check_impl
from forex_trader.core.core_profit_sync import (
    sync_profit as _sync_profit_impl,
    schedule_profit_sync as _schedule_profit_sync_impl,
    profit_sweep as _profit_sweep_impl,
)
from forex_trader.core.core_update_signal import update_signal as _update_signal_impl
from forex_trader.core.core_risk_governor import (
    is_trading_paused as _is_trading_paused_impl,
    check_pre_trade_filters as _check_pre_trade_filters_impl,
    rg_day_start_ts as _rg_day_start_ts_impl,
    rg_size_and_check as _rg_size_and_check_impl,
    rg_check_halt as _rg_check_halt_impl,
    rg_apply_halts_on_close as _rg_apply_halts_on_close_impl,
)
from forex_trader.core.core_tp_safety_net import (
    tp_safety_net_sweep as _tp_safety_net_sweep_impl,
    tp_safety_net_check_trade as _tp_safety_net_check_trade_impl,
    compute_be_cost_pts as _compute_be_cost_pts_impl,
)
from forex_trader.core.core_untracked_positions import (
    get_untracked_mt5_positions as _get_untracked_mt5_positions_impl,
)
from forex_trader.core.core_ai_signal_fallback import (
    try_ai_signal_fallback as _try_ai_signal_fallback_impl,
    push_ai_recovered_created as _push_ai_recovered_created_impl,
    apply_sl_adjustment as _apply_sl_adjustment_impl,
    queue_unrecognised as _queue_unrecognised_impl,
    analyse_unrecognised_message as _analyse_unrecognised_message_impl,
)
from forex_trader.core.core_instant_followup import (
    apply_followup_to_instant_trade as _apply_followup_to_instant_trade_impl,
    find_and_apply_instant_followup as _find_and_apply_instant_followup_impl,
    ime_timeout_watchdog as _ime_timeout_watchdog_impl,
)
from forex_trader.core.core_instant_entry import (
    process_instant_entry as _process_instant_entry_impl,
)
from forex_trader.core.core_run_tp_ladder import (
    run_tp_ladder as _run_tp_ladder_impl,
    handle_signal_climber as _handle_signal_climber_impl,
    handle_reversal_runner as _handle_reversal_runner_impl,
    handle_adaptive_runner as _handle_adaptive_runner_impl,
    handle_adaptive_runner_2 as _handle_adaptive_runner_2_impl,
    handle_limit_runner as _handle_limit_runner_impl,
)
from forex_trader.core.core_handle_scale_out import handle_scale_out as _handle_scale_out_impl
from forex_trader.core.core_handle_be_runner import handle_be_runner as _handle_be_runner_impl
from forex_trader.core.core_handle_trail_stop import handle_trail_stop as _handle_trail_stop_impl
from forex_trader.core.core_handle_protected_scale import (
    handle_protected_scale as _handle_protected_scale_impl,
)
from forex_trader.core.core_handle_no_sl_scale import handle_no_sl_scale as _handle_no_sl_scale_impl
from forex_trader.core.core_handle_conservative import handle_conservative as _handle_conservative_impl
from forex_trader.core.core_handle_conservative_trial import (
    handle_conservative_trial as _handle_conservative_trial_impl,
)
from forex_trader.core.core_handle_scalp_runner import handle_scalp_runner as _handle_scalp_runner_impl
from forex_trader.core.core_handle_orb_fixed import handle_orb_fixed as _handle_orb_fixed_impl
from forex_trader.core.core_dpm_handler import (
    run_dpm_calibration as _run_dpm_calibration_impl,
    handle_dynamic_position_management as _handle_dynamic_position_management_impl,
)
from forex_trader.core.core_partial_close import partial_close_trade as _partial_close_trade_impl
from forex_trader.core.core_open_trade import open_trade as _open_trade_impl
from forex_trader.core.core_manual_market_order import (
    open_manual_market_order as _open_manual_market_order_impl,
)
from forex_trader.core.core_manual_limit_order import (
    open_manual_limit_order as _open_manual_limit_order_impl,
)
from forex_trader.core.core_open_trade_from_signal import (
    open_trade_from_signal as _open_trade_from_signal_impl,
)
from forex_trader.core.core_close_trade import (
    CloseTradeContext as _CloseTradeContext,
    get_trading_balance as _get_trading_balance_impl,
    close_trade as _close_trade_impl,
    record_close as _record_close_impl,
    close_all_ladder_legs as _close_all_ladder_legs_impl,
)
from backend.src.services.analytics.orb_report import (
    build_orb_report as _build_orb_report_impl,
    get_orb_target_multiple as _get_orb_target_multiple_impl,
    backtest_orb_target_multiple as _backtest_orb_target_multiple_impl)
from forex_trader.core.core_orb_report import orb_auto_execute as _orb_auto_execute_impl
from forex_trader.core.core_pending_signal_activation import (
    try_activate_pending_signals as _try_activate_pending_signals_impl,
)
from forex_trader.core.core_dpm_bookkeeping import (
    DPMCache as _DPMCache,
    load_dpm_calibrated as _load_dpm_calibrated_impl,
    record_dpm_entry as _record_dpm_entry_impl,
    update_dpm_peak as _update_dpm_peak_impl,
    set_dpm_milestone as _set_dpm_milestone_impl,
    finalize_dpm_record as _finalize_dpm_record_impl,
)

if TYPE_CHECKING:
    from forex_trader.core.telegram_reader import TelegramReader

log = logging.getLogger(__name__)

_TP_NUM_RE   = re.compile(r'^TP(\d+)', re.IGNORECASE)
_TP_CACHE_TTL = 2.5  # seconds — in-memory triggered-TP cache TTL (safe for 1s poll rate)


def _ema(values: list[float], period: int) -> Optional[float]:
    """Exponential moving average of the final value in `values`, or None."""
    if not values or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    ema = values[0]
    for v in values[1:]:
        ema = v * k + ema * (1.0 - k)
    return ema


def _make_bridge(config: dict):
    """Native Windows imports MetaTrader5 directly in this process instead
    of going through mt5_bridge.py as a separate HTTP-served subprocess —
    that split only exists for macOS, where MT5 runs under Wine and the
    main app's own Python can't import MetaTrader5 at all. Falls back to
    the HTTP bridge if native mode is explicitly disabled via config."""
    if _native_bridge_available() and config.get("mt5_native_bridge_enabled", True):
        log.info("Using NativeMT5Bridge (in-process, no HTTP bridge) — native Windows")
        return NativeMT5Bridge()
    return MT5BridgeClient(config.get("mt5_bridge_url", ""))


class SimulationEngine:
    def __init__(self, config: dict):
        import time as _time_mod
        self._started_at = _time_mod.monotonic()
        self._cfg       = config
        self._bridge    = _make_bridge(config)
        self._using_native_bridge = isinstance(self._bridge, NativeMT5Bridge)
        self._tg_reader: Optional["TelegramReader"] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._tp_ladder_fast_task: Optional[asyncio.Task] = None
        self._scanner_task: Optional[asyncio.Task] = None
        self._bot_task:     Optional[asyncio.Task] = None
        self._monitor_running = False
        self._sync_cycle     = 0
        self._profit_cycle   = 0
        self._cal_cycle      = 0
        self._bot_offset     = 0
        self._email_task:    Optional[asyncio.Task] = None
        # Incremented when a trade closes with gross profit > 0; UI polls to trigger sound
        self._profit_sound_seq: int = 0
        # Group tracking for signal scanner
        self._active_group_ids:   dict[str, int] = {}   # group_id_str → slot
        self._active_group_names: dict[str, str] = {}   # group_id_str → name
        # Candle cache — refreshed once per monitor loop cycle, shared across all trades
        self._dpm_candles: list[dict] = []
        # DXY candle cache — refreshed every ~60s for correlation bias
        self._dpm_dxy_candles: list[dict] = []
        # In-memory TP trigger cache to avoid per-trade DB query every monitor cycle.
        # Maps trade_id → (set_of_triggered_tp_nums, cache_timestamp).
        # Invalidated on trade close; expires after _TP_CACHE_TTL regardless.
        # Bundled with _tp_wait_log_ts below into a single TPCache instance
        # (see core_tp_trigger_tracking.py) -- both are in-memory state that
        # isn't derivable from the database.
        self._tp_trigger_cache = _TPCache()
        # Backoff after a failed MT5 partial-close attempt. _check_tp_hits()
        # re-detects the same price-based TP hit every monitor cycle (~1s)
        # with no memory of a recent failure, so without this a persistent
        # broker-side error (market closed, a rejected requote, any transient
        # issue) hammers the MT5 API once per second indefinitely — observed
        # live: 'Partial close failed: 10018' (market closed) retried every
        # ~1.1s with no end condition. Maps (trade_id, tp_num) → last attempt ts.
        self._scale_out_last_fail: dict[tuple[str, int], float] = {}
        # Throttled diagnostic-log timestamps for the TP1-wait branches (see
        # _log_tp_wait_diagnostic) — added 2026-07-03 after several trades ran
        # deep into a favourable TP ladder (per the retrospective max_tp_hit
        # check) with the live tick loop never once registering TP1 hit, no
        # exception anywhere in the process. Point-in-time tick checks have no
        # memory of a price excursion that reverses between two polls; this
        # gives a live, always-on trail of exactly what price/target each
        # trade was being compared against, to catch it happening again with
        # certainty instead of reconstructing it from candles after the fact.
        # (bundled into self._tp_trigger_cache above, as TPCache.wait_log_ts)
        # TP Safety Net: trade_id -> last time a failed/skipped BE-move attempt
        # was alerted. Without this, a persistently-rejected modify_order (or a
        # trade where price has moved back past the point a BE move is even
        # valid) gets retried AND re-alerted every single sweep (every 180s)
        # for as long as the trade stays open — observed live sending the same
        # "TP Safety Net FAILED" message every 3 minutes.
        self._tp_safety_net_last_alert: dict[str, float] = {}
        # MT5 sync: consecutive cycles a tracked ticket has been missing from
        # get_positions() — a single miss can be a transient bridge/IPC
        # hiccup, not a real close, so we require MT5_SYNC_MISS_THRESHOLD
        # consecutive misses before treating the position as actually closed.
        self._mt5_sync_missing_streak: dict[str, int] = {}
        # PendingWatcher: signal_id -> earliest time to retry activation after
        # a failed attempt. Without this, a persistently-rejected order (e.g.
        # a broker-side filling-mode/session issue) gets retried every single
        # monitor cycle (as often as every ~1s while trades are open) with no
        # backoff, hammering the MT5 bridge for a failure that won't resolve
        # itself within seconds.
        self._pending_activation_retry_after: dict[str, float] = {}
        self._dxy_cycle: int = 0
        # DPM bookkeeping in-memory state (which trade_ids have been recorded
        # this session, and the calibrated-params cache with its TTL) -- see
        # core_dpm_bookkeeping.DPMCache.
        self._dpm_cache = _DPMCache()
        # Bridge watchdog — auto-reconnect unless user manually stopped the bridge
        self._bridge_inhibit_reconnect: bool = False
        self._bridge_watchdog_task: Optional[asyncio.Task] = None
        self._max_tp_task: Optional[asyncio.Task] = None
        self._signal_bus_prune_task: Optional[asyncio.Task] = None
        self._tp_safety_net_task: Optional[asyncio.Task] = None
        self._channel_ai_task: Optional[asyncio.Task] = None
        self._ai_model_refresh_task: Optional[asyncio.Task] = None
        self._data_retention_task: Optional[asyncio.Task] = None
        self._reversal_engine_research_task: Optional[asyncio.Task] = None
    def set_telegram_reader(self, reader: "TelegramReader") -> None:
        self._tg_reader = reader

    def set_bridge_inhibit_reconnect(self, inhibit: bool) -> None:
        """Called by the UI when the user manually starts or stops the bridge."""
        self._bridge_inhibit_reconnect = inhibit
        log.info("Bridge auto-reconnect %s", "inhibited (manual stop)" if inhibit else "enabled")

    async def startup(self) -> None:
        db_module._apply_schema()
        await self._bridge.startup()
        self._monitor_running = True
        self._monitor_task        = asyncio.create_task(self._monitor_loop())
        self._tp_ladder_fast_task = asyncio.create_task(self._tp_ladder_fast_loop())
        self._scanner_task        = asyncio.create_task(self._signal_scanner_loop())
        self._bot_task            = asyncio.create_task(self._bot_command_loop())
        self._email_task          = asyncio.create_task(self._email_scheduler_loop())
        self._bridge_watchdog_task = asyncio.create_task(self._bridge_watchdog_loop())
        self._max_tp_task         = asyncio.create_task(self._max_tp_checker_loop())
        self._signal_bus_prune_task = asyncio.create_task(self._signal_bus_prune_loop())
        self._tp_safety_net_task  = asyncio.create_task(self._tp_safety_net_loop())
        self._channel_ai_task     = asyncio.create_task(self._channel_ai_auto_eval_loop())
        self._ai_model_refresh_task = asyncio.create_task(self._ai_model_refresh_loop())
        self._data_retention_task = asyncio.create_task(self._data_retention_loop())
        self._reversal_engine_research_task = asyncio.create_task(self._reversal_engine_research_loop())
        from backend.src.utils.self_healer import SelfHealer
        self._self_healer = SelfHealer(self)
        self._self_healer.start()
        # EA bridge — local TCP listener a companion MQL5 EA connects to.
        # Always started (cheap, idle if nothing connects); actual handoff of
        # any trade only happens when ea_bridge_enabled is on in Risk Settings
        # AND the EA is actually connected — see open_trade()'s handoff check.
        try:
            from forex_trader.core import ea_bridge as _ea_mod
            self._ea_bridge = _ea_mod.EABridge(self)
            await self._ea_bridge.start()
            _ea_mod.set_instance(self._ea_bridge)
        except Exception as _ea_e:
            log.warning("[EA] bridge failed to start: %s", _ea_e)
        log.info("SimulationEngine started")

    async def shutdown(self) -> None:
        self._monitor_running = False
        for t in (self._monitor_task, self._tp_ladder_fast_task, self._scanner_task, self._bot_task,
                  self._email_task, self._bridge_watchdog_task,
                  self._max_tp_task, self._signal_bus_prune_task,
                  self._tp_safety_net_task, self._channel_ai_task,
                  self._ai_model_refresh_task, self._data_retention_task,
                  self._reversal_engine_research_task):
            if t and not t.done():
                t.cancel()
        if hasattr(self, "_self_healer"):
            self._self_healer.stop()
        if hasattr(self, "_ea_bridge"):
            await self._ea_bridge.stop()
        await self._bridge.shutdown()
        log.info("SimulationEngine stopped")

    # ── Market data ───────────────────────────────────────────────────────────

    async def get_tick(self) -> Optional[Tick]:
        return await self._bridge.get_tick()

    async def get_fresh_tick(self) -> Optional[Tick]:
        """Bypass cache — always fetches from the bridge.  Use just before placing orders."""
        return await self._bridge.get_fresh_tick()

    async def get_candles(self, timeframe: str = "M5", count: int = 200) -> list[dict]:
        return await self._bridge.get_candles(timeframe, count)

    async def get_bridge_health(self) -> dict:
        return await self._bridge.get_health()

    async def get_candles_for_symbol(self, symbol: str, timeframe: str, count: int) -> list[dict]:
        return await self._bridge.get_candles_for_symbol(symbol, timeframe, count)

    async def get_mt5_account(self) -> Optional[dict]:
        return await self._bridge.get_account()

    # ── Fee model ─────────────────────────────────────────────────────────────

    def calculate_fees(self, lot_size: float, spread: float, hold_hours: float = 0.0) -> dict:
        # Delegation: this was a verbatim duplicate of core_fees_sizing's copy,
        # kept alive only because reversal_engine_manage.py:86 reaches it via
        # self._main_eng. Two copies of a cost calculation is how the sizing
        # ones drifted apart.
        return _calculate_fees_impl(lot_size, spread, hold_hours)

    # ── P&L ───────────────────────────────────────────────────────────────────

    @staticmethod
    def pnl(direction: str, entry: float, current: float, lots: float) -> float:
        return _pnl_impl(direction, entry, current, lots)

    # ── Lot sizing ────────────────────────────────────────────────────────────

    def suggest_lot_size(self, entry: float, stop_loss: float, balance: float, risk_pct: float) -> float:
        # Delegation, deliberately: this once held its own copy of the maths,
        # and when Max Risk per trade % was added to core_fees_sizing but not
        # here, _scan_messages (which injects this as suggest_lot_size_fn)
        # sized Telegram signals without a ceiling manual orders applied.
        return _suggest_lot_size_impl(entry, stop_loss, balance, risk_pct)

    # ── Account ───────────────────────────────────────────────────────────────

    def get_sim_account(self) -> dict:
        return _get_sim_account_impl()

    def update_sim_balance(self, delta: float) -> None:
        _update_sim_balance_impl(delta)

    def reset_simulation(self) -> None:
        starting = float(self._cfg.get("starting_balance", 1000.0))
        _reset_simulation_impl(starting)

    # ── Signals ───────────────────────────────────────────────────────────────

    def create_signal(self, source_name: str, direction: str, entry_low: float,
                      entry_high: float, stop_loss: float,
                      tp1=None, tp2=None, tp3=None, tp4=None, tp5=None,
                      tp6=None, tp7=None, tp8=None,
                      lot_size=None, risk_pct=None, notes: str = "") -> dict:
        return _create_signal_impl(
            source_name, direction, entry_low, entry_high, stop_loss,
            tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8, lot_size, risk_pct, notes,
        )

    def get_signals(self, status: Optional[str] = None) -> list[dict]:
        return _get_signals_impl(status)

    def activate_signal(self, signal_id: str) -> None:
        _activate_signal_impl(signal_id)

    def cancel_signal(self, signal_id: str) -> None:
        _cancel_signal_impl(signal_id)

    # ── Trade management ──────────────────────────────────────────────────────

    async def open_trade(self, signal_id: str, direction: str, entry_low: float, entry_high: float,
                         stop_loss: float,
                         tp1=None, tp2=None, tp3=None, tp4=None, tp5=None,
                         tp6=None, tp7=None, tp8=None,
                         lot_size: float = 0.01, tick: Optional[Tick] = None,
                         strategy: str = STRATEGY_SCALE_OUT,
                         tg_source: Optional[str] = None,
                         mt5_tp_override: Optional[float] = None) -> dict:
        return await _open_trade_impl(
            self._bridge, signal_id, direction, entry_low, entry_high, stop_loss,
            tp1=tp1, tp2=tp2, tp3=tp3, tp4=tp4, tp5=tp5, tp6=tp6, tp7=tp7, tp8=tp8,
            lot_size=lot_size, tick=tick, strategy=strategy, tg_source=tg_source,
            mt5_tp_override=mt5_tp_override,
        )

    async def _get_trading_balance(self) -> float:
        """Current account balance for lot-size calculations.
        Prefers the live MT5 bridge balance; falls back to the local simulation account."""
        return await _get_trading_balance_impl(
            self._bridge, self._cfg.get("starting_balance", 1000.0)
        )

    def _make_close_trade_ctx(self) -> _CloseTradeContext:
        return _CloseTradeContext(
            self._bridge,
            starting_balance=self._cfg.get("starting_balance", 1000.0),
            tp_cache=self._tp_trigger_cache,
            scale_out_last_fail=self._scale_out_last_fail,
            tp_safety_net_last_alert=self._tp_safety_net_last_alert,
            on_profit=lambda: setattr(self, "_profit_sound_seq", self._profit_sound_seq + 1),
            schedule_profit_sync=self._schedule_profit_sync,
            background_close_commentary=self._background_close_commentary,
        )

    async def open_trade_from_signal(self, signal_id: str,
                                     lot_size_override: Optional[float] = None,
                                     tick: Optional["Tick"] = None,
                                     age_lot_mult: float = 1.0) -> dict:
        return await _open_trade_from_signal_impl(
            self._bridge, signal_id, lot_size_override=lot_size_override, tick=tick,
            age_lot_mult=age_lot_mult, dpm_candles=self._dpm_candles,
            starting_balance=self._cfg.get("starting_balance", 1000.0),
            background_open_commentary=self._background_open_commentary,
        )

    async def open_manual_market_order(
        self,
        direction: str,
        stop_loss: Optional[float] = None,
        lot_size: Optional[float] = None,
        strategy: Optional[str] = None,
        take_profit: Optional[float] = None,
        source_name: str = "manual_market",
    ) -> dict:
        """
        Place an immediate market order from the UI without a pre-existing signal.

        take_profit/source_name: used by the ORB/IVB Report tab's Execute
        Trade button to pass its own computed target and tag the trade
        distinctly from a plain Market Order — every other caller leaves
        these at their defaults (no TP, "manual_market" tag), unchanged from
        before this was added.

        - direction: 'BUY' or 'SELL'
        - stop_loss: explicit SL price; if None and DPM is enabled, an ATR-based SL is
          calculated automatically.  If None and DPM is disabled, raises ValueError.
        - lot_size: explicit lots; if None, auto-calculated from risk settings.

        Returns the same dict as open_trade(): trade_id, mt5_ticket, entry_price, strategy.
        """
        return await _open_manual_market_order_impl(
            self._bridge, direction, stop_loss=stop_loss, lot_size=lot_size,
            strategy=strategy, take_profit=take_profit, source_name=source_name,
            starting_balance=self._cfg.get("starting_balance", 1000.0),
            background_open_commentary=self._background_open_commentary,
        )

    async def open_manual_limit_order(
        self,
        direction: str,
        entry_low: float,
        entry_high: float,
        stop_loss: float,
        tp1: Optional[float] = None, tp2: Optional[float] = None,
        tp3: Optional[float] = None, tp4: Optional[float] = None,
        tp5: Optional[float] = None, tp6: Optional[float] = None,
        tp7: Optional[float] = None, tp8: Optional[float] = None,
        lot_size: Optional[float] = None,
        notes: str = "",
    ) -> dict:
        """Place a genuine resting BuyLimit/SellLimit via the EA from the
        Trading > Limit Order form — see core_manual_limit_order.py."""
        return await _open_manual_limit_order_impl(
            self._bridge, direction, entry_low, entry_high, stop_loss,
            tp1=tp1, tp2=tp2, tp3=tp3, tp4=tp4, tp5=tp5, tp6=tp6, tp7=tp7, tp8=tp8,
            lot_size=lot_size, notes=notes,
            starting_balance=self._cfg.get("starting_balance", 1000.0),
        )

    async def _close_all_ladder_legs(self, trade_id: str, row: dict, legs: list[dict],
                                     reason: str) -> dict:
        """Manual/forced full close of an Adaptive Runner ladder trade —
        closes every still-open leg at market (each is its own MT5 ticket),
        sums their real P&L, and records the parent trade closed. Used by
        close_trade() so /close and the UI's Close button don't orphan
        every leg but the anchor."""
        return await _close_all_ladder_legs_impl(
            trade_id, row, legs, reason, self._make_close_trade_ctx()
        )

    async def close_trade(self, trade_id: str, reason: str = "manual_close") -> dict:
        return await _close_trade_impl(trade_id, reason, self._make_close_trade_ctx())

    async def _record_close(self, trade_id: str, close_price: float, reason: str) -> dict:
        return await _record_close_impl(trade_id, close_price, reason, self._make_close_trade_ctx())

    async def partial_close_trade(self, trade_id: str, lots_to_close: float,
                                  close_price: float, reason: str = "TP") -> dict:
        return await _partial_close_trade_impl(trade_id, lots_to_close, close_price, reason)

    def get_open_trades(self) -> list[dict]:
        return _get_open_trades_impl()

    async def get_untracked_mt5_positions(self) -> list[dict]:
        """
        Return live MT5 positions that the app has no open trade record for.
        These are positions opened directly in MT5 (not via the app's signal system).
        Each dict is the raw bridge position payload plus a `_untracked=True` marker.
        """
        return await _get_untracked_mt5_positions_impl(self._bridge)

    def get_all_trades(self, status: Optional[str] = None, limit: int = 100) -> list[dict]:
        return _get_all_trades_impl(status, limit)

    # ── Performance ───────────────────────────────────────────────────────────

    def compute_performance(self) -> dict:
        starting = float(self._cfg.get("starting_balance", 1000.0))
        return _compute_performance_impl(starting)

    # ── TP trigger tracking ───────────────────────────────────────────────────

    async def _get_triggered_tps(self, trade_id: str) -> set[int]:
        return await _get_triggered_tps_impl(self._tp_trigger_cache, trade_id)

    def _last_closed_tp(self, trade_id: str) -> Optional[int]:
        return _last_closed_tp_impl(trade_id)

    _TP_WAIT_LOG_INTERVAL = 60.0  # seconds between diagnostic log lines per trade

    def _log_tp_wait_diagnostic(
        self, trade_id: str, tag: str, direction: str,
        current_price: float, target_price: float, hit: bool,
    ) -> None:
        """Throttled, always-on (INFO, not DEBUG) visibility into the TP1-wait
        comparison every strategy handler runs every poll. One line per trade
        per _TP_WAIT_LOG_INTERVAL, plus always logs the transition to hit=True."""
        _log_tp_wait_diagnostic_impl(
            self._tp_trigger_cache, trade_id, tag, direction, current_price, target_price, hit,
        )

    def _check_sl(self, trade: dict, tick: Tick) -> Optional[tuple]:
        return _check_sl_impl(trade, tick)

    async def _check_tp_hits(self, trade: dict, tick: Tick) -> list[tuple]:
        return await _check_tp_hits_impl(self._tp_trigger_cache, trade, tick)

    # ── Strategy handlers ─────────────────────────────────────────────────────

    def _get_remaining_lots(self, trade_id: str) -> float:
        """Read current remaining_lots from DB to avoid stale snapshot."""
        return _get_remaining_lots_impl(trade_id)

    async def _handle_scale_out(self, trade: dict, tick: Tick) -> None:
        return await _handle_scale_out_impl(
            trade, tick, self._bridge, self._tp_trigger_cache, self._scale_out_last_fail,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_be_runner(self, trade: dict, tick: Tick) -> None:
        return await _handle_be_runner_impl(
            trade, tick, self._bridge, self._tp_trigger_cache, self._scale_out_last_fail,
            dpm_candles=self._dpm_candles, close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_trail_stop(self, trade: dict, tick: Tick) -> None:
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
        return await _handle_trail_stop_impl(trade, tick, self._bridge, self._tp_trigger_cache)

    async def _handle_protected_scale(self, trade: dict, tick: Tick) -> None:
        return await _handle_protected_scale_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_conservative(self, trade: dict, tick: Tick) -> None:
        """
        Conservative strategy (fixed-point levels; signal SL/TP ignored after fill):

          SL   = fill ∓ 5 pts
          TP1  = fill ± 3 pts  →  close 80 % of position
          After TP1: trail remaining 20 % with a fixed 3-pt stop,
                     floored at entry price (breakeven).
        """
        return await _handle_conservative_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_orb_fixed(self, trade: dict, tick: Tick) -> None:
        """
        ORB/IVB Report trades — no management beyond the reload-zone setup
        itself. SL is already handled generically by _check_sl() in
        _monitor_loop before any strategy handler ever runs; this handler's
        only job is a full close (no partials, no BE-move, no trailing) the
        instant tp1 (the report's own target) is hit. Deliberately bypasses
        DPM even when the global DPM toggle is on — see the dispatch order in
        _monitor_loop — since the whole point of this strategy is "exactly
        the setup the report computed, nothing recalculated."
        """
        return await _handle_orb_fixed_impl(trade, tick, self._bridge, self._tp_trigger_cache)

    async def _handle_scalp_runner(self, trade: dict, tick: Tick) -> None:
        """
        Scalp Runner strategy — three phases:

          SL   = fill ∓ 10 pts
          TP1  = fill ± 3 pts  →  close 50 % of position (SL untouched)
          TP2  = fill ± 4 pts  →  move SL to entry (breakeven); trailing starts
          After TP2: trail remaining 50 % with a fixed 3-pt stop,
                     floored at entry price (breakeven).
        """
        return await _handle_scalp_runner_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_signal_climber(self, trade: dict, tick: Tick) -> None:
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
        return await _handle_signal_climber_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_reversal_runner(self, trade: dict, tick: Tick) -> None:
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
        return await _handle_reversal_runner_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_adaptive_runner(self, trade: dict, tick: Tick) -> None:
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

        Called from _tp_ladder_fast_loop (sub-second polling), not
        _monitor_loop — see that loop's docstring for why: an earlier
        2026-07-17 attempt at genuine resting-broker-side TP orders (N
        separate MT5 tickets per signal) was reverted the same day after
        causing too much MT5/UI clutter for what should read as one trade.
        """
        return await _handle_adaptive_runner_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_adaptive_runner_2(self, trade: dict, tick: Tick) -> None:
        """
        Adaptive Runner 2: fixed 10pt SL (not derived from the signal at
        all -- see _ADAPTIVE2_SL_PT in core_signal_resolution.py), the reference channel
        Runner's back-loaded close schedule (_GDVR_PCTS), and a different
        SL trail: breakeven at TP2 (be_at_pos=1), then from TP3 onward SL
        steps to the midpoint of the two TPs before the one just cleared
        instead of the single immediately-previous TP price. See
        STRATEGY_DESCRIPTIONS[STRATEGY_ADAPTIVE_RUNNER_2] in core/models.py.

        Called from _tp_ladder_fast_loop, same as the other three
        _run_tp_ladder-shaped strategies — see _handle_adaptive_runner's
        docstring for why (sub-second polling, not _monitor_loop).
        """
        return await _handle_adaptive_runner_2_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_limit_runner(self, trade: dict, tick: Tick) -> None:
        """
        Limit Runner: Python-side fallback ladder handler, used only if the
        EA goes unhealthy mid-trade — every Limit Runner trade is placed as
        a genuine EA pending order to begin with (core_limit_order_signal.py
        only fires when the EA is connected), so this is a reclaim path, not
        the normal case. Splits the position across however many numeric TPs
        the originating signal actually had (varies message to message,
        unlike every other ladder strategy's fixed 8-TP table) and, if the
        signal carried a literal "TP OPEN" line, leaves the configured
        reserve permanently open once the last numeric TP closes its share
        instead of closing everything. See
        STRATEGY_DESCRIPTIONS[STRATEGY_LIMIT_RUNNER] in core/models.py.

        Called from _tp_ladder_fast_loop, same as the other ladder
        strategies — see _handle_adaptive_runner's docstring for why
        (sub-second polling, not _monitor_loop).
        """
        return await _handle_limit_runner_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _run_tp_ladder(
        self, trade: dict, tick: Tick, pcts_table: dict[int, list[float]], log_tag: str,
        be_at_pos: int = 0,
    ) -> None:
        """Shared TP-ladder walk used by Signal Climber and Reversal Runner.

        Closes fractions of the original lot at each signal TP (per pcts_table,
        keyed by TP count). SL is left untouched until the TP at index
        `be_at_pos` (0 = TP1, 1 = TP2, ...) is hit, at which point it moves to
        breakeven; every subsequent TP after that trails SL to the previous
        TP's price.
        """
        return await _run_tp_ladder_impl(
            trade, tick, pcts_table, log_tag, self._bridge, self._tp_trigger_cache,
            be_at_pos=be_at_pos, close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_conservative_trial(self, trade: dict, tick: Tick) -> None:
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
        return await _handle_conservative_trial_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    async def _handle_no_sl_scale(self, trade: dict, tick: Tick) -> None:
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
        return await _handle_no_sl_scale_impl(
            trade, tick, self._bridge, self._tp_trigger_cache,
            close_full_after_tps=self._close_full_after_tps,
        )

    # ── DPM helpers ──────────────────────────────────────────────────────────

    def _load_dpm_calibrated(self) -> dict:
        """
        Load calibrated DPM multipliers from app_config into a flat dict keyed
        "{session}_{momentum_bucket}".  Refreshed at most once per 10 minutes.
        """
        return _load_dpm_calibrated_impl(self._dpm_cache)

    def _record_dpm_entry(self, trade: dict, params: dict) -> None:
        """Snapshot market state and DPM parameters on the first cycle for a trade."""
        _record_dpm_entry_impl(self._dpm_cache, trade, params)

    def _update_dpm_peak(self, trade_id: str, unrealized_pnl: float) -> None:
        """Keep the high-water P&L for this trade so calibration can compute capture ratio."""
        _update_dpm_peak_impl(trade_id, unrealized_pnl)

    def _set_dpm_milestone(self, trade_id: str, column: str) -> None:
        """Mark a milestone column (reached_be, reached_tp1, reached_tp2) as 1."""
        _set_dpm_milestone_impl(trade_id, column)

    def _finalize_dpm_record(self, trade_id: str, close_price: float,
                             exit_type: str, final_pnl: float) -> None:
        """Write close-time fields to the DPM performance record."""
        _finalize_dpm_record_impl(trade_id, close_price, exit_type, final_pnl)

    async def _run_dpm_calibration(self) -> None:
        """
        Calibrate DPM multipliers from completed trade history.
        Triggers when at least 5 new closed DPM trades exist since the last run,
        with a 2-hour minimum gap to prevent back-to-back runs in busy sessions.
        Results are stored in dpm_calibration and loaded into _dpm_calibrated.
        """
        return await _run_dpm_calibration_impl(self._dpm_cache)

    # ── Dynamic Position Management ──────────────────────────────────────────

    async def _handle_dynamic_position_management(self, trade: dict, tick: Tick) -> None:
        """
        Adaptive DPM — all parameters computed from ATR, session, momentum and
        structural swing levels.  No manual tuning required.

        Priority order each tick:
          1. If TP1 cleared: partial close (momentum-scaled %) → SL to entry.
          2. If unrealised P&L >= adaptive BE trigger: SL to entry.
          3. If SL already at entry: adaptive trailing SL (arithmetic or structural,
             whichever locks in more profit).
          4. Mark TP2+ chips for UI (no close — trailing handles the exit).
        """
        return await _handle_dynamic_position_management_impl(
            trade, tick, self._bridge, self._tp_trigger_cache, self._dpm_cache,
            self._dpm_candles, self._dpm_dxy_candles,
        )

    # ── Pause check ───────────────────────────────────────────────────────────

    def is_trading_paused(self) -> bool:
        return _is_trading_paused_impl()

    @staticmethod
    def _price_in_entry_range(direction: str, entry_low: float, entry_high: float,
                               tick: "Tick") -> bool:
        return _price_in_entry_range_impl(direction, entry_low, entry_high, tick)

    # ── Pre-trade filters ─────────────────────────────────────────────────────

    def _check_pre_trade_filters(
        self,
        direction: str,
        entry_low: float,
        entry_high: float,
        stop_loss: float,
        tp1,
        actual_price: Optional[float] = None,
        source_name: str = "",
    ) -> Optional[str]:
        """
        Evaluate two structural risk filters before opening any trade.

        Filter 1 — Minimum R:R on TP1 (0.75 : 1)
            Compares TP1 distance against SL distance from the reference price.
            Skipped for channels in RR_BYPASS_SOURCES that supply their own
            TP/SL levels from a signal provider service.

        Filter 2 — Directional cap (max 2 unprotected same-direction trades)
            Blocks a new trade when 2 or more currently-open trades in the same
            direction have not yet reached breakeven (sl_moved_to_be = 0).

        Returns an error string when a filter fires, None when the trade may proceed.
        """
        return _check_pre_trade_filters_impl(
            direction, entry_low, entry_high, stop_loss, tp1,
            actual_price=actual_price, source_name=source_name,
        )

    # ── Tier 1 Risk Governor ───────────────────────────────────────────────────
    # Deterministic, app-wide safety layer. When risk_governor_enabled is set it
    #   (A) sizes every position from risk %, (B) enforces a hard per-trade $ ceiling,
    #   (C) halts on the daily-loss limit, (D) cools down after a loss streak, and
    #   (E) rejects trades below a minimum TP1 R:R or beyond the directional cap.
    # All thresholds come from existing vantage_risk_settings columns. When the
    # toggle is off none of this runs and strategies behave exactly as before.

    def _rg_day_start_ts(self) -> float:
        """Epoch of the current trading day's start, in broker time (UTC+3)."""
        return _rg_day_start_ts_impl()

    def _rg_size_and_check(self, *, direction: str, ref_price: float,
                           stop_loss: float, tp1, strategy: str,
                           atr: float, balance: float, rs: dict):
        """Return (lot_size, None) when a trade is allowed, or (None, reason)."""
        return _rg_size_and_check_impl(
            direction=direction, ref_price=ref_price, stop_loss=stop_loss,
            tp1=tp1, strategy=strategy, atr=atr, balance=balance, rs=rs,
        )

    def _rg_check_halt(self, rs: dict, balance: float) -> Optional[str]:
        """Return a reason string when the Risk Governor should halt trading.

        balance must be the live MT5 balance (see the caller in _record_close)
        — this used to read vantage_simulation_account directly, which is this
        app's own internally-computed P&L ledger and can drift significantly
        from the real account (confirmed 2026-07-07: $707 sim vs $1122 real),
        producing a false drawdown-halt reason based on a number unrelated to
        the account's actual health.
        """
        return _rg_check_halt_impl(rs, balance)

    def _rg_apply_halts_on_close(self, rs: dict, balance: float) -> None:
        """After a close, trip the pause flag if a circuit breaker fired."""
        return _rg_apply_halts_on_close_impl(rs, balance)

    # ── Pending signal watcher ────────────────────────────────────────────────

    async def _try_activate_pending_signals(self, tick: "Tick", rs: dict) -> bool:
        """Activate queued signals whose entry zone has been reached.

        Called every monitor-loop cycle. Signals that have not
        filled within 2 minutes are expired — the market context will have
        changed and the zone level is stale for a scalping strategy.

        Exception: Reversal Runner's edge depends on zone signals that often
        take well over an hour to fill (median ~101min in the backtest this
        strategy is derived from) — signals get _GDVR_PENDING_EXPIRY_SEC (4h)
        instead of the default whenever Reversal Runner applies to that signal,
        either as the global Active Strategy or as a per-channel override on
        the signal's own source channel (Channel Strategy tab) — a signal
        from a channel overridden to reversal_runner must get the long window
        even while some other channel is driving the global strategy, or
        every the reference channel zone signal expires in 2 minutes before the ~101min
        median fill time, silently starving that strategy of any fills.

        Returns whether any signal was pending at the start of this cycle —
        _monitor_loop uses this to stay on the fast (1s) poll cadence while a
        zone-fill is being awaited, instead of dropping to the idle 5s poll
        just because no trade happens to be open yet (see _monitor_loop).
        """
        return await _try_activate_pending_signals_impl(
            tick, rs, self._bridge, self._pending_activation_retry_after, self._dpm_candles,
            starting_balance=self._cfg.get("starting_balance", 1000.0),
            background_open_commentary=self._background_open_commentary,
        )

    # ── Monitor loop ──────────────────────────────────────────────────────────

    async def _monitor_loop(self) -> None:
        _has_open_trades = False      # tracked across cycles for adaptive sleep
        _has_pending_signals = False  # a queued zone-fill signal also needs fast polling
        while self._monitor_running:
            try:
                tick = await self.get_tick()
                if tick:
                    open_trades = await db_module.to_db_thread(self.get_open_trades)
                    _has_open_trades = bool(open_trades)
                    rs = await db_module.to_db_thread(db_module.get_risk_settings)
                    profit_close_usd = float(rs.get("profit_close_usd", 0.0) or 0.0)
                    # Refresh candle cache once per cycle (shared by all DPM trade handlers)
                    if bool(rs.get("dpm_enabled", 0)) and open_trades:
                        try:
                            self._dpm_candles = await self.get_candles("M5", 30)
                        except Exception:
                            pass
                        # DXY candles refreshed every ~60s (12 cycles × 5s)
                        self._dxy_cycle += 1
                        if self._dxy_cycle >= 12:
                            self._dxy_cycle = 0
                            try:
                                dxy_sym = await db_module.to_db_thread(db_module.get_app_config, "dxy_symbol") or "USDX"
                                fetched = await self._bridge.get_candles_for_symbol(
                                    dxy_sym, "M5", 20
                                )
                                if fetched:
                                    self._dpm_dxy_candles = fetched
                            except Exception:
                                pass
                    _eff_strategy, _ooh_active = db_module.get_effective_strategy(rs)
                    for trade in open_trades:
                        # OOH overrides the per-trade strategy when the window is active.
                        strategy = _eff_strategy if _ooh_active else trade.get("strategy", STRATEGY_SCALE_OUT)
                        hit = self._check_sl(trade, tick)
                        if hit:
                            trade_id, price, reason = hit
                            await _reconcile_sl_hit_impl(
                                trade, tick, price, reason, self._bridge, self._make_close_trade_ctx(),
                            )
                            continue
                        # Profit-close target check — cumulative (partials taken + unrealised).
                        if await _check_profit_close_target_impl(
                            trade, tick, profit_close_usd, self._bridge, self._make_close_trade_ctx(),
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
                                await self._handle_orb_fixed(trade, tick)
                            elif dpm_enabled and not _ooh_active:
                                await self._handle_dynamic_position_management(trade, tick)
                            elif strategy == STRATEGY_BE_RUNNER:
                                await self._handle_be_runner(trade, tick)
                            elif strategy == STRATEGY_TRAIL_STOP:
                                await self._handle_trail_stop(trade, tick)
                            elif strategy == STRATEGY_PROTECTED_SCALE:
                                await self._handle_protected_scale(trade, tick)
                            elif strategy == STRATEGY_CONSERVATIVE:
                                await self._handle_conservative(trade, tick)
                            elif strategy == STRATEGY_SCALP_RUNNER:
                                await self._handle_scalp_runner(trade, tick)
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
                                await self._handle_no_sl_scale(trade, tick)
                            elif strategy == STRATEGY_CONSERVATIVE_TRIAL:
                                await self._handle_conservative_trial(trade, tick)
                            else:
                                await self._handle_scale_out(trade, tick)
                        except Exception as exc:
                            log.warning("Strategy handler [%s] error: %s", strategy, exc)

                    # ── Pending signal watcher ───────────────────────────────
                    if bool(rs.get("auto_execute_signals", 0)) and not await db_module.to_db_thread(self.is_trading_paused):
                        try:
                            _has_pending_signals = await self._try_activate_pending_signals(tick, rs)
                        except Exception as exc:
                            log.debug("[PendingWatcher] Error: %s", exc)
                            _has_pending_signals = False
                    else:
                        _has_pending_signals = False

                    # ── IME timeout watchdog ──────────────────────────────────
                    # Auto-assigns TP/SL to IME trades with no follow-up after 3 min
                    try:
                        await self._ime_timeout_watchdog(tick)
                    except Exception as exc:
                        log.debug("[IME-timeout] Watchdog error: %s", exc)
            except Exception as exc:
                log.warning("Monitor loop error: %s", exc)

            self._sync_cycle += 1
            if self._sync_cycle >= 6:
                self._sync_cycle = 0
                try:
                    await self._sync_closed_mt5_positions()
                except Exception as e:
                    log.debug("MT5 sync error: %s", e)

            self._profit_cycle += 1
            if self._profit_cycle >= 24:
                self._profit_cycle = 0
                try:
                    await self._profit_sweep()
                except Exception as e:
                    log.debug("Profit sweep error: %s", e)

            # DPM self-calibration: attempt once per hour.
            # Cal_cycle threshold adapts to the current sleep interval:
            # 720 × 5s = 3600s idle, 3600 × 1s = 3600s active.
            _fast_poll = _has_open_trades or _has_pending_signals
            self._cal_cycle += 1
            _cal_threshold = 720 if not _fast_poll else 3600
            if self._cal_cycle >= _cal_threshold:
                self._cal_cycle = 0
                try:
                    rs_cal = await db_module.to_db_thread(db_module.get_risk_settings)
                    if bool(rs_cal.get("dpm_enabled", 0)):
                        asyncio.create_task(self._run_dpm_calibration())
                except Exception as e:
                    log.debug("DPM calibration trigger error: %s", e)

            # Fast poll (1s) when trades are open OR a queued signal is waiting
            # for its zone to be re-entered — a GD2-style queued signal with no
            # other trade open would otherwise only get checked every 5s, adding
            # up to ~5s of pure polling latency on top of everything else once
            # price actually returns to the zone. Slow poll (5s) only when
            # there is truly nothing time-sensitive to watch.
            await asyncio.sleep(1 if _fast_poll else 5)

    # ── Fast TP-ladder polling ──────────────────────────────────────────────

    _TP_LADDER_STRATEGIES = (
        STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER,
        STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
        STRATEGY_LIMIT_RUNNER,
    )
    # 50ms — below this, polling faster stops buying real coverage: MT5's own
    # XAUUSD tick feed doesn't reliably update faster than this even in
    # volatile conditions, and every fetch shares NativeMT5Bridge._call()'s
    # single global lock with every other bridge operation (order placement,
    # position queries, MT5 sync) — polling tighter just means more lock
    # contention for no additional crossing coverage.
    _TP_LADDER_POLL_INTERVAL = 0.05  # seconds

    async def _tp_ladder_fast_loop(self) -> None:
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
        while self._monitor_running:
            try:
                rs = await db_module.to_db_thread(db_module.get_risk_settings)
                if not bool(rs.get("dpm_enabled", 0)):
                    open_trades = await db_module.to_db_thread(self.get_open_trades)
                    ladder_trades = [t for t in open_trades
                                     if t.get("strategy") in self._TP_LADDER_STRATEGIES]
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
                            from forex_trader.core import ea_bridge as _ea_mod
                            _ea = _ea_mod.get_instance()
                            _ea_healthy = _ea is not None and _ea.is_ea_healthy()
                        except ImportError:
                            _ea_healthy = False
                        if _ea_healthy:
                            ladder_trades = [t for t in ladder_trades if t.get("managed_by") != "ea"]
                    if ladder_trades:
                        tick = await self.get_fresh_tick()
                        if tick:
                            for trade in ladder_trades:
                                try:
                                    strat = trade["strategy"]
                                    if strat == STRATEGY_SIGNAL_CLIMBER:
                                        await self._handle_signal_climber(trade, tick)
                                    elif strat == STRATEGY_REVERSAL_RUNNER:
                                        await self._handle_reversal_runner(trade, tick)
                                    elif strat == STRATEGY_ADAPTIVE_RUNNER_2:
                                        await self._handle_adaptive_runner_2(trade, tick)
                                    elif strat == STRATEGY_LIMIT_RUNNER:
                                        await self._handle_limit_runner(trade, tick)
                                    else:
                                        await self._handle_adaptive_runner(trade, tick)
                                except Exception as exc:
                                    log.warning("[TP-ladder-fast] handler error trade=%s: %s",
                                               trade.get("trade_id"), exc)
            except Exception as exc:
                log.warning("[TP-ladder-fast] loop error: %s", exc)
            await asyncio.sleep(self._TP_LADDER_POLL_INTERVAL)

    # ── MT5 position sync ─────────────────────────────────────────────────────

    MT5_SYNC_MISS_THRESHOLD = 2  # consecutive cycles before treating a missing ticket as a real close

    async def _sync_closed_mt5_positions(self) -> None:
        if not self._bridge.is_configured():
            return
        def _fetch_open_trades():
            with db_module.db() as conn:
                # Excludes managed_by='ea' trades: the native EA already pushes
                # its own trade_closed event the moment it detects the position
                # gone (ea_bridge.py's _on_trade_closed), which calls the same
                # _record_close() + sends the same Telegram alert this function
                # would otherwise send a second time. _record_close() has no
                # idempotency guard, so without this exclusion both paths race
                # to close the trade and the user gets a duplicate "Stop Loss
                # Hit" message (confirmed live, ticket 1572181515, 2026-07-10 —
                # single DB row, single node, no dual-node involvement at all).
                # Non-EA-managed trades still need this poll as their only
                # detection of an out-of-band MT5-side close.
                # Also excludes any trade with vantage_ladder_legs rows (Adaptive
                # Runner ladder trades): this trade's own mt5_ticket is only
                # leg 1/the anchor, so once leg 1 closes at its own native TP
                # this loop would see the anchor ticket vanish, compute
                # closed_volume from JUST that leg, and — since
                # _handle_adaptive_runner_ladder had already subtracted leg 1's
                # lots from remaining_lots — call partial_close_trade() a
                # SECOND time for the same lots every monitor cycle (the
                # ticket never reappears, so the miss-streak keeps re-firing),
                # draining remaining_lots to 0 and marking the whole parent
                # trade closed within seconds even though legs 2-N are still
                # genuinely open in MT5. Once the parent shows status!='open',
                # _handle_adaptive_runner_ladder (which owns real per-leg
                # closure detection AND survivor SL-trailing) stops being
                # invoked at all for it, orphaning the remaining legs from
                # all further management. Confirmed live 2026-07-17: trades
                # b7dcacbe/bed873ca both closed within ~30s of leg 1, legs
                # 2-N left untracked until a SEPARATE bug (the untracked-
                # position importer not checking vantage_ladder_legs; also
                # fixed below) re-discovered them as phantom duplicate trades.
                return [db_module.row_to_dict(r) for r in conn.execute(
                    "SELECT * FROM vantage_simulated_trades WHERE status='open' AND mt5_ticket IS NOT NULL "
                    "AND (managed_by IS NULL OR managed_by != 'ea') "
                    "AND trade_id NOT IN (SELECT DISTINCT trade_id FROM vantage_ladder_legs)"
                ).fetchall()]
        open_trades = await db_module.to_db_thread(_fetch_open_trades)
        if not open_trades:
            return
        live_positions = await self._bridge.get_positions()

        # get_positions() returns [] both when the bridge is offline and when
        # there are genuinely no open positions.  Before treating an empty list
        # as "all positions closed", confirm the bridge is actually connected.
        # If MT5 is unreachable (terminal closed, maintenance, bridge restart)
        # an empty list is ambiguous — skip the sync to avoid falsely closing
        # live trades and misfiring Telegram alerts.
        if not live_positions:
            health = await self._bridge.get_health()
            if not health.get("connected", False):
                log.debug("MT5 sync: skipping — bridge not connected (live_positions empty)")
                return

        live_tickets = {int(p["ticket"]) for p in live_positions}
        deals_by_pos: dict[int, list] = {}
        all_deals = await self._bridge.get_deal_history(7)
        for d in all_deals:
            pid = d.get("position_id")
            if pid:  # excludes None and 0
                deals_by_pos.setdefault(int(pid), []).append(d)
        tick = await self.get_tick()
        for trade in open_trades:
            ticket = int(trade["mt5_ticket"])
            if ticket in live_tickets:
                self._mt5_sync_missing_streak.pop(trade["trade_id"], None)
                continue

            # A ticket can be transiently absent from get_positions() (bridge
            # lock contention, a momentary IPC hiccup) without the position
            # actually having closed. Require MT5_SYNC_MISS_THRESHOLD
            # consecutive misses before acting, so one bad read can't
            # falsely mark a genuinely-open trade as closed.
            streak = self._mt5_sync_missing_streak.get(trade["trade_id"], 0) + 1
            self._mt5_sync_missing_streak[trade["trade_id"]] = streak
            if streak < self.MT5_SYNC_MISS_THRESHOLD:
                log.warning(
                    "MT5 sync: ticket=%s missing from live positions (%d/%d) — "
                    "not yet treating as closed",
                    ticket, streak, self.MT5_SYNC_MISS_THRESHOLD,
                )
                continue

            deals = await self._bridge.get_position_history(ticket)
            if not deals:
                deals = deals_by_pos.get(ticket, [])
            close_price = None
            reason = "MT5_close"
            close_deals: list = []
            if deals:
                # entry 1=OUT, 2=INOUT, 3=OUT_BY (close-by-opposite on hedge accounts)
                close_deals = [d for d in deals if d.get("entry") in (1, 2, 3)]
                if not close_deals:
                    open_type = 0 if trade["direction"].upper() == "BUY" else 1
                    close_deals = [d for d in deals if d.get("type") != open_type]
                if close_deals:
                    best = max(close_deals, key=lambda d: d.get("time", 0))
                    close_price = best.get("price")
                    comment = (best.get("comment") or "").lower()
                    if "sl" in comment or "stop" in comment:
                        reason = "SL"
                    elif "tp" in comment or "take" in comment:
                        reason = "MT5_sync_TP"
            if close_price is None:
                close_price = (tick.bid if trade["direction"].upper() == "BUY" else tick.ask) if tick \
                    else float(trade.get("entry_price") or 0)
            try:
                # ── Partial-close detection ───────────────────────────────────────
                # If MT5 closed fewer lots than we are tracking, record a partial
                # close and keep the trade open rather than falsely marking it done.
                if close_deals:
                    closed_volume = round(
                        sum(float(d.get("volume", 0)) for d in close_deals), 4
                    )
                    remaining_lots = round(float(trade["remaining_lots"]), 4)
                    if closed_volume < remaining_lots - 0.001:
                        partial_profit = round(sum(
                            float(d.get("profit", 0)) + float(d.get("swap", 0))
                            + float(d.get("fee", 0))
                            for d in close_deals
                        ), 2)
                        log.info(
                            "MT5 sync: partial close trade=%s ticket=%s "
                            "closed=%.4f remaining=%.4f profit=%.2f",
                            trade["trade_id"], ticket, closed_volume,
                            remaining_lots - closed_volume, partial_profit,
                        )
                        await self.partial_close_trade(
                            trade["trade_id"], closed_volume,
                            float(close_price), f"MT5_{reason}",
                        )
                        # Update ticket if the continuing position has a new ticket
                        new_remaining = round(remaining_lots - closed_volume, 4)
                        for lp in live_positions:
                            lp_vol = round(float(lp.get("volume", 0)), 4)
                            lp_ticket = int(lp.get("ticket", 0))
                            if abs(lp_vol - new_remaining) < 0.001 and lp_ticket != ticket:
                                def _reassign_ticket(lp_ticket=lp_ticket):
                                    with db_module.db() as conn:
                                        conn.execute(
                                            "UPDATE vantage_simulated_trades "
                                            "SET mt5_ticket=? WHERE trade_id=?",
                                            (lp_ticket, trade["trade_id"]),
                                        )
                                await db_module.to_db_thread(_reassign_ticket)
                                log.info("MT5 sync: ticket %s → %s (partial close continues)",
                                         ticket, lp_ticket)
                                break
                        asyncio.create_task(telegram_alerts.send_message(
                            telegram_alerts.fmt_mt5_partial_close(
                                trade, closed_volume, float(close_price),
                                new_remaining, partial_profit, reason,
                            ),
                            trade["trade_id"], f"mt5_partial_{reason.lower()}",
                        ))
                        self._mt5_sync_missing_streak.pop(trade["trade_id"], None)
                        continue  # trade still open — do not record as full close

                # ── Full close ────────────────────────────────────────────────────
                self._mt5_sync_missing_streak.pop(trade["trade_id"], None)
                log.info("MT5 sync: closing trade %s ticket=%s @ %.2f reason=%s",
                         trade["trade_id"], ticket, close_price, reason)
                result = await self._record_close(trade["trade_id"], float(close_price), reason)
                await self._sync_profit(trade["trade_id"], ticket)
                asyncio.create_task(self._schedule_profit_sync(trade["trade_id"], ticket))
                def _fetch_closed_row():
                    with db_module.db() as conn:
                        return db_module.row_to_dict(conn.execute(
                            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
                            (trade["trade_id"],),
                        ).fetchone())
                closed_row = await db_module.to_db_thread(_fetch_closed_row)
                account  = await self.get_mt5_account()
                last_tp  = await db_module.to_db_thread(self._last_closed_tp, trade["trade_id"]) if reason == "SL" else None
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_trade_close(closed_row, result, {}, account,
                                                    last_tp=last_tp),
                    trade["trade_id"], f"mt5_sync_{reason}",
                ))
            except Exception as e:
                log.warning("MT5 sync close failed %s: %s", trade["trade_id"], e)

        # ── Import any MT5 positions the app doesn't know about ───────────────
        # Covers trades opened directly in MT5 and positions where a partial
        # close on a hedge account replaced the ticket with a new one.
        def _fetch_known_tickets():
            with db_module.db() as _conn:
                known = {
                    int(r[0])
                    for r in _conn.execute(
                        "SELECT mt5_ticket FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
                    ).fetchall()
                }
                # Adaptive Runner ladder legs 2+ are opened via self._bridge.
                # place_order() directly (see _open_adaptive_runner_ladder) —
                # they never get their own vantage_simulated_trades row, only
                # a vantage_ladder_legs row linked to the parent trade. Without
                # this, every non-anchor leg looked "untracked" here and got
                # re-imported as its own phantom duplicate trade (tg_source=
                # MT5_imported) each cycle, inflating open-trade counts against
                # max_open_trades and showing duplicate rows in the UI.
                known |= {
                    int(r[0])
                    for r in _conn.execute(
                        "SELECT mt5_ticket FROM vantage_ladder_legs WHERE mt5_ticket IS NOT NULL"
                    ).fetchall()
                }
                return known
        try:
            all_known_tickets = await db_module.to_db_thread(_fetch_known_tickets)
        except Exception:
            all_known_tickets = set()

        rs = await db_module.to_db_thread(db_module.get_risk_settings)
        default_strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT) or STRATEGY_SCALE_OUT

        for pos in live_positions:
            ticket = int(pos["ticket"])
            if ticket in all_known_tickets:
                continue
            # New untracked position — import it so the engine can manage it
            trade_id = str(uuid.uuid4())[:16]
            direction = pos.get("type", "BUY").upper()
            lot_size  = float(pos.get("volume", 0.01))
            entry_p   = float(pos.get("open_price", 0))
            sl        = float(pos.get("sl") or 0) or None
            tp        = float(pos.get("tp") or 0) or None
            open_ts   = float(pos.get("open_time") or time.time())
            try:
                def _import_position():
                    with db_module.db() as conn:
                        # vantage_simulated_trades.signal_id is NOT NULL with a
                        # FOREIGN KEY into vantage_signals — passing "" here (as
                        # this always has, for any directly-MT5-opened position)
                        # can never satisfy that constraint since no row with
                        # signal_id="" has ever existed, so this insert has never
                        # actually succeeded; it just silently failed every
                        # monitor cycle, forever, for any such position. Ensure
                        # a sentinel row exists first (idempotent) instead.
                        conn.execute(
                            """INSERT OR IGNORE INTO vantage_signals
                               (signal_id, source_name, direction, entry_low, entry_high,
                                stop_loss, status, created_at)
                               VALUES ('MT5_DIRECT', 'MT5 direct import', ?, 0, 0, 0,
                                       'activated', ?)""",
                            (direction, time.time()),
                        )
                        conn.execute(
                            """INSERT INTO vantage_simulated_trades
                               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,
                                entry_price,lot_size,remaining_lots,stop_loss,tp1,
                                status,open_time,strategy,tg_source)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (
                                trade_id, "MT5_DIRECT", ticket, direction,
                                entry_p, entry_p, entry_p,
                                lot_size, lot_size,
                                sl, tp,
                                "open", open_ts, default_strategy, "MT5_imported",
                            ),
                        )
                await db_module.to_db_thread(_import_position)
                log.info(
                    "MT5 sync: imported untracked position ticket=%s %s %.2f lots @ %.2f",
                    ticket, direction, lot_size, entry_p,
                )
                all_known_tickets.add(ticket)
            except Exception as imp_err:
                log.warning("MT5 sync: failed to import ticket %s: %s", ticket, imp_err)

    async def _sync_profit(self, trade_id: str, mt5_ticket: int) -> Optional[float]:
        return await _sync_profit_impl(trade_id, mt5_ticket, self._bridge)

    async def _schedule_profit_sync(self, trade_id: str, mt5_ticket: int) -> None:
        return await _schedule_profit_sync_impl(trade_id, mt5_ticket, self._bridge)

    async def _profit_sweep(self) -> None:
        return await _profit_sweep_impl(self._bridge)

    async def _close_full_after_tps(self, trade_id: str, mt5_ticket: Optional[int],
                                     close_price: float) -> None:
        if mt5_ticket:
            # Verify the position is actually gone before declaring the trade
            # closed — the app's own lot-close bookkeeping can drift from the
            # broker's real fill volume (rounding to lot_step), which used to
            # let this fire a false "closed" alert while MT5 still held a
            # residual position open.
            try:
                live = await self._bridge.get_positions()
                residual = next(
                    (p for p in live if int(p.get("ticket", -1)) == int(mt5_ticket)
                     and float(p.get("volume", 0)) > 0.0001),
                    None,
                )
            except Exception as e:
                log.debug("[TP-close] residual position check failed: %s", e)
                residual = None
            if residual is not None:
                residual_vol = float(residual["volume"])
                log.warning(
                    "[TP-close] %s marked closed but MT5 ticket=%s still has %.2f lots open — "
                    "reopening trade record and closing the residual.",
                    trade_id[:8], mt5_ticket, residual_vol,
                )
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET status='open',close_time=NULL,"
                        "exit_reason=NULL,remaining_lots=? WHERE trade_id=?",
                        (residual_vol, trade_id),
                    )
                mt5_res = await self._bridge.close_position(int(mt5_ticket))
                if mt5_res.get("success"):
                    await self._record_close(trade_id, float(mt5_res.get("close_price", close_price)),
                                              "all_tps_hit_residual")
                else:
                    asyncio.create_task(telegram_alerts.send_message(
                        f"*TP Close Residual Left Open*\n"
                        f"Trade {trade_id[:8]} ticket {mt5_ticket} still has {residual_vol:.2f} "
                        f"lots open in MT5 after all TPs — the app's DB record has been reopened "
                        f"to reflect this, but the residual close attempt failed "
                        f"({mt5_res.get('error', 'unknown error')}). Please check MT5 manually.",
                        trade_id, "tp_close_residual",
                    ))
                return
            await self._sync_profit(trade_id, int(mt5_ticket))
            asyncio.create_task(self._schedule_profit_sync(trade_id, int(mt5_ticket)))
        with db_module.db() as conn:
            closed_row = db_module.row_to_dict(conn.execute(
                "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
            ).fetchone())
        account = await self.get_mt5_account()
        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_trade_close(
                closed_row,
                {"close_price": close_price, "reason": "all_tps_hit",
                 "net_pnl": closed_row.get("net_pnl", 0)},
                {}, account,
            ),
            trade_id, "trade_close_all_tps",
        ))

    # ── Background commentary ─────────────────────────────────────────────────

    async def _background_open_commentary(self, trade_id: str, sig: dict, tick: Tick) -> None:
        try:
            candles = await self.get_candles("M5", 20)
            with db_module.db() as conn:
                trade_row = db_module.row_to_dict(conn.execute(
                    "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
                ).fetchone())
            commentary = await claude_ai.request_commentary(
                "trade_open", trade_row, sig, tick, candles, self._cfg,
            )
            db_module.save_commentary(commentary, trade_id, sig.get("signal_id"))
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_simulated_trades SET claude_open=? WHERE trade_id=?",
                    (json.dumps(commentary), trade_id),
                )
            await telegram_alerts.send_message(
                telegram_alerts.fmt_trade_open(trade_row, tick, commentary), trade_id, "trade_open",
            )
        except Exception as e:
            log.warning("Background open commentary failed %s: %s", trade_id, e)

    async def _background_close_commentary(self, trade_id: str, result: dict,
                                            reason: str, tick: Tick) -> None:
        """Send the Telegram trade-close notification with the real MT5 P&L."""
        try:
            # Fetch mt5_ticket and try to sync the real broker P&L before notifying.
            # This replaces the previous race-condition approach (hoping _sync_profit
            # would win the race against get_mt5_account).
            def _fetch_ticket():
                with db_module.db() as conn:
                    return db_module.row_to_dict(conn.execute(
                        "SELECT mt5_ticket FROM vantage_simulated_trades WHERE trade_id=?",
                        (trade_id,),
                    ).fetchone())
            quick = await db_module.to_db_thread(_fetch_ticket)
            mt5_ticket = (quick or {}).get("mt5_ticket")
            if mt5_ticket:
                try:
                    await asyncio.wait_for(
                        self._sync_profit(trade_id, int(mt5_ticket)), timeout=8.0
                    )
                except (asyncio.TimeoutError, Exception) as _e:
                    log.debug("Close commentary: profit sync skipped (%s)", _e)

            account = await self.get_mt5_account()
            def _fetch_row():
                with db_module.db() as conn:
                    return db_module.row_to_dict(conn.execute(
                        "SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
                    ).fetchone())
            row = await db_module.to_db_thread(_fetch_row)
            last_tp = await db_module.to_db_thread(self._last_closed_tp, trade_id) if reason == "SL" else None
            await telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(row, result, {}, account, last_tp=last_tp),
                trade_id, reason,
            )
        except Exception as e:
            log.warning("Close notification failed %s: %s", trade_id, e)

    # ── Unrecognised message handling ─────────────────────────────────────────

    @staticmethod
    def _is_active_trader_node() -> bool:
        """Whether THIS node is the one currently allowed to execute real
        trades — mirrors open_trade()'s two-sided mutual-exclusion gate
        (server/VPS role via sync.server.is_standing_down(), client/Mac role
        via get_active_trader() == TRADER_REMOTE_VPS). Fails open (True) on
        any error or unpaired/standalone install, same as those gates.

        Used to decide whether this node should spend paid AI credits on
        Telegram-signal recovery — see _try_ai_signal_fallback. Both sides of
        a paired Mac/VPS run separate Telethon sessions on the same account
        and parse every message independently regardless of which one is
        active (confirmed live 2026-07-09: both nodes independently invoked
        the AI fallback on the same unrecognised messages, doubling AI cost
        and splitting the Reader Logic > AI review queue across two node-
        local DBs with no way to see or clear the other node's entries from
        either UI). Only the paid AI call is gated here — deterministic
        parsing/signal creation keeps running on both nodes exactly as
        before, so a promoted standby still has everything it needs.
        """
        try:
            from forex_trader.sync import server as _sync_srv_mod
            _srv = _sync_srv_mod.get_instance()
            if _srv is not None and _srv.is_standing_down():
                return False
        except ImportError:
            pass
        try:
            from forex_trader.sync.protocol import TRADER_REMOTE_VPS
            from forex_trader.sync.client import SyncClient
            _host, _, _ = SyncClient.load_config()
            if _host and db_module.get_active_trader() == TRADER_REMOTE_VPS:
                return False
        except ImportError:
            pass
        return True

    async def _orb_auto_execute(self, report: dict) -> None:
        """
        Places the ORB/IVB Report's recommended trade unattended, every
        morning, when the auto-execute toggle is on.

        This scheduler runs independently and unconditionally on *both* Mac
        and VPS (same as the email report above it). Normally it deliberately
        does NOT forward to the other node the way the Trading tab's Execute
        Trade button does — that button forwards because it's a single user
        click on one specific node's UI with nowhere else to go, whereas here
        both nodes compute the same report on the same clock, so only the
        active trader should ever act, or the same trade fires twice.

        Under centralized signal generation (Settings > Remote Node), that
        inverts: the VPS has stopped analyzing entirely
        (should_generate_signals_here() is False there), so it must defer
        even though it's the active trader — and the Mac proceeds even
        though it *isn't* the active trader, since open_trade() (called via
        open_manual_market_order() below) forwards the resolved trade to the
        VPS for execution under that mode. Still exactly one node ever acts:
        whichever one currently owns generation.
        """
        return await _orb_auto_execute_impl(report, self._bridge, self._is_active_trader_node())

    async def _try_ai_signal_fallback(self, text: str, channel_name: str, tg_id: str) -> Optional[dict]:
        """Last-resort AI extraction for a message the deterministic
        parser (is_format_ab_signal/parse_gold_signal, is_gd2_message/
        parse_gd2_signal) failed to recognise or fully parse. Only called
        after the deterministic path has already given up — see
        ai_signal_extractor.py for why this is safe (confidence floor, no
        fabricated numbers on bare triggers, no action on chatter).

        Gated to the active-trader node only (see _is_active_trader_node) —
        the standby side of a paired Mac/VPS returns None immediately without
        recording a dedup check, so nothing is lost: if this node is later
        promoted, the message is still sitting unclassified in the reader's
        buffer and gets a normal first attempt then.

        The same AI call also classifies "Adjust SL to X"-style follow-up
        instructions (see ai_signal_extractor.classify_message) — handled
        entirely here (apply + queue for review) since it's not a new entry
        signal for the caller to execute, so this still returns None for
        that case exactly as it would for chatter.

        Deduplicated on (tg_id, text) via db_module.has_ai_fallback_check()/
        record_ai_fallback_check() — the Telegram reader's message buffer
        (get_buffer_messages) holds the last N messages regardless of
        processing status, and this function is reached on every scan cycle
        (~1s) for any message that still fails deterministic parsing. Without
        this, one piece of chatter that's neither a signal nor an
        SL-adjustment got reclassified by a live paid AI call every single
        cycle, forever (confirmed live 2026-07-08 via a temporary
        caller-debug patch in ai_provider.complete() — this was the entire
        explanation for API credits draining with no corresponding Telegram
        activity). The check only gets recorded *after* a successful AI call
        (any definitive result, including "not a signal") — a transient
        failure (network blip, timeout) is deliberately NOT recorded, so it's
        retried next cycle instead of giving up on a message permanently.
        Keyed on text too, not just tg_id, so a genuine edit (new text under
        the same tg_id) still gets classified fresh.

        A cheap pre-check reuses the existing "Currency:" regex so the AI
        never even gets asked about an explicitly-non-XAUUSD message (the
        AI's own prompt is gold-only and untested against other pairs)."""
        return await _try_ai_signal_fallback_impl(
            text, channel_name, tg_id, self._cfg,
            self._is_active_trader_node(), self._bridge,
        )

    @staticmethod
    async def _push_ai_recovered_created(
        tg_id: str, channel_name: str, text: str, message_type: str,
        confidence: float, reasoning: str, **fields,
    ) -> None:
        """Mirror a newly-created ai_recovered_signals row to the paired
        Local/Remote node — see sync/protocol.py's MSG_AI_RECOVERED_SIGNAL_SYNC
        comment. Best-effort: a missed delivery is caught by the periodic
        full-queue pull (sync.client.SyncClient._ai_recovered_pull_loop)."""
        return await _push_ai_recovered_created_impl(
            tg_id, channel_name, text, message_type, confidence, reasoning, **fields,
        )

    async def _apply_sl_adjustment(
        self, new_sl: float, channel_name: str, tg_id: str, via: str,
    ) -> None:
        """Apply a recognised "Adjust SL to X" instruction to whichever open
        trade this channel most recently produced. via is "learned_rule"
        (fast, deterministic match — see signal_parser.check_sl_adjustment_rules)
        or "ai_fallback" (first time this channel's wording is seen; also
        queued for review in Telegram > Reader Logic > AI so a rule gets
        generated for next time).

        Safe no-op if this node has no matching open trade locally — e.g. it's
        the stood-down side of a Local/Remote pair and never executed the
        position in the first place (see open_trade()'s active_trader gate).
        Modifying an *already open* trade's SL, unlike opening a new one, is
        safe to attempt redundantly from either node since MT5 itself is the
        single source of truth for the real position — both nodes reaching
        the same target SL from the same message is harmless, not a race.

        Deduplicated on tg_id via db_module.try_claim_sl_adjustment() — the
        Telegram reader's message buffer (get_buffer_messages) is re-scanned
        every ~1s regardless of whether a message was already handled, and
        without this a matched learned rule kept re-firing on the same
        message every cycle for as long as it stayed buffered, each time
        sending a fresh "SL adjusted" alert (found live 2026-07-08 — same
        message re-triggered roughly once a minute for over half an hour)."""
        return await _apply_sl_adjustment_impl(new_sl, channel_name, tg_id, via, self._bridge)

    def _queue_unrecognised(self, tg_id: str, channel_name: str, text: str) -> None:
        return _queue_unrecognised_impl(tg_id, channel_name, text, self._cfg)

    async def _analyse_unrecognised_message(
        self, unrec_id: int, channel_name: str, text: str
    ) -> None:
        return await _analyse_unrecognised_message_impl(unrec_id, channel_name, text, self._cfg)

    # ── Signal scanner ────────────────────────────────────────────────────────

    async def _signal_scanner_loop(self) -> None:
        await asyncio.sleep(15)
        while self._monitor_running:
            try:
                await self._scan_messages()
            except Exception as e:
                log.warning("Signal scanner error: %s", e)
            # Wake immediately when a new message is buffered (set by _event_processor).
            # Falls back to a 1-second poll if the reader is unavailable or not connected.
            if self._tg_reader is not None:
                try:
                    await self._tg_reader.wait_for_new_message(timeout=1.0)
                except Exception:
                    await asyncio.sleep(1)
            else:
                await asyncio.sleep(1)

    async def _process_instant_entry(
        self,
        msg: dict,
        tg_id: str,
        group_id: str,
        channel_name: str,
        text: str,
        direction: str,
        price: Optional[float],
        rs: dict,
        auto_execute: bool,
    ) -> None:
        """Open an immediate market order for a bare 'XAU Buy/Sell Now' message."""
        return await _process_instant_entry_impl(
            msg, tg_id, group_id, channel_name, text, direction, price, rs, auto_execute,
            self._bridge, self._dpm_candles, self._cfg.get("starting_balance", 1000.0),
        )

    async def _apply_followup_to_instant_trade(
        self,
        instant_trade: dict,
        parsed: dict,
        tg_id: str,
        channel_name: str,
        source_label: str,
    ) -> None:
        """Apply SL/TP from a full follow-up signal to an open instant market trade."""
        return await _apply_followup_to_instant_trade_impl(
            instant_trade, parsed, tg_id, channel_name, source_label, self._bridge,
        )

    async def _find_and_apply_instant_followup(
        self, channel_name: str, direction: str, parsed: dict, tg_id: str,
    ) -> bool:
        """Locate the open instant-entry trade this follow-up signal belongs
        to and apply it, on whichever node actually holds that trade's row.

        Under centralized signal generation with the VPS as active trader,
        the IME trade this follow-up is meant for was opened via open_trade()'s
        forwarding branch and only exists in the VPS's own vantage_simulated_trades
        — never this (generating) node's. Querying the local table here always
        found nothing, so every follow-up fell through to opening a second,
        independent trade: the VPS ended up placing two real MT5 orders for one
        signal. Mirrors open_trade()'s own forwarding condition exactly so the
        two stay in sync — if a signal was forwarded to open, its follow-up
        must be forwarded to update the same node's copy.

        Returns True if a matching open instant trade was found (locally or on
        the VPS) and the follow-up was applied to it — the caller should skip
        opening a new trade. False means no match — fall through as normal.
        """
        return await _find_and_apply_instant_followup_impl(
            channel_name, direction, parsed, tg_id, self._bridge,
        )

    async def _ime_timeout_watchdog(self, tick: "Tick") -> None:
        """
        Auto-assign SL/TP levels to IME trades that have been open for more than
        3 minutes without receiving a follow-up signal.

        When the follow-up never arrives the trade sits with no TP levels and only
        a provisional emergency SL, making conservative strategy inoperative (no
        partial closes, no SL-to-breakeven).  This watchdog:

        1. Finds open trades with tp1=NULL and open_time > 3 minutes ago.
        2. Assigns six standard XAUUSD TP levels (3/5/7/10/14/18 pts from entry).
        3. If the current price has already cleared auto-TP1, moves the SL to
           entry (breakeven) so the trade is at least protected.
        4. Calls modify_order on MT5 so broker-side SL is updated.
        5. Sends a Telegram alert so the operator knows auto-TPs were assigned.
        """
        return await _ime_timeout_watchdog_impl(tick, self._bridge)

    async def _scan_messages(self) -> list[dict]:
        # Centralized signal generation (Settings > Remote Node): once this
        # VPS is the active trader and generation has moved to the Mac, skip
        # parsing/creating GD2/the reference channel/Format-AB signals entirely here rather
        # than just letting a later open_trade() gate discard the work —
        # this is what actually saves the CPU, not just the execution.
        if not await db_module.to_db_thread(db_module.should_generate_signals_here):
            return []
        if self._tg_reader is None:
            return []
        msgs = self._tg_reader.get_buffer_messages(limit=100)
        if not msgs:
            return []

        slot_groups = self._tg_reader.get_active_group_slots()
        rs           = db_module.get_risk_settings()

        if not bool(rs.get("accept_tg_signals", 1)):
            # Dropping signals must never be silent — an accidental toggle-off
            # cost a valid the reference channel signal on 2026-07-03 with zero log evidence.
            # Warn (throttled to once per 5 min) whenever messages are being
            # discarded while the switch is off.
            now_ts = time.time()
            if now_ts - getattr(self, "_tg_off_warned_at", 0.0) > 300:
                self._tg_off_warned_at = now_ts
                log.warning(
                    "[engine] accept_tg_signals is OFF — %d buffered Telegram "
                    "message(s) are being IGNORED (Trading tab → Signals → "
                    "'Telegram Signals' toggle)", len(msgs),
                )
            return []

        auto_execute = bool(rs.get("auto_execute_signals", 0))
        new_signals: list[dict] = []

        exclude_high_risk = bool(rs.get("exclude_high_risk", 0))

        for msg in msgs:
            tg_id    = str(msg.get("id") or "")
            group_id = str(msg.get("group_id") or "")
            text     = (msg.get("text") or "").strip()
            if not tg_id or not text:
                continue
            from backend.src.utils import latency_trace as _lt_scan
            _lt_scan.mark(tg_id, "t6_scanning")
            if slot_groups and group_id and group_id not in slot_groups:
                continue

            if exclude_high_risk and "high risk" in text.lower():
                log.info("[engine] Skipping high-risk signal tg_id=%s", tg_id)
                continue

            slot         = slot_groups.get(group_id, 1)
            channel_name = self._tg_reader.get_group_name(group_id) or f"Channel {slot}"

            # Resolve channel parser config — auto-bootstrap on first sight
            ch_cfg = db_module.get_channel_parser_config(channel_name)
            if ch_cfg is None:
                _default_fmt    = 'format_ab' if slot == 1 else 'gd2'
                _default_prefix = SIGNAL_PREFIX if _default_fmt == 'format_ab' else ''
                _default_ime    = 1  # enabled for both format_ab and gd2
                db_module.save_channel_parser_config(
                    channel_name, _default_fmt, _default_prefix,
                    bool(_default_ime), True,
                    f'Auto-configured from slot {slot}',
                )
                ch_cfg = db_module.get_channel_parser_config(channel_name) or {}
            parser_fmt  = ch_cfg.get('parser_format', 'auto')
            sig_prefix  = ch_cfg.get('signal_prefix') or SIGNAL_PREFIX
            # Default ime_enabled to True for both known formats; only False when
            # the user has explicitly set it to 0 in the channel config.
            _ime_default = 1 if parser_fmt in ('format_ab', 'gd2') else 0
            ime_enabled  = bool(ch_cfg.get('instant_entry_enabled', _ime_default))

            if parser_fmt == 'none' or not bool(ch_cfg.get('enabled', 1)):
                continue

            # ── Logic Keywords (Parsing page) ────────────────────────────────
            # Global, user-editable phrase lexicons -- checked ahead of
            # everything else below since Ignore Media/Forwarded and the
            # CLOSE ALL/RISK FREE-BE/TP HIT triggers are orthogonal to normal
            # signal parsing (a "CLOSE ALL" message, say, would never parse as
            # a signal anyway, but skipping it here avoids it falling through
            # to the unrecognised-message queue). See core_logic_keyword_triggers.py.
            _lk_skip = should_skip_media_or_forwarded(msg, rs)
            if _lk_skip:
                log.debug("[LogicKeywords] tg_id=%s skipped — %s", tg_id, _lk_skip)
                continue
            if await try_handle_close_all_trigger(
                text, channel_name, tg_id, rs, close_trade_fn=self.close_trade,
            ):
                continue
            if await try_handle_risk_free_be_trigger(
                text, channel_name, tg_id, rs, bridge=self._bridge,
            ):
                continue
            if await try_handle_tp_hit_trigger(text, channel_name, tg_id, rs):
                continue

            # Dedup — skip already-processed messages, but re-parse edited ones
            # if the signal hasn't been executed yet (direction correction).
            with db_module.db() as conn:
                _existing = conn.execute(
                    "SELECT id, direction, status, raw_text, entry_low FROM vantage_tg_signals "
                    "WHERE tg_message_id=?", (tg_id,)
                ).fetchone()
            if _existing:
                _ex = db_module.row_to_dict(_existing)
                _edit_result = await _handle_signal_edit_impl(
                    tg_id, group_id, channel_name, text, parser_fmt, _ex,
                    ai_fallback_fn=self._try_ai_signal_fallback,
                    find_and_apply_instant_followup_fn=self._find_and_apply_instant_followup,
                    close_trade_fn=self.close_trade,
                )
                if _edit_result is None:
                    continue
                # A pending_followup signal just received its SL/TP via this
                # edit — fall through to the normal Instant-Market-Entry /
                # SL-adjustment / classify-and-parse pipeline below exactly
                # like a brand-new message would (the edited raw_text now
                # parses as a full signal on its own); _edit_result itself is
                # deliberately unused past this point, matching the original
                # inline code's behavior of re-deriving `parsed` from scratch
                # rather than reusing the edit-time reparse.

            # ── Instant Market Entry ────────────────────────────────────────
            # format_ab: fires on "XAUUSD Buy Now" / "XAU Sell Now" (requires NOW).
            # gd2: fires on "XAU USD BUY [NOW]" / "XAU USD SELL [NOW]" with or
            #      without "NOW", or the newer "Buy/Sell Zone Now" bare trigger
            #      (see is_gd2_message — a plain "XAU" substring check missed
            #      this format entirely, since none of its messages mention
            #      XAU anywhere, silently disabling IME for GD2 on 2026-07-06
            #      even with the setting on and correctly synced).  GD2
            #      messages that already carry SL/TP are excluded (they go
            #      through parse_gd2_signal as full signals).
            _ime_gate = is_gd2_message(text) if parser_fmt == 'gd2' else "XAU" in text.upper()
            if bool(rs.get("immediate_market_entry", 0)) and _ime_gate and ime_enabled:
                if parser_fmt == 'gd2':
                    _instant = parse_gd2_instant_entry(text)
                else:
                    _instant = parse_instant_entry(text)
                if _instant:
                    _instant_dir, _instant_px = _instant
                    await self._process_instant_entry(
                        msg, tg_id, group_id, channel_name, text,
                        _instant_dir, _instant_px, rs, auto_execute,
                    )
                    continue

            # ── SL-adjustment fast path (learned rule only — no AI call) ────
            # Checked before any entry-signal parsing since it's an orthogonal
            # message category (a follow-up to an EXISTING trade, e.g. "Adjust
            # SL to 4060", not a new entry) — see signal_parser.
            # check_sl_adjustment_rules and _apply_sl_adjustment above. A
            # message with no matching rule yet still reaches the entry-signal
            # gates below and, if those also fail, the AI fallback classifies
            # it there instead (first-time cost; a rule gets built on approval
            # so it never needs another AI call for this channel's wording).
            _sl_adj = check_sl_adjustment_rules(text, channel_name)
            if _sl_adj is not None:
                await self._apply_sl_adjustment(_sl_adj, channel_name, tg_id, via="learned_rule")
                continue

            # ── Logic Keywords: exclusion pre-check ──────────────────────────
            # Gates only the new-signal-parsing path below (unlike the
            # triggers above, which run regardless) -- see
            # should_skip_for_exclusion's own docstring for why this doesn't
            # also gate on symbol_tokens.
            _lk_parse_skip = should_skip_for_exclusion(text, rs)
            if _lk_parse_skip:
                log.debug("[LogicKeywords] tg_id=%s skipped — %s", tg_id, _lk_parse_skip)
                continue

            # ── Channel-name-based signal parsing ────────────────────────────
            source_label = channel_name
            parsed = await _classify_and_parse_impl(
                tg_id, group_id, channel_name, text, msg, parser_fmt, sig_prefix,
                ai_fallback_fn=self._try_ai_signal_fallback,
                queue_unrecognised_fn=self._queue_unrecognised,
                rs=rs,
            )
            if parsed is None:
                continue

            # ── Logic Keywords: Enable TP/SL Parsing toggles ─────────────────
            # OFF means "don't use this signal's own stated TP/SL levels" --
            # strips the field(s) right after parsing so every downstream
            # consumer (validation, strategy resolution, execution) sees
            # exactly the same "missing field" shape it already handles for
            # a signal that never had one, rather than a new code path.
            if not bool(rs.get("lk_enable_tp_parsing", 1)):
                for _tp_i in range(1, 9):
                    parsed[f"tp{_tp_i}"] = None
            if not bool(rs.get("lk_enable_sl_parsing", 1)):
                parsed["stop_loss"] = None

            # Staleness guard — signals are scalps: an entry zone is only valid for
            # minutes. Anything older than 4 minutes at processing time is recorded
            # as historical and never executed. The previous 2-hour window let a
            # 22-minute-old the reference channel signal execute at market after a downtime
            # (2026-07-03: toggle-off gap → backfilled signal filled 22min late at
            # a worse price → straight to SL). 4 min covers Telegram delivery
            # latency plus one scan cycle, nothing more.
            _is_fresh = await _record_staleness_or_new_impl(
                tg_id, group_id, channel_name, msg, parsed, source_label,
            )
            if not _is_fresh:
                continue

            from backend.src.utils import latency_trace as _lt_dec
            _lt_dec.mark(tg_id, "t7_decided")

            if parsed.get("_ai_extracted"):
                # The deterministic parser missed this message (format drift) —
                # log it for the review tab; the Telegram notification is
                # merged into the fmt_signal() call below (single message
                # instead of two separate ones hitting the user's phone).
                log.info(
                    "[AI-Fallback] tg_id=%s executing an AI-recovered signal "
                    "(confidence=%.2f)", tg_id, parsed.get("_ai_confidence", 0),
                )

            executed             = False
            exec_lot             = None
            exec_price           = None
            trade_result         = None
            _gap_note            = ""  # only set below if gap-adjusted execution fires

            # Channel override > auto-Claude rec > global Active Strategy,
            # plus the per-signal "High Risk" override, session gate, and
            # trading-paused check.
            _strat_result = await _resolve_strategy_and_skip_reason_impl(
                rs, channel_name, text, parsed, auto_execute,
                getattr(self, "_cfg", {}), self,
                is_trading_paused_fn=lambda: db_module.to_db_thread(self.is_trading_paused),
            )
            strategy             = _strat_result["strategy"]
            strategy_name        = _strat_result["strategy_name"]
            skip_reason          = _strat_result["skip_reason"]
            _sess_ok             = _strat_result["sess_ok"]
            _per_signal_skip     = _strat_result["per_signal_skip"]
            _per_signal_skip_rsn = _strat_result["per_signal_skip_reason"]

            if auto_execute:
                # Limit Runner: a genuine EA pending order, not a market-fill
                # signal — parse_limit_order_signal's `tp_open` marker (always
                # present, True or False, only on its own return dicts) means
                # this signal would default to Limit Runner regardless of
                # whatever channel-override/active-strategy _resolve_strategy_
                # and_skip_reason_impl above just computed, since the strategy
                # here is format-triggered, not channel-configured — EXCEPT
                # when the channel has an EA Template assigned (Trading >
                # Strategy > Channel Strategy > "Template: <name>"): a
                # template fully replaces strategy dispatch by design (see
                # core_ea_templates.py's module docstring), so it must win
                # over the format-triggered default too, not just over the
                # built-in strategies. Fixed 2026-07-24 — a template-assigned
                # channel's "[LIMITS]"-formatted signals were silently always
                # executing as Limit Runner instead, so a configured Grid
                # template never actually ran for them.
                if parsed.get("tp_open") is not None and not _ea_templates.is_template_override(strategy):
                    strategy      = STRATEGY_LIMIT_RUNNER
                    strategy_name = STRATEGY_NAMES[STRATEGY_LIMIT_RUNNER]
                    _exec_result = await _handle_limit_order_signal_impl(
                        parsed, tg_id, channel_name, source_label, rs,
                        _sess_ok, _per_signal_skip, _per_signal_skip_rsn, skip_reason,
                        get_trading_balance_fn=self._get_trading_balance,
                        suggest_lot_size_fn=self.suggest_lot_size,
                        bridge=self._bridge,
                    )
                    executed     = False
                    exec_lot     = None
                    exec_price   = None
                    trade_result = None
                    skip_reason  = _exec_result["skip_reason"]
                else:
                    _exec_result = await _execute_auto_signal_impl(
                        parsed, tg_id, channel_name, source_label, strategy, rs,
                        _sess_ok, _per_signal_skip, _per_signal_skip_rsn, skip_reason,
                        self._bridge,
                        get_open_trades_fn=self.get_open_trades,
                        find_and_apply_instant_followup_fn=self._find_and_apply_instant_followup,
                        check_pre_trade_filters_fn=self._check_pre_trade_filters,
                        suggest_lot_size_fn=self.suggest_lot_size,
                        get_trading_balance_fn=self._get_trading_balance,
                        open_trade_fn=self.open_trade,
                    )
                    executed     = _exec_result["executed"]
                    exec_lot     = _exec_result["exec_lot"]
                    exec_price   = _exec_result["exec_price"]
                    trade_result = _exec_result["trade_result"]
                    skip_reason  = _exec_result["skip_reason"]
                    _gap_note    = _exec_result["gap_note"]
                    if _exec_result.get("followup_matched") or _exec_result.get("deferred_stood_down"):
                        new_signals.append(parsed | {"tg_message_id": tg_id, "auto_executed": executed,
                                                     "source_label": source_label})
                        continue

            # Forwarded trade (centralized signal generation): the VPS actually
            # placed it — trade_id/mt5_ticket belong to its DB, not this node's
            # — and its own _handle_signal_order already schedules the correct
            # "Node: Remote" notification via _background_open_commentary. This
            # node sending its own fmt_signal alert here as well would always
            # read "Node: Local" (_node_label() reflects this machine, not
            # which one executed the trade) — a duplicate, misleading alert
            # claiming local execution for a trade the VPS handled.
            if not (trade_result and trade_result.get("executed_remotely")):
                _alert_strategy = (
                    f"{strategy_name} | {_gap_note}" if _gap_note else strategy_name
                )
                alert = telegram_alerts.fmt_signal(
                    parsed, channel_name, executed,
                    exec_lot, exec_price, skip_reason, _alert_strategy,
                    ai_confidence=parsed.get("_ai_confidence") if parsed.get("_ai_extracted") else None,
                    ai_reasoning=parsed.get("_ai_reasoning", "") if parsed.get("_ai_extracted") else "",
                )
                asyncio.create_task(telegram_alerts.send_message(alert, event_type="tg_signal_detected"))
            new_signals.append(parsed | {"tg_message_id": tg_id, "auto_executed": executed,
                                         "source_label": source_label})

        return new_signals

    async def import_mt5_history(self, days: int = 90) -> dict:
        """Pull closed deals from MT5 bridge, reconstruct positions, insert any missing into DB."""
        return await _import_mt5_history_impl(self._bridge, days)

    def get_tg_signals(self, limit: int = 50) -> list[dict]:
        return _get_tg_signals_impl(limit, self._tg_reader)

    async def update_signal(self, signal_id: str, updates: dict) -> dict:
        """Update signal fields and propagate changes to any linked open trade."""
        return await _update_signal_impl(self._bridge, signal_id, updates)

    async def compute_mt5_performance(self, days: int = 90) -> dict:
        """Compute performance stats directly from MT5 bridge deal history."""
        return await _compute_mt5_performance_impl(self._bridge, days)

    async def get_total_deposits(self) -> float:
        return await _get_total_deposits_impl(self._bridge)

    # ── Max TP checker ────────────────────────────────────────────────────────

    async def _max_tp_checker_loop(self) -> None:
        """Every 5 minutes, find trades closed 30+ minutes ago with no max_tp_hit set.
        Fetches M1 candles for the trade's own open_time -> close_time window and
        records the highest TP level price reached during that window (ignoring
        the SL). The 30-minute wait is only a data-settling delay before this
        loop processes a trade — it is not part of the measurement window.
        (Prior to 2026-07-18 the fetch window extended 30 min past close_time,
        which meant a trade's Max TP Hit could reflect price action from AFTER
        it had already closed — see ticket 1615526315: tagged TP6 from a
        continuation move that ran up 20+ minutes after the position was flat.
        See _backfill_max_tp_hit_corrected for the one-off correction of
        already-computed values.)"""
        await asyncio.sleep(90)  # startup delay
        while self._monitor_running:
            try:
                await _max_tp_checker_sweep_impl(self._bridge)
            except Exception as e:
                log.debug("_max_tp_checker_loop error: %s", e)
            await asyncio.sleep(300)  # run every 5 minutes

    async def _backfill_max_tp_hit_corrected(self) -> None:
        """One-off (2026-07-18): recompute max_tp_hit for every already-closed
        trade using the corrected open_time->close_time window, replacing
        values computed under the old close_time+30min window that could
        attribute post-close continuation moves to a trade that was already
        flat (see _max_tp_checker_loop docstring). Safe to leave running at
        startup — recompute-and-compare is idempotent, and it no-ops once
        every row already matches its corrected value."""
        await _backfill_max_tp_hit_corrected_impl(self._bridge)

    # ── TP safety net ────────────────────────────────────────────────────────
    # 2026-07-03: several open trades ran deep into a favourable TP ladder
    # (confirmed via real M1 candle history — the same reliable method as
    # _max_tp_checker_loop above) with the live tick-poll loop never once
    # registering a TP1 hit, then gave the entire move back to a full loss
    # against a stop-loss that was never moved. No exception was ever thrown;
    # a point-in-time tick check has no memory of a price excursion that
    # happens between two polls, so a large-enough gap (an event-loop stall,
    # a fast spike, a missed cycle) can make a real, sustained favourable move
    # completely invisible to it. This loop is the backstop: it doesn't try
    # to replicate the full TP-ladder trailing logic (higher risk to get
    # right blind on a live system) — it only ever does the one minimum-risk
    # protective thing, moving SL to breakeven+cost once ANY TP1+ level is
    # confirmed reached and the live loop hasn't already protected it.
    # Was 180s — for a scalp strategy securing only 3-10pts, that (compounded
    # with the "too young" floor below) meant a trade's first-ever check
    # could land up to ~5 minutes after open, long after a fast round trip
    # had already fully reversed (confirmed live 2026-07-07, ticket
    # 1552937830 — see [[project_ea_stale_tp_bug]]-adjacent memory). Cheap to
    # run this often now that the candle fetch is position-based, not the
    # broken date-range call it used to be.
    _TP_SAFETY_NET_INTERVAL = 20  # seconds between sweeps

    async def _tp_safety_net_loop(self) -> None:
        await asyncio.sleep(150)  # let the app settle before the first sweep
        while self._monitor_running:
            try:
                await self._tp_safety_net_sweep()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("_tp_safety_net_loop error: %s", e)
            await asyncio.sleep(self._TP_SAFETY_NET_INTERVAL)

    async def _channel_ai_auto_eval_loop(self) -> None:
        """
        Periodic Channel Strategy AI evaluation — one singleton instance per engine.

        This used to be a ui.timer(1800, ...) inside the Trading > Strategy page's
        render function. NiceGUI scopes a page-level ui.timer per browser client,
        so every reconnect (tab refresh, dropped websocket, reopening the
        dashboard) spawned another timer that was never reliably cancelled —
        confirmed via evaluate_channels() log timestamps clustering far tighter
        than the intended 30 minutes, sometimes under a minute apart. That
        compounding was burning Anthropic API credits. Running it here instead
        guarantees exactly one call per interval regardless of how many browser
        tabs are open or reconnect.
        """
        await asyncio.sleep(120)  # let the app settle before the first evaluation
        while self._monitor_running:
            try:
                if ai_provider.is_configured(self._cfg):
                    from backend.src.services.channels import strategy_ai as _csai
                    await _csai.evaluate_channels(self, self._cfg)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("_channel_ai_auto_eval_loop error: %s", e)
            await asyncio.sleep(1800)

    async def _ai_model_refresh_loop(self) -> None:
        """
        Daily refresh of the Claude and DeepSeek model catalogues shown in
        Settings > AI's dropdowns. Both providers' available models change
        over time, so the dropdown is backed by a cached list (refreshed here
        and via the on-demand "Refresh Models" button) rather than a
        hardcoded array that only updates when someone edits the source.
        """
        await asyncio.sleep(60)  # let the app settle before the first refresh
        _INTERVAL = 86400
        while self._monitor_running:
            try:
                updates: dict = {}
                if self._cfg.get("anthropic_api_key"):
                    models = await ai_provider.fetch_available_models(
                        "claude", self._cfg["anthropic_api_key"]
                    )
                    updates["claude_models_cache"] = models
                    self._cfg["claude_models_cache"] = models
                if self._cfg.get("deepseek_api_key"):
                    models = await ai_provider.fetch_available_models(
                        "deepseek", self._cfg["deepseek_api_key"]
                    )
                    updates["deepseek_models_cache"] = models
                    self._cfg["deepseek_models_cache"] = models
                if updates:
                    updates["ai_models_last_refreshed"] = time.time()
                    self._cfg["ai_models_last_refreshed"] = updates["ai_models_last_refreshed"]
                    import backend.src.config as _cfg_mod
                    await db_module.to_db_thread(lambda: _cfg_mod.save_to_yaml(updates))
                    log.info("[AI] model catalogue refreshed: %s", list(updates.keys()))
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("_ai_model_refresh_loop error: %s", e)
            await asyncio.sleep(_INTERVAL)

    async def _tp_safety_net_sweep(self) -> None:
        return await _tp_safety_net_sweep_impl(self._bridge, self._tp_safety_net_last_alert)

    async def _tp_safety_net_check_trade(self, trade: dict, now: float) -> None:
        return await _tp_safety_net_check_trade_impl(
            trade, now, self._bridge, self._tp_safety_net_last_alert,
        )

    def _compute_be_cost_pts(self, trade: dict) -> float:
        """Round-trip cost estimate (spread+commission+slippage) in points,
        for a breakeven-plus-cost stop rather than a raw-entry stop that
        quietly bleeds the spread on every safety-net-protected trade."""
        return _compute_be_cost_pts_impl(trade)

    # ── Signal bus maintenance ────────────────────────────────────────────────

    async def _signal_bus_prune_loop(self) -> None:
        """Delete expired signal_bus rows every 30 minutes.

        prune_signal_bus() existed but was never called from anywhere, so the
        table grew unbounded. Harmless functionally (expired/closed rows are
        already excluded by has_conflict_on_bus's own filters) but worth
        cleaning up now that ttl_seconds runs to 6h per entry instead of 5min.
        """
        await asyncio.sleep(120)  # startup delay
        while self._monitor_running:
            try:
                db_module.prune_signal_bus()
            except Exception as e:
                log.debug("_signal_bus_prune_loop error: %s", e)
            await asyncio.sleep(1800)  # every 30 minutes

    async def _data_retention_loop(self) -> None:
        """Once a day, delete historical data older than the configured
        retention window (Settings > Diagnostics > Data Retention).
        No-op while retention is set to indefinite (the default) — see
        db_module.prune_historical_data for exactly what it does and does
        not touch."""
        await asyncio.sleep(300)  # startup delay
        while self._monitor_running:
            try:
                result = await db_module.to_db_thread(db_module.prune_historical_data)
                if result.get("pruned"):
                    log.info("[DataRetention] pruned rows older than %sd: %s",
                              result.get("retention_days"), result.get("deleted"))
            except Exception as e:
                log.debug("_data_retention_loop error: %s", e)
            await asyncio.sleep(86400)  # once a day

    async def _reversal_engine_research_loop(self) -> None:
        """Once a day at 22:00 Europe/London, read the day's Gold Diggers
        REF + GD2 Telegram messages (text + chart images) and have Claude
        synthesise the real trader's risk-management/entry-logic behaviour
        into two scores that feed Reversal Engine's ML model (ml_engine.py's
        ref_discipline_score / ref_aggression_score features), force an
        immediate retrain, and email a summary. See
        reversal_engine/telegram_research.py for the full pipeline. Checked
        every minute like the ORB report job above — zoneinfo handles the
        BST/GMT switch automatically. Dedup'd by date via app_config so a
        restart near 22:00 can't fire it twice the same day.

        Gated to the physical local node only (is_remote_node(), same gate
        Reversal Engine's own signal generator uses) — NOT _is_active_trader_node().
        The ML model this enriches/retrains (re_ml_batch.pkl/re_ml_online.pkl)
        is a per-node file, never auto-synced between Mac and VPS, and GD
        Copy's signal generation is now local-node-only regardless of which
        side executes trades — so this must follow generation, not execution,
        or it retrains a model nothing is using. Both nodes still only ever
        run this once (is_remote_node() is unconditional, unlike the old
        active-trader check which could migrate), so no duplicate email risk.
        """
        await asyncio.sleep(90)  # let the app settle before the first check
        while self._monitor_running:
            try:
                await _reversal_engine_research_sweep_impl(self)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.warning("_reversal_engine_research_loop error: %s", e)
            await asyncio.sleep(60)

    # ── Morning ORB / IVB report ──────────────────────────────────────────────
    # Adapted from Faber Vaale's "The Simplest Orderflow Trading Model"
    # (youtube.com/watch?v=cUTsoU-15Tc) — the IVB (Initial Value Balance)
    # method: define an opening range, overlay a volume profile on it to find
    # the POC and value area, use the value-area-edge-to-POC band as the
    # "reload zone" entry after a breakout+retracement (tighter, better R:R
    # than fading the whole range), and a statistically-derived target
    # ("protection level"). The video uses the NY session's first 15/30 min;
    # this account trades gold around the London open instead, so the same
    # mechanics are applied to the first hour of the London session (Europe/
    # London local time, so it's DST-correct year-round) rather than NY —
    # widened from an initial 15-minute window after backtesting showed the
    # first 15 minutes is dominated by fakeouts (see the scheduler comment
    # below for the numbers). This matches Market Profile's classic "Initial
    # Balance" definition. The video's "protection level" comes from DeepCharts' own
    # proprietary ML model trained on data this app doesn't have access to —
    # rather than fabricate a number, _get_orb_target_multiple() computes a
    # genuine analog from this account's own MT5 history (see below).
    # Order-flow confirmation (the video's "Model 2" — reading absorption/
    # exhaustion in the footprint at the reload zone) is deliberately not
    # implemented here; it needs genuine tick-level bid/ask data, which is a
    # separate, larger undertaking than this report.

    async def build_orb_report(self) -> Optional[dict]:
        """
        Reference range is the Asian session (00:00-08:00 UTC, the same
        calendar day) rather than a freshly-forming first-hour-of-London
        range. Standard London-breakout convention trades the breakout of
        the ALREADY-ESTABLISHED Asian range the moment London opens — not a
        brand-new range built from London's own first hour, which this
        report used until 2026-07-17.

        Why: the Asian range averages ~4x wider than the old London-hour
        range (~48pt vs ~13pt over a 14-day sample, 2026-07-15). Building a
        volume-profile stop from an already-13pt window left nowhere for
        VAL/VAH to spread — confirmed live: all 4 real orb_fixed trades to
        date hit SL, 3 of them with a stop under 1pt on gold (smaller than
        typical spread). Switching the reference range to the wider Asian
        session, and deriving the stop from a fixed fraction of ITS height
        (_ORB_SL_RANGE_PCT) instead of an inner volume-profile boundary,
        fixes both: the range itself is more stable, and the stop no longer
        depends on how tightly volume happens to cluster within it.

        Also means the report is available the instant London opens rather
        than only after waiting out a full extra hour for a new range to
        form — the Asian range is already complete by then.
        """
        return await _build_orb_report_impl(self._bridge)

    # ── Empirical target-multiple backtest ────────────────────────────────────

    async def _get_orb_target_multiple(self) -> dict:
        """Cached wrapper — the backtest only needs to run once per day."""
        return await _get_orb_target_multiple_impl(self._bridge)

    async def _backtest_orb_target_multiple(self) -> dict:
        """
        Genuine, disclosed analog to the video's proprietary "protection
        level" — measures, over this account's own recent gold history, how
        far price actually travelled past the Asian session range on days
        it cleanly broke one side only after London open, expressed as a
        multiple of that day's range height. Falls back to the standard
        ORB-literature 2x default if there isn't yet enough clean-breakout
        history.

        Reference range matches build_orb_report's own basis (see that
        method's docstring) — must stay in sync, since a mismatch here would
        calibrate the multiplier against a different-sized unit than what
        the live report actually multiplies it by.
        """
        return await _backtest_orb_target_multiple_impl(self._bridge)

    # ── Email scheduler ───────────────────────────────────────────────────────

    async def _email_scheduler_loop(self) -> None:
        await asyncio.sleep(60)  # initial startup delay
        while self._monitor_running:
            try:
                await _email_scheduler_sweep_impl(
                    self._bridge, self._cfg, self._is_active_trader_node(),
                )
            except Exception as e:
                log.debug("Email scheduler error: %s", e)
            # Sleep until next minute boundary
            await asyncio.sleep(60)

    # ── Telegram bot command loop ─────────────────────────────────────────────

    @staticmethod
    def _is_bot_command_authority() -> bool:
        """Whether THIS node should own Telegram getUpdates polling right now.

        Only one process may long-poll a given bot token at a time — a second
        concurrent poller gets 409 Conflict from Telegram. When this Mac/VPS
        pair is connected, both sides' engines run this loop unconditionally,
        so without this gate they fight over the same token forever (each
        side's 409-triggered deleteWebhook kicks the other, which then kicks
        back — the recurring conflict cycle seen in the logs). The standing-
        down side is already a view-only dashboard (see Settings > Remote
        Node), so it has nothing to execute commands against anyway — this
        just extends that same mutual-exclusion to Telegram control.
        An unpaired, standalone install has no counterpart to conflict with,
        so it always polls.
        """
        try:
            from forex_trader.sync.protocol import TRADER_LOCAL, TRADER_REMOTE_VPS
            if db_module.get_app_config("sync_server_enabled") == "1":
                return db_module.get_active_trader() == TRADER_REMOTE_VPS
            from forex_trader.sync.client import SyncClient
            host, _port, _token = SyncClient.load_config()
            if not host:
                return True  # standalone install, no counterpart to conflict with
            return db_module.get_active_trader() == TRADER_LOCAL
        except Exception:
            return True  # fail open rather than silently killing bot control

    async def _bot_command_loop(self) -> None:
        import httpx
        await asyncio.sleep(15)

        # Restore the update offset persisted from the previous run so we never
        # re-process commands (e.g. /restartapp) that were already handled before
        # the app shut down.
        try:
            saved = db_module.get_app_config("bot_update_offset")
            if saved:
                self._bot_offset = int(saved)
                log.info("Bot: restored update offset %d from DB", self._bot_offset)
        except Exception:
            pass

        # One persistent, connection-pooled client for this loop's whole
        # lifetime instead of a fresh httpx.AsyncClient (new TLS handshake)
        # on every ~1s getUpdates poll — that churn was the single most
        # frequently implicated coroutine in LoopMonitor's asyncio-debug
        # slow-callback trace (confirmed live 2026-07-09: thousands of
        # "SimulationEngine._bot_command_loop ... took 0.4-1.5s" entries,
        # far more than any other task). Reusing one client lets httpx keep
        # the connection to api.telegram.org warm across polls.
        _client = httpx.AsyncClient(timeout=12)
        try:
            # Register slash commands and clear any active webhook / conflicting getUpdates
            # session before we start polling. A 409 Conflict on getUpdates always means
            # either a webhook is set or another polling session is still open — deleteWebhook
            # removes both causes in one call. Skipped entirely if this node isn't the
            # current bot-command authority — calling deleteWebhook here would otherwise
            # interrupt the OTHER node's live polling session on every restart of this one.
            if await db_module.to_db_thread(self._is_bot_command_authority):
                cfg = db_module.get_telegram_config()
                if cfg.get("enabled") and cfg.get("bot_token_enc"):
                    try:
                        await _client.post(
                            f"https://api.telegram.org/bot{cfg['bot_token_enc']}/deleteWebhook",
                            params={"drop_pending_updates": "false"},
                            timeout=10,
                        )
                    except Exception:
                        pass
                    await telegram_alerts.register_commands(cfg["bot_token_enc"])

            _saved_offset = self._bot_offset  # track last-persisted value

            while self._monitor_running:
                try:
                    if not await db_module.to_db_thread(self._is_bot_command_authority):
                        await asyncio.sleep(5)
                        continue

                    cfg          = await db_module.to_db_thread(db_module.get_telegram_config)
                    token        = cfg.get("bot_token_enc", "")
                    allowed_chat = str(cfg.get("chat_id", ""))

                    if cfg.get("enabled") and token:
                        r = await _client.get(
                            f"https://api.telegram.org/bot{token}/getUpdates",
                            params={"offset": self._bot_offset, "timeout": 10},
                            timeout=12,
                        )
                        if r.status_code == 200:
                            for update in r.json().get("result", []):
                                uid = update.get("update_id", 0)
                                if uid >= self._bot_offset:
                                    self._bot_offset = uid + 1

                                msg     = update.get("message") or {}
                                chat_id = str(msg.get("chat", {}).get("id", ""))
                                text    = (msg.get("text") or "").strip()

                                # Security: only respond to the configured chat
                                if not text or not chat_id or chat_id != allowed_chat:
                                    continue

                                reply = await self._handle_bot_command(text)
                                if not reply:
                                    continue

                                await _client.post(
                                    f"https://api.telegram.org/bot{token}/sendMessage",
                                    json={
                                        "chat_id":    chat_id,
                                        "text":       reply,
                                        "parse_mode": "Markdown",
                                    },
                                    timeout=8,
                                )

                            # Persist offset after each successful poll so restarts
                            # resume from the correct position.
                            if self._bot_offset != _saved_offset:
                                await db_module.to_db_thread(
                                    db_module.set_app_config, "bot_update_offset",
                                    str(self._bot_offset),
                                )
                                _saved_offset = self._bot_offset

                        elif r.status_code == 409:
                            # Another session is polling — back off and retry deleteWebhook
                            log.warning("Bot polling 409 Conflict — clearing webhook and backing off 60s")
                            try:
                                await _client.post(
                                    f"https://api.telegram.org/bot{token}/deleteWebhook",
                                    params={"drop_pending_updates": "false"},
                                    timeout=10,
                                )
                            except Exception:
                                pass
                            await asyncio.sleep(60)
                            continue
                except httpx.TransportError as e:
                    # Connection dropped/reset — the pooled client recovers on
                    # its own next call; just avoid a busy-loop on repeated failures.
                    log.debug("Bot command loop transport error: %s", e)
                except Exception as e:
                    log.debug("Bot command loop error: %s", e)
                await asyncio.sleep(1)
        finally:
            await _client.aclose()

    async def _handle_bot_command(self, text: str) -> str:
        """Route an incoming message to the correct command handler."""
        if not text.startswith("/"):
            return ""
        parts = text.split()
        # Strip @BotName suffix Telegram appends in groups
        cmd  = parts[0].lstrip("/").split("@")[0].lower()
        args = parts[1:]

        handlers = {
            "help":              self._cmd_help,
            "balance":           self._cmd_balance,
            "daily":             self._cmd_daily,
            "status":            self._cmd_status,
            "trades":            self._cmd_trades,
            "pause":             self._cmd_pause,
            "resume":            self._cmd_resume,
            "close":             self._cmd_close,
            "strategy":          self._cmd_strategy,
            "risk":              self._cmd_risk,
            "marketbuy":         self._cmd_market_price_buy,
            "marketsell":        self._cmd_market_price_sell,
            "dpmon":             self._cmd_dpm_on,
            "dpmoff":            self._cmd_dpm_off,
            "imeon":             self._cmd_ime_on,
            "imeoff":            self._cmd_ime_off,
            "activate":          self._cmd_activate,
            "report":            self._cmd_report,
            "restartbridge":      self._cmd_restart_bridge,
            "restartapp":         self._cmd_restart_app,
            "switchlive":         self._cmd_switch_live,
            "switchdemo":         self._cmd_switch_demo,
            "headless":           self._cmd_headless,
        }
        handler = handlers.get(cmd)
        if handler:
            try:
                return await handler(args)
            except Exception as e:
                log.warning("Bot command /%s error: %s", cmd, e)
                return f"Error running /{cmd}: {e}"
        return f"Unknown command `/{cmd}`. Send /help to see all available commands."

    # ── Command handlers ──────────────────────────────────────────────────────

    async def _cmd_help(self, args: list) -> str:
        return await _cmd_help_impl(args)

    async def _cmd_balance(self, args: list) -> str:
        return await _cmd_balance_impl(args, self._bridge)

    async def _cmd_daily(self, args: list) -> str:
        """Send a daily summary: account state, today's closed trades and open positions."""
        return await _cmd_daily_impl(args, self._bridge)

    async def _cmd_status(self, args: list) -> str:
        return await _cmd_status_impl(args, self._bridge, self._tg_reader)

    async def _cmd_trades(self, args: list) -> str:
        return await _cmd_trades_impl(args, self._bridge)

    async def _cmd_pause(self, args: list) -> str:
        return await _cmd_pause_impl(args)

    async def _cmd_resume(self, args: list) -> str:
        return await _cmd_resume_impl(args)

    async def _cmd_close(self, args: list) -> str:
        open_trades = self.get_open_trades()
        if not open_trades:
            return "No open trades to close."

        if not args:
            return "Usage: /close all  or  /close `<ticket>`"

        if args[0].lower() == "all":
            n     = len(open_trades)
            lines = [f"Closing {n} open trade{'s' if n != 1 else ''}..."]
            total = 0.0
            for t in open_trades:
                try:
                    result = await self.close_trade(t["trade_id"], reason="manual_close")
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
        result = await self.close_trade(trade["trade_id"], reason="manual_close")
        pnl    = float(result.get("net_pnl", 0))
        cp     = float(result.get("close_price", 0))
        sign   = "+" if pnl >= 0 else ""
        return (
            f"Closed: {trade['direction']} {trade['lot_size']} lots @ ${cp:.2f}\n"
            f"P&L: {sign}${pnl:.2f}"
        )

    async def _cmd_strategy(self, args: list) -> str:
        return await _cmd_strategy_impl(args)

    async def _cmd_risk(self, args: list) -> str:
        return await _cmd_risk_impl(args, self._bridge)

    async def _cmd_activate(self, args: list) -> str:
        return await _cmd_activate_impl(
            args, self._bridge, self._cfg.get("starting_balance", 1000.0),
        )

    async def _cmd_report(self, args: list) -> str:
        return await _cmd_report_impl(args, self._bridge, self._cfg)

    # ── Bridge watchdog ───────────────────────────────────────────────────────

    async def _start_bridge_process(self) -> bool:
        """Tear down any running bridge and start a clean new one.

        On Windows: kills the native mt5_bridge.py process and restarts it directly.
        On macOS:   tears down the entire Wine session (wineserver + all children)
                    before starting a fresh instance, to avoid duplicate MT5 windows.

        With NativeMT5Bridge there's no separate process at all — recovery
        means reconnecting the in-process MT5 session instead of killing and
        relaunching a subprocess.

        Returns True if recovery was launched (not necessarily connected yet).
        """
        if self._using_native_bridge:
            log.info("Bridge watchdog: reconnecting in-process MT5 session (native bridge)")
            result = await self._bridge.reconnect()
            ok = result.get("status") == "connected"
            if not ok:
                log.warning("Bridge watchdog: native reconnect failed: %s", result.get("error"))
            return ok

        import subprocess, os as _os, sys as _sys
        from backend.src.utils import os_utils as _pu

        _bridge_py = _os.path.normpath(
            _os.path.join(_os.path.dirname(__file__), "..", "..", "mt5_bridge.py")
        )
        if not _os.path.isfile(_bridge_py):
            log.warning("Bridge watchdog: mt5_bridge.py not found at %s", _bridge_py)
            return False

        # ── Windows: native Python execution (no Wine) ────────────────────────
        if _sys.platform == "win32":
            log.info("Bridge watchdog: stopping native bridge process")
            _pu.kill_matching("mt5_bridge.py")
            await asyncio.sleep(2)
            _pu.kill_matching("mt5_bridge.py", force=True)
            await asyncio.sleep(1)

            from backend.src.config import USER_DATA_DIR, get as _cfg_get
            from urllib.parse import urlparse as _urlparse
            _creds = str(USER_DATA_DIR / "bridge_credentials.json")
            _bridge_port = _urlparse(_cfg_get("mt5_bridge_url", "")).port or 9010
            _env = {
                **_os.environ,
                "MT5_BRIDGE_PORT":   str(_bridge_port),
                "BRIDGE_CREDS_PATH": _creds,
            }
            try:
                subprocess.Popen(
                    [_sys.executable, _bridge_py],
                    env=_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    start_new_session=True,
                )
                log.info("Bridge watchdog: Windows native bridge subprocess launched")
                return True
            except Exception as _e:
                log.warning("Bridge watchdog: failed to start bridge: %s", _e)
                return False

        # ── macOS: full Wine session teardown then restart ─────────────────────
        import signal as _signal

        def _pids_of(pattern: str) -> list[int]:
            return _pu.pids_matching(pattern)

        def _kill_all(pids: list[int], sig: int) -> None:
            for pid in pids:
                try:
                    _os.kill(pid, sig)
                except (ProcessLookupError, OSError):
                    pass

        # Step 1: graceful shutdown of the entire Wine session
        log.info("Bridge restart: stopping Wine session (wineserver + all children)")
        _kill_all(_pids_of("wineserver"),    _signal.SIGTERM)
        _kill_all(_pids_of("mt5_bridge.py"), _signal.SIGTERM)
        await asyncio.sleep(3)

        # Step 2: force-kill any survivors
        for _pattern in ("wineserver", "mt5_bridge.py", "terminal64.exe", "winewrapper.exe"):
            _kill_all(_pids_of(_pattern), _signal.SIGKILL)

        await asyncio.sleep(2)

        _remaining_mt5 = _pids_of("terminal64.exe")
        if _remaining_mt5:
            log.warning(
                "Bridge restart: %d terminal64.exe process(es) still alive after kill — "
                "proceeding; may result in duplicate MT5 if CrossOver re-uses it",
                len(_remaining_mt5),
            )
        else:
            log.info("Bridge restart: clean slate — no terminal64.exe or wineserver running")

        try:
            from backend.src.config import load as _cfg_load, USER_DATA_DIR
            _cfg = _cfg_load()
            _wine = (
                _cfg.get("wine_bin")
                or "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"
            )
            _backend = _cfg.get("bridge_backend", "crossover")
            if _backend == "crossover":
                _bottle = _os.path.expanduser(
                    "~/Library/Application Support/CrossOver/Bottles/MetaTrader 5"
                )
                _extra_env = {"CX_BOTTLE": _bottle, "CX_NO_BROWSER": "1"}
            else:
                _bottle = _os.path.expanduser(
                    _cfg.get("mt5_bottle_path") or "~/.wine_mt5"
                )
                _extra_env = {}

            _bridge_win = "Z:" + _bridge_py.replace("/", "\\")
            _mac_creds  = str(USER_DATA_DIR / "bridge_credentials.json")
            _win_creds  = "Z:" + _mac_creds.replace("/", "\\")
            from urllib.parse import urlparse as _urlparse
            _bridge_port = _urlparse(_cfg.get("mt5_bridge_url", "")).port or 9010

            _env = {
                **_os.environ,
                "WINEPREFIX":        _bottle,
                "WINEDEBUG":         "-all",
                "MT5_BRIDGE_PORT":   str(_bridge_port),
                "BRIDGE_CREDS_PATH": _win_creds,
                **_extra_env,
            }
            subprocess.Popen(
                [_wine, "C:\\Python311\\python.exe", _bridge_win],
                env=_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("Bridge watchdog: macOS Wine bridge subprocess launched")
            return True
        except Exception as _e:
            log.warning("Bridge watchdog: failed to start bridge subprocess: %s", _e)
            return False

    async def _bridge_watchdog_loop(self) -> None:
        """Check bridge health every 60 s. Restart automatically unless the user
        explicitly stopped it via the Stop Bridge button (inhibit flag set).

        Requires CONSECUTIVE_FAIL_THRESHOLD failed checks in a row before
        restarting — a single failed /health call is not trusted on its own.
        The bridge's HTTP server processes one request at a time; under the
        concurrent polling load from four engines (main + breakout + bounce +
        reversal_engine all hitting /tick, /candles, /positions, /account on their own
        schedules) a slow request ahead of a health check in the queue can
        make that check exceed its 4s timeout with MT5 itself perfectly fine.
        That false positive triggered a real, disruptive bridge restart —
        actually causing the "frequent disconnect/reconnect" it was meant to
        prevent. Two failures 60s apart is no longer explainable by one queued
        request; only then is a real problem plausible enough to act on.
        """
        # 180s, not 30s: a full VPS/OS reboot needs MT5 terminal to cold-start
        # and log into the broker before the bridge can serve ticks — observed
        # taking up to ~150s in practice. With the old 30s wait plus two 60s-
        # apart checks (~90s total patience), every full reboot would cross
        # the failure threshold and trigger a false "bridge offline" restart
        # and alert while MT5 was simply still logging in. This only delays
        # the watchdog's first check; a genuine mid-session outage still gets
        # caught by the normal 60s-interval / 2-consecutive-failure logic
        # below once this initial window has passed.
        await asyncio.sleep(180)

        state = {"last_restart_at": 0.0, "was_connected": True, "consecutive_fails": 0}
        while self._monitor_running:
            sleep_for = await _bridge_watchdog_check_impl(
                self._bridge, state, self._bridge_inhibit_reconnect, self._start_bridge_process,
            )
            await asyncio.sleep(sleep_for)

    # ── Bot commands ──────────────────────────────────────────────────────────

    async def _cmd_restart_bridge(self, args: list) -> str:
        """Stop and restart the MT5 bridge process, then enable AutoTrading."""
        return await _cmd_restart_bridge_impl(args, self._bridge, self._start_bridge_process)

    async def _cmd_restart_app(self, args: list) -> str:
        """Restart the FOREX Trader app process (5-second delay so reply can send)."""
        return await _cmd_restart_app_impl(args, self._bot_offset)

    async def _cmd_headless(self, args: list) -> str:
        """Toggle headless mode (no web UI) and restart to apply it.

        The flag is only read once, at process start (run.py), before it
        decides whether to call ui.run() at all — NiceGUI owns the event
        loop for the whole process once started, so this can't be a live
        hot-toggle. Reuses the exact same restart mechanism as /restartapp,
        just with the flag flipped first, so it's one command end to end
        rather than "set a setting, then remember to also restart."
        """
        return await _cmd_headless_impl(args, self._cmd_restart_app)

    async def _cmd_switch_live(self, args: list) -> str:
        return await _cmd_switch_live_impl(args, self._bridge)

    async def _cmd_switch_demo(self, args: list) -> str:
        return await _cmd_switch_demo_impl(args, self._bridge)

    async def _cmd_switch_env(self, new_env: str) -> str:
        """Switch the active MT5 account environment (live/demo)."""
        return await _cmd_switch_env_impl(new_env, self._bridge)

    async def _cmd_market_price_buy(self, args: list) -> str:
        try:
            result = await self.open_manual_market_order("BUY")
            ticket = result.get("mt5_ticket", "—")
            entry  = float(result.get("entry_price", 0))
            lot    = result.get("lot_size") or result.get("remaining_lots", "?")
            return (
                f"*BUY order placed*\n"
                f"Entry: ${entry:.2f}  |  Lots: {lot}\n"
                f"MT5 Ticket: {ticket}"
            )
        except Exception as e:
            return f"Order failed: {telegram_alerts._md_esc(str(e))}"

    async def _cmd_market_price_sell(self, args: list) -> str:
        try:
            result = await self.open_manual_market_order("SELL")
            ticket = result.get("mt5_ticket", "—")
            entry  = float(result.get("entry_price", 0))
            lot    = result.get("lot_size") or result.get("remaining_lots", "?")
            return (
                f"*SELL order placed*\n"
                f"Entry: ${entry:.2f}  |  Lots: {lot}\n"
                f"MT5 Ticket: {ticket}"
            )
        except Exception as e:
            return f"Order failed: {telegram_alerts._md_esc(str(e))}"

    async def _cmd_dpm_on(self, args: list) -> str:
        return await _cmd_dpm_on_impl(args)

    async def _cmd_dpm_off(self, args: list) -> str:
        return await _cmd_dpm_off_impl(args)

    async def _cmd_ime_on(self, args: list) -> str:
        return await _cmd_ime_on_impl(args)

    async def _cmd_ime_off(self, args: list) -> str:
        return await _cmd_ime_off_impl(args)

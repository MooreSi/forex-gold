"""
SimulationEngine — core trading logic.
Extracted from vantage_mt5/service.py with FastAPI layer removed.
Callers invoke methods directly; no HTTP indirection.
"""

import asyncio
import json
import logging
import re
import time
from typing import Optional, TYPE_CHECKING

from backend.src.db import database as db_module
from backend.src.utils.models import (
    Tick,
    STRATEGY_SCALE_OUT,
    STRATEGY_SIGNAL_CLIMBER,
    STRATEGY_REVERSAL_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
    STRATEGY_LIMIT_RUNNER,
)
from backend.src.services.broker.mt5_client import MT5BridgeClient
from backend.src.services.broker.mt5_native import NativeMT5Bridge, is_available as _native_bridge_available
from backend.src.services.broker.debug_guard import reject_real_bridge_in_debug as _reject_real_bridge_in_debug
from backend.src.services.signals.parser import is_gd2_message
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.ai import claude_ai as claude_ai
from backend.src.services.ai import provider as ai_provider
from backend.src.services.trading import trade_repo
from backend.src.services.signals.scan_messages import (
    ScanCtx as _ScanCtx,
    scan_messages as _scan_messages_impl,
)
from backend.src.services.broker.bridge_process import (
    start_bridge_process as _start_bridge_process_impl,
)
from backend.src.services.broker.position_sync import (
    PositionSyncCtx as _PositionSyncCtx,
    sync_closed_mt5_positions as _sync_closed_mt5_positions_impl,
)
from backend.src.services.positions.monitor_cycle import (
    MonitorCtx as _MonitorCtx,
    MonitorState as _MonitorState,
    run_monitor_cycle as _run_monitor_cycle_impl,
)
from backend.src.services.telegram.bot_loop import (
    BotLoopCtx as _BotLoopCtx,
    bot_command_loop as _bot_command_loop_impl,
)
from backend.src.services.positions.tp_ladder_loop import (
    TPLadderCtx as _TPLadderCtx,
    tp_ladder_fast_loop as _tp_ladder_fast_loop_impl,
)
from backend.src.services.ai.model_refresh_loop import (
    ai_model_refresh_loop as _ai_model_refresh_loop_impl,
)
from backend.src.services.broker.watchdog_loop import (
    bridge_watchdog_loop as _bridge_watchdog_loop_impl,
)
from backend.src.services.reversal_engine.research_loop import (
    reversal_engine_research_loop as _reversal_engine_research_loop_impl,
)
# Re-exported for callers that import them from here rather than from the
# service that owns them -- see tests/refactor/test_runtime_has_no_dead_imports.py,
# which treats an external `from backend.src.runtime import X` as a use.
from backend.src.services.broker.mt5_performance import _apply_fee, _platform_fee_rate
from backend.src.services.positions.max_tp import _tp_level_from_extreme
from backend.src.services.risk import expert_params as _expert_params
from backend.src.services.cluster.node_roles import (
    is_active_trader_node as _is_active_trader_node_impl,
    is_bot_command_authority as _is_bot_command_authority_impl,
)
from backend.src.services.positions.max_tp import max_tp_checker_sweep as _max_tp_checker_sweep_impl
# ── Added by the 2026-08-25 upstream merge ────────────────────────────────────
from backend.src.services.positions.core_closed_market_queue import flush_queued_limits
from backend.src.services.positions.core_ref_signal_backfill import backfill_ref_signals
from backend.src.services.positions.core_pending_order_revalidation import (
    revalidate_pending_orders as _revalidate_pending_orders_impl,
)
from backend.src.services.positions.core_signal_snapshot import (
    capture_pending_snapshots as _capture_signal_snapshots_impl,
    capture_background_snapshot as _capture_background_snapshot_impl,
)
from backend.src.services.positions.core_ea_link_watchdog import (
    new_state as _ea_link_new_state,
    ea_link_check as _ea_link_check_impl,
)
from backend.src.services.trading.limit_order_signal import (
    handle_limit_order_signal as _handle_limit_order_signal_impl,
)
from backend.src.services.trading.fees_sizing import (
    pnl as _pnl_impl, suggest_lot_size as _suggest_lot_size_impl,
    calculate_fees as _calculate_fees_impl)
from backend.src.services.broker.mt5_performance import compute_mt5_performance as _compute_mt5_performance_impl
from backend.src.services.broker.deposits import get_total_deposits as _get_total_deposits_impl
from backend.src.services.analytics.reporting import (
    get_open_trades as _get_open_trades_impl,
    compute_performance as _compute_performance_impl,
)
from backend.src.services.broker.history_import import import_mt5_history as _import_mt5_history_impl
from backend.src.services.signals.tg_repo import get_tg_signals as _get_tg_signals_impl
from backend.src.services.positions.tp_tracking import (
    TPCache as _TPCache,
    get_triggered_tps as _get_triggered_tps_impl,
    last_closed_tp as _last_closed_tp_impl,
)
from backend.src.services.signals.repo import (
    create_signal as _create_signal_impl,
    get_signals as _get_signals_impl,
    cancel_signal as _cancel_signal_impl,
)
from backend.src.services.notifications.scheduler import email_scheduler_sweep as _email_scheduler_sweep_impl
# The bot command table lives in services/telegram/bot_dispatch.py (M4 B4);
# the runtime only binds its collaborators and keeps the four order/process
# commands it injects there.
from backend.src.services.telegram.bot_dispatch import BotDeps as _BotDeps
from backend.src.services.telegram.bot_infra import (
    cmd_restart_app as _cmd_restart_app_impl,
)
from backend.src.services.trading.profit_sync import (
    sync_profit as _sync_profit_impl,
    schedule_profit_sync as _schedule_profit_sync_impl,
)
from backend.src.services.trading.update_signal import update_signal as _update_signal_impl
from backend.src.services.risk.governor import (
    is_trading_paused as _is_trading_paused_impl,
    check_pre_trade_filters as _check_pre_trade_filters_impl,
    rg_apply_halts_on_close as _rg_apply_halts_on_close_impl,
)
from backend.src.services.positions.safety_net import tp_safety_net_sweep as _tp_safety_net_sweep_impl
from backend.src.services.broker.untracked import (
    get_untracked_mt5_positions as _get_untracked_mt5_positions_impl,
)
from backend.src.services.trading.ai_signal_fallback import (
    try_ai_signal_fallback as _try_ai_signal_fallback_impl,
    queue_unrecognised as _queue_unrecognised_impl,
)
from backend.src.services.trading.instant_followup import (
    apply_followup_to_instant_trade as _apply_followup_to_instant_trade_impl,
    find_and_apply_instant_followup as _find_and_apply_instant_followup_impl,
)
from backend.src.services.trading.partial_close import partial_close_trade as _partial_close_trade_impl
from backend.src.services.trading.open_trade import open_trade as _open_trade_impl
from backend.src.services.trading.manual_market_order import (
    open_manual_market_order as _open_manual_market_order_impl,
)
from backend.src.services.trading.manual_limit_order import (
    open_manual_limit_order as _open_manual_limit_order_impl,
)
from backend.src.services.trading.open_from_signal import (
    open_trade_from_signal as _open_trade_from_signal_impl,
)
from backend.src.services.trading.close_trade import (
    CloseTradeContext as _CloseTradeContext,
    get_trading_balance as _get_trading_balance_impl,
    close_trade as _close_trade_impl,
    record_close as _record_close_impl,
)
from backend.src.services.analytics.orb_report import build_orb_report as _build_orb_report_impl
from backend.src.services.dpm.bookkeeping import (
    DPMCache as _DPMCache,
    load_dpm_calibrated as _load_dpm_calibrated_impl,
    finalize_dpm_record as _finalize_dpm_record_impl,
)

if TYPE_CHECKING:
    from backend.src.services.telegram.reader import TelegramReader

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


class TradingRuntime:
    def __init__(self, config: dict):
        self._started_at = time.monotonic()
        self._cfg       = config
        self._bridge    = _reject_real_bridge_in_debug(_make_bridge(config), config)
        self._using_native_bridge = isinstance(self._bridge, NativeMT5Bridge)
        self._tg_reader: Optional["TelegramReader"] = None
        self._monitor_task: Optional[asyncio.Task] = None
        self._tp_ladder_fast_task: Optional[asyncio.Task] = None
        self._scanner_task: Optional[asyncio.Task] = None
        self._bot_task:     Optional[asyncio.Task] = None
        self._monitor_running = False
        # Cadence counters and adaptive-poll flags for the monitor loop.
        # Were four loose attributes here; the cycle that owns them lives in
        # services/positions/monitor_cycle.py now and mutates this object by
        # reference, so the counts survive from one cycle to the next.
        # (Upstream's _pending_revalidate_cycle joined it in the 2026-08-25
        # merge -- see MonitorState.pending_revalidate_cycle.)
        self._monitor_state = _MonitorState()
        self._bot_offset     = 0
        self._email_task:    Optional[asyncio.Task] = None
        # Incremented when a trade closes with gross profit > 0; UI polls to trigger sound
        self._profit_sound_seq: int = 0
        # Group tracking for signal scanner
        self._active_group_ids:   dict[str, int] = {}   # group_id_str → slot
        self._active_group_names: dict[str, str] = {}   # group_id_str → name
        # Candle cache — refreshed once per monitor loop cycle, shared across all trades
        self._dpm_candles: list[dict] = []
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
        self._auto_template_task: Optional[asyncio.Task] = None
        self._ai_model_refresh_task: Optional[asyncio.Task] = None
        self._data_retention_task: Optional[asyncio.Task] = None
        self._reversal_engine_research_task: Optional[asyncio.Task] = None
        # Throttle state for the "accept_tg_signals is OFF" warning, owned
        # here and handed to the scan context by reference so the once-per-
        # 5-minutes window spans scans. Was a lazily-set _tg_off_warned_at
        # attribute before the pipeline moved to services/signals.
        self._tg_off_warn_state: dict = {}
        self._closed_market_queue_task: Optional[asyncio.Task] = None
        self._ref_backfill_task: Optional[asyncio.Task] = None

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
        self._sigsnap_task        = asyncio.create_task(self._signal_snapshot_loop())
        self._bridge_watchdog_task = asyncio.create_task(self._bridge_watchdog_loop())
        self._max_tp_task         = asyncio.create_task(self._max_tp_checker_loop())
        self._signal_bus_prune_task = asyncio.create_task(self._signal_bus_prune_loop())
        self._tp_safety_net_task  = asyncio.create_task(self._tp_safety_net_loop())
        self._channel_ai_task     = asyncio.create_task(self._channel_ai_auto_eval_loop())
        self._auto_template_task  = asyncio.create_task(self._auto_template_loop())
        self._ai_model_refresh_task = asyncio.create_task(self._ai_model_refresh_loop())
        self._data_retention_task = asyncio.create_task(self._data_retention_loop())
        self._reversal_engine_research_task = asyncio.create_task(self._reversal_engine_research_loop())
        self._closed_market_queue_task = asyncio.create_task(self._closed_market_queue_loop())
        self._ref_backfill_task = asyncio.create_task(self._ref_backfill_loop())
        from backend.src.services.health.self_healer import SelfHealer
        self._self_healer = SelfHealer(self)
        self._self_healer.start()
        # EA bridge — local TCP listener a companion MQL5 EA connects to.
        # Always started (cheap, idle if nothing connects); actual handoff of
        # any trade only happens when ea_bridge_enabled is on in Risk Settings
        # AND the EA is actually connected — see open_trade()'s handoff check.
        try:
            from backend.src.services.broker import ea_bridge as _ea_mod
            self._ea_bridge = _ea_mod.EABridge(self)
            await self._ea_bridge.start()
            _ea_mod.set_instance(self._ea_bridge)
            self._ea_link_task = asyncio.create_task(self._ea_link_watchdog_loop())
        except Exception as _ea_e:
            log.warning("[EA] bridge failed to start: %s", _ea_e)
        log.info("TradingRuntime started")

    async def shutdown(self) -> None:
        self._monitor_running = False
        for t in (self._monitor_task, self._tp_ladder_fast_task, self._scanner_task, self._bot_task,
                  self._email_task, self._bridge_watchdog_task,
                  self._max_tp_task, self._signal_bus_prune_task,
                  self._tp_safety_net_task, self._channel_ai_task,
                  self._auto_template_task,
                  self._ai_model_refresh_task, self._data_retention_task,
                  self._reversal_engine_research_task,
                  self._closed_market_queue_task,
                  self._ref_backfill_task,
                  getattr(self, "_ea_link_task", None)):
            if t and not t.done():
                t.cancel()
        if hasattr(self, "_self_healer"):
            self._self_healer.stop()
        if hasattr(self, "_ea_bridge"):
            await self._ea_bridge.stop()
        await self._bridge.shutdown()
        log.info("TradingRuntime stopped")

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
            background_open_commentary=self.background_open_commentary,
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
            background_open_commentary=self.background_open_commentary,
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

    async def close_trade(self, trade_id: str, reason: str = "manual_close") -> dict:
        return await _close_trade_impl(trade_id, reason, self._make_close_trade_ctx())

    async def record_close(self, trade_id: str, close_price: float, reason: str) -> dict:
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

    # ── Performance ───────────────────────────────────────────────────────────

    def compute_performance(self) -> dict:
        starting = float(self._cfg.get("starting_balance", 1000.0))
        return _compute_performance_impl(starting)

    # ── TP trigger tracking ───────────────────────────────────────────────────

    async def get_triggered_tps(self, trade_id: str) -> set[int]:
        return await _get_triggered_tps_impl(self._tp_trigger_cache, trade_id)

    _TP_WAIT_LOG_INTERVAL = 60.0  # seconds between diagnostic log lines per trade

    # ── Strategy handlers ─────────────────────────────────────────────────────

    # ── DPM helpers ──────────────────────────────────────────────────────────

    def _load_dpm_calibrated(self) -> dict:
        """
        Load calibrated DPM multipliers from app_config into a flat dict keyed
        "{session}_{momentum_bucket}".  Refreshed at most once per 10 minutes.
        """
        return _load_dpm_calibrated_impl(self._dpm_cache)

    def _finalize_dpm_record(self, trade_id: str, close_price: float,
                             exit_type: str, final_pnl: float) -> None:
        """Write close-time fields to the DPM performance record."""
        _finalize_dpm_record_impl(trade_id, close_price, exit_type, final_pnl)

    # ── Dynamic Position Management ──────────────────────────────────────────

    # ── Pause check ───────────────────────────────────────────────────────────

    def is_trading_paused(self) -> bool:
        return _is_trading_paused_impl()

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

    def _rg_apply_halts_on_close(self, rs: dict, balance: float) -> None:
        """After a close, trip the pause flag if a circuit breaker fired."""
        return _rg_apply_halts_on_close_impl(rs, balance)

    # ── Pending signal watcher ────────────────────────────────────────────────

    # ── Monitor loop ──────────────────────────────────────────────────────────

    def _make_monitor_ctx(self) -> _MonitorCtx:
        """Bind one monitor cycle's collaborators.

        state is passed by reference, not copied: the cycle counters and
        the adaptive-poll flags have to survive into the next cycle.
        dpm_candles stays on the runtime behind get/set because
        open_trade_from_signal and the scan context read it too.
        """
        return _MonitorCtx(
            state=self._monitor_state,
            bridge=self._bridge,
            cfg=self._cfg,
            tp_trigger_cache=self._tp_trigger_cache,
            dpm_cache=self._dpm_cache,
            scale_out_last_fail=self._scale_out_last_fail,
            pending_activation_retry_after=self._pending_activation_retry_after,
            get_dpm_candles=lambda: self._dpm_candles,
            set_dpm_candles=self._set_dpm_candles,
            get_tick=self.get_tick,
            get_open_trades=self.get_open_trades,
            get_candles=self.get_candles,
            is_trading_paused=self.is_trading_paused,
            background_open_commentary=self.background_open_commentary,
            close_full_after_tps=self._close_full_after_tps,
            make_close_trade_ctx=self._make_close_trade_ctx,
            sync_closed_mt5_positions=self._sync_closed_mt5_positions,
            close_trade=self.close_trade,
        )

    def _set_dpm_candles(self, candles: list) -> None:
        self._dpm_candles = candles

    async def _monitor_loop(self) -> None:
        while self._monitor_running:
            fast_poll = await _run_monitor_cycle_impl(self._make_monitor_ctx())
            # Fast poll (1s) when trades are open OR a queued signal is waiting
            # for its zone to be re-entered — a GD2-style queued signal with no
            # other trade open would otherwise only get checked every 5s, adding
            # up to ~5s of pure polling latency on top of everything else once
            # price actually returns to the zone. Slow poll (5s) only when
            # there is truly nothing time-sensitive to watch.
            await asyncio.sleep(1 if fast_poll else 5)

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
        """Owns the task; one pass lives in
        services/positions/tp_ladder_loop.py."""
        await _tp_ladder_fast_loop_impl(_TPLadderCtx(
            is_running=lambda: self._monitor_running,
            poll_interval=self._TP_LADDER_POLL_INTERVAL,
            ladder_strategies=self._TP_LADDER_STRATEGIES,
            bridge=self._bridge,
            tp_trigger_cache=self._tp_trigger_cache,
            get_fresh_tick=self.get_fresh_tick,
            get_open_trades=self.get_open_trades,
            close_full_after_tps=self._close_full_after_tps,
        ))

    # ── MT5 position sync ─────────────────────────────────────────────────────

    # Kept for callers that read it off the class; the live value comes
    # from Settings > Expert Tunables via _make_position_sync_ctx.
    MT5_SYNC_MISS_THRESHOLD = 2

    def _make_position_sync_ctx(self) -> _PositionSyncCtx:
        """Bind reconciliation's collaborators in one place.

        The miss-streak dict is passed by reference, not copied: it counts
        consecutive cycles and a copy would reset it every pass.
        """
        return _PositionSyncCtx(
            bridge=self._bridge,
            mt5_sync_missing_streak=self._mt5_sync_missing_streak,
            miss_threshold=_expert_params.get("mt5_sync_miss_threshold"),
            get_tick=self.get_tick,
            partial_close_trade=self.partial_close_trade,
            record_close=self.record_close,
            sync_profit=self.sync_profit,
            schedule_profit_sync=self._schedule_profit_sync,
            get_mt5_account=self.get_mt5_account,
        )

    async def _sync_closed_mt5_positions(self) -> None:
        return await _sync_closed_mt5_positions_impl(self._make_position_sync_ctx())

    async def sync_profit(self, trade_id: str, mt5_ticket: int) -> Optional[float]:
        return await _sync_profit_impl(trade_id, mt5_ticket, self._bridge)

    async def _schedule_profit_sync(self, trade_id: str, mt5_ticket: int) -> None:
        return await _schedule_profit_sync_impl(trade_id, mt5_ticket, self._bridge)

    async def schedule_profit_sync(self, trade_id: str, mt5_ticket: int) -> None:
        """Public delegate to _schedule_profit_sync, for collaborators.

        EABridge schedules a profit sync when the EA reports a close; upstream
        did that by calling the private directly, which
        tests/core/test_runtime_facade.py forbids. Added as a delegate rather
        than by renaming the private, because the private is bound into the
        close-path context and CLAUDE.md freezes that shape -- renaming it
        reshapes a close-path binding, which needs owner sign-off and a demo.
        Allowlisted in facade_allowlist.json. (2026-08-25 merge.)"""
        return await self._schedule_profit_sync(trade_id, mt5_ticket)

    async def _revalidate_pending_orders(self) -> None:
        return await _revalidate_pending_orders_impl(self._bridge)


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
                trade_repo.reopen_residual_trade(trade_id, residual_vol)
                mt5_res = await self._bridge.close_position(int(mt5_ticket))
                if mt5_res.get("success"):
                    await self.record_close(trade_id, float(mt5_res.get("close_price", close_price)),
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
            await self.sync_profit(trade_id, int(mt5_ticket))
            asyncio.create_task(self._schedule_profit_sync(trade_id, int(mt5_ticket)))
        closed_row = trade_repo.get_trade(trade_id)
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

    async def _await_trade_promotion(self, trade_id: str,
                                      timeout: float = 15.0) -> Optional[dict]:
        """The trade's current DB row, waiting out an EA Template placeholder.

        A template trade's row is INSERTed with mt5_ticket=0/entry_price=0.0 --
        at that moment the EA has only staged the legs, so no broker ticket or
        fill price exists yet. The first leg to fill promotes the row (see
        EABridge._on_template_leg_filled), normally within seconds. Poll for
        that promotion so the trade-open alert can carry the real ticket and
        entry instead of the placeholder zeros.

        Never blocks the alert indefinitely: a grid whose legs all sit unfilled
        is a legitimate state, and fmt_trade_open() reports that case
        explicitly. Non-placeholder rows (every Python-managed trade, and any
        template row already promoted) return on the first read with no wait.
        """
        deadline = time.monotonic() + timeout

        row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
        while row and not row.get("mt5_ticket") and time.monotonic() < deadline:
            await asyncio.sleep(1.0)
            row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
        return row

    async def background_open_commentary(self, trade_id: str, sig: dict, tick: Tick) -> None:
        try:
            candles = await self.get_candles("M5", 20)
            trade_row = trade_repo.get_trade(trade_id)
            commentary = await claude_ai.request_commentary(
                "trade_open", trade_row, sig, tick, candles, self._cfg,
            )
            db_module.save_commentary(commentary, trade_id, sig.get("signal_id"))
            trade_repo.set_open_commentary(trade_id, json.dumps(commentary))
            # Re-read the row instead of reusing the copy fetched above: an EA
            # Template row is INSERTed as a placeholder (mt5_ticket=0,
            # entry_price=0.0) and only gains its real ticket and fill price
            # when the first leg fills, which normally happens while the
            # commentary request above is still in flight. The stale copy is
            # what produced "MT5 Ticket: 0 / Entry: 0.0" alerts.
            fresh_row = await self._await_trade_promotion(trade_id) or trade_row
            await telegram_alerts.send_message(
                telegram_alerts.fmt_trade_open(fresh_row, tick, commentary), trade_id, "trade_open",
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
            quick = await db_module.to_db_thread(
                trade_repo.get_trade_mt5_ticket, trade_id)
            mt5_ticket = (quick or {}).get("mt5_ticket")
            if mt5_ticket:
                try:
                    await asyncio.wait_for(
                        self.sync_profit(trade_id, int(mt5_ticket)), timeout=8.0
                    )
                except (asyncio.TimeoutError, Exception) as _e:
                    log.debug("Close commentary: profit sync skipped (%s)", _e)

            account = await self.get_mt5_account()
            row = await db_module.to_db_thread(trade_repo.get_trade, trade_id)
            last_tp = await db_module.to_db_thread(_last_closed_tp_impl, trade_id) if reason == "SL" else None
            await telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(row, result, {}, account, last_tp=last_tp),
                trade_id, reason,
            )
        except Exception as e:
            log.warning("Close notification failed %s: %s", trade_id, e)

    # ── Unrecognised message handling ─────────────────────────────────────────

    @staticmethod
    def _is_active_trader_node() -> bool:
        """Whether THIS node may execute real trades. See
        services/cluster/node_roles.py. Kept as a staticmethod on the class
        because several characterization packs patch it by name here."""
        return _is_active_trader_node_impl()

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

    def _queue_unrecognised(self, tg_id: str, channel_name: str, text: str) -> None:
        return _queue_unrecognised_impl(tg_id, channel_name, text, self._cfg)

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

    async def apply_followup_to_instant_trade(
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

    def _make_scan_ctx(self) -> _ScanCtx:
        """Bind the scan pipeline's collaborators in one place.

        Same pattern as _make_close_trade_ctx: the service owns the logic,
        the runtime owns the wiring. Rebuilt per scan so _dpm_candles and
        _cfg reflect the current cycle, exactly as the inline body's
        `self.` lookups did.
        """
        return _ScanCtx(
            bridge=self._bridge,
            tg_reader=self._tg_reader,
            cfg=self._cfg,
            dpm_candles=self._dpm_candles,
            tg_off_warn_state=self._tg_off_warn_state,
            engine_for_eval=self,
            close_trade=self.close_trade,
            try_ai_signal_fallback=self._try_ai_signal_fallback,
            find_and_apply_instant_followup=self._find_and_apply_instant_followup,
            get_trading_balance=self._get_trading_balance,
            suggest_lot_size=self.suggest_lot_size,
            queue_unrecognised=self._queue_unrecognised,
            is_trading_paused=self.is_trading_paused,
            get_open_trades=self.get_open_trades,
            check_pre_trade_filters=self._check_pre_trade_filters,
            open_trade=self.open_trade,
        )

    async def _scan_messages(self) -> list[dict]:
        return await _scan_messages_impl(self._make_scan_ctx())

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

    # Deliberately its own loop rather than a step inside _monitor_loop:
    # that loop's whole body sits behind `if tick:`, and over a weekend --
    # exactly when this queue fills up -- there is no tick to be had, so a
    # step added there would never run at the moment it's needed. 60s is
    # ample for a reopen the caller only has to notice within a minute.
    _CLOSED_MARKET_FLUSH_INTERVAL = 60  # seconds between reopen checks

    async def _closed_market_queue_loop(self) -> None:
        while self._monitor_running:
            try:
                rs = await db_module.to_db_thread(db_module.get_risk_settings)
                if bool(rs.get("lk_queue_closed_market_limits", 0)):
                    async def _place(parsed, tg_id, channel_name, source_label):
                        return await _handle_limit_order_signal_impl(
                            parsed, tg_id, channel_name, source_label, rs,
                            True, False, "", "",
                            get_trading_balance_fn=self._get_trading_balance,
                            suggest_lot_size_fn=self.suggest_lot_size,
                            bridge=self._bridge,
                        )
                    await flush_queued_limits(rs, _place)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("_closed_market_queue_loop error: %s", e)
            await asyncio.sleep(self._CLOSED_MARKET_FLUSH_INTERVAL)

    # Hourly rather than startup-only: while accept_tg_signals is off, new
    # messages keep arriving and keep going unparsed, so a one-shot at boot
    # would leave the REF feed going stale again within the hour. Reads and
    # records only -- see core_ref_signal_backfill's module docstring for why
    # this can never open a trade.
    _REF_BACKFILL_INTERVAL = 3600

    async def _ref_backfill_loop(self) -> None:
        while self._monitor_running:
            try:
                await db_module.to_db_thread(backfill_ref_signals)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("_ref_backfill_loop error: %s", e)
            await asyncio.sleep(self._REF_BACKFILL_INTERVAL)

    async def _auto_template_loop(self) -> None:
        """Supervise auto template management. One tick per minute.

        The tick itself -- regime detection, baseline forcing and the paid AI
        review cadence -- is core_auto_template.run_auto_template_cycle. It
        lives there rather than here because it is decision logic with a live
        scar behind it (forcing the baseline every tick reverts an AI override
        within 60s), and because in here it had no test.

        State is created here and passed in, so nothing about this loop
        persists at module level in the service.
        """
        from backend.src.services.positions import core_auto_template as _auto

        await asyncio.sleep(90)   # let the first M5 window populate
        state = _auto.AutoTemplateState()

        while self._monitor_running:
            try:
                await _auto.run_auto_template_cycle(
                    state, self._dpm_candles, self._cfg, self)
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.debug("_auto_template_loop error: %s", e)
            await asyncio.sleep(60)

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
        """Owns the task; the refresh itself lives in
        services/ai/model_refresh_loop.py."""
        await _ai_model_refresh_loop_impl(self._cfg, lambda: self._monitor_running)

    async def _tp_safety_net_sweep(self) -> None:
        return await _tp_safety_net_sweep_impl(self._bridge, self._tp_safety_net_last_alert)

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
        """Owns the task; the sweep lives in
        services/reversal_engine/research_loop.py."""
        await _reversal_engine_research_loop_impl(self, lambda: self._monitor_running)

    # ── Morning ORB / IVB report ──────────────────────────────────────────────
    # Classic Opening-Range-Breakout methodology (rebuilt 2026-08-01) --
    # see core_orb_report.py's module docstring for the full rationale:
    # whole-Asian-session (00:00-08:00 UTC) range as a confirmation filter,
    # the first 15 minutes of London (08:00-08:15 UTC) as the traded
    # opening range, stop at the opening range's midpoint, target at 2x the
    # resulting risk. Delegates entirely to core_orb_report.build_orb_report
    # -- this method only exists as SimulationEngine's public entry point.

    async def build_orb_report(self) -> Optional[dict]:
        return await _build_orb_report_impl(self._bridge)

    # ── Email scheduler ───────────────────────────────────────────────────────

    async def _signal_snapshot_loop(self) -> None:
        """Supervise the signal-snapshot research log. One tick per 5s.

        The tick -- the per-signal capture plus the 15-minute background
        negatives and the 60s pro-outcome resolve, each isolated from the
        others' failures -- is core_signal_snapshot.run_snapshot_cycle.

        Polls rather than hooking the parser: vantage_tg_signals has seven
        separate INSERT sites, and a research log must not be able to break
        signal processing. 5s keeps capture lag small enough that the
        candle-derived indicators are effectively contemporaneous.
        """
        from backend.src.services.positions import core_signal_snapshot as _snap

        await asyncio.sleep(20)   # let the bridge settle after startup
        state = _snap.SnapshotState()

        while self._monitor_running:
            try:
                await _snap.run_snapshot_cycle(
                    state, self._bridge,
                    capture=_capture_signal_snapshots_impl,
                    background=_capture_background_snapshot_impl)
            except asyncio.CancelledError:
                break
            except Exception:
                log.debug("Signal snapshot cycle failed", exc_info=True)
            await asyncio.sleep(5)
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
        """Whether THIS node owns Telegram getUpdates polling. See
        services/cluster/node_roles.py."""
        return _is_bot_command_authority_impl()

    async def _bot_command_loop(self) -> None:
        """Owns the task; the polling lives in
        services/telegram/bot_loop.py."""
        await _bot_command_loop_impl(_BotLoopCtx(
            is_running=lambda: self._monitor_running,
            is_bot_command_authority=self._is_bot_command_authority,
            make_bot_deps=self._make_bot_deps,
            get_bot_offset=lambda: self._bot_offset,
            set_bot_offset=self._set_bot_offset,
        ))

    def _set_bot_offset(self, offset: int) -> None:
        self._bot_offset = offset

    # ── Command handlers ──────────────────────────────────────────────────────

    async def close_cmd(self, args: list) -> str:
        """Public delegate to _cmd_close, for collaborators.

        The EA's on-chart panel has a CLOSE ALL button, and its handler in
        ea_bridge imported cmd_close out of services/trading/bot_trading --
        a function this branch deleted (2847e32) as an unwired extraction while
        the live implementation stayed here. The 2026-08-25 merge brought the
        panel across without noticing, so that button raised ImportError and
        reported "CLOSE_ALL FAILED" instead of closing anything.

        A delegate rather than a rename: _cmd_close drives the frozen close
        path, and tests/core/test_runtime_facade.py forbids a service reaching
        into a runtime private. Allowlisted in facade_allowlist.json.

        MONEY PATH: this closes real positions. Needs the demo-session sign-off
        (session-agenda Part B) before it is trusted, like everything else that
        can close a trade."""
        return await self._cmd_close(args)

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

    # ── Bridge watchdog ───────────────────────────────────────────────────────

    async def start_bridge_process(self) -> bool:
        """Tear down any running bridge and start a clean new one.

        Body lives in services/broker/bridge_process.py; the runtime keeps
        the name because the self-healer and the bot's /restartbridge both
        reach for it here.
        """
        return await _start_bridge_process_impl(self._bridge, self._using_native_bridge)

    async def _bridge_watchdog_loop(self) -> None:
        """Owns the task; the health check lives in
        services/broker/watchdog_loop.py. Both flags are passed as callables
        so the loop sees shutdown and Stop Bridge while it is awaiting."""
        await _bridge_watchdog_loop_impl(
            self._bridge,
            lambda: self._monitor_running,
            lambda: self._bridge_inhibit_reconnect,
            self.start_bridge_process,
        )

    async def _ea_link_watchdog_loop(self) -> None:
        """Watch the EA's socket link, keep every port it might dial open, and
        bounce the terminal if it stays down.

        Separate from _bridge_watchdog_loop above: that one watches the MT5
        *bridge* and acts when it goes unhealthy, this one watches the *EA
        inside the terminal*, which can be dead while the bridge is perfectly
        fine -- exactly the 2026-08-07 outage, where the bridge watchdog
        correctly did nothing for four hours. Both end up calling the same
        _start_bridge_process; core_ea_link_watchdog checks bridge health
        first so only one of them ever owns a given restart.
        """
        # Only the macOS/Wine path of _start_bridge_process tears the terminal
        # down (wineserver + terminal64.exe), which is what makes MT5 restore
        # its charts and reload the expert. The Windows path restarts
        # mt5_bridge.py alone and the native bridge just reconnects in-process
        # -- on both, the terminal keeps running and the EA is NOT reloaded, so
        # a restart would drop the bridge for no gain. Withhold the restarter
        # there and let the watchdog run alert-only.
        import sys as _sys
        _restart_reloads_ea = (
            not self._using_native_bridge and _sys.platform != "win32"
        )
        if not _restart_reloads_ea:
            log.info("[EALink] automatic EA recovery unavailable on this bridge "
                     "(restarting it would not reload the expert) — alert-only")

        state = _ea_link_new_state()
        while self._monitor_running:
            sleep_for = await _ea_link_check_impl(
                self._ea_bridge, state,
                mt5_bridge=self._bridge,
                restart_bridge=(
                    self._start_bridge_process if _restart_reloads_ea else None
                ),
                inhibit_reconnect=self._bridge_inhibit_reconnect,
            )
            await asyncio.sleep(sleep_for)

    # ── Bot commands ──────────────────────────────────────────────────────────

    def _make_bot_deps(self) -> _BotDeps:
        """Bind the command table's collaborators once (M4 B4).

        Same idiom as _make_close_trade_ctx. The four order/process commands
        below are injected rather than moved: their bodies stay here, so the
        order path is untouched by the dispatcher's relocation.
        """
        return _BotDeps(
            bridge=self._bridge,
            tg_reader=self._tg_reader,
            cfg=self._cfg,
            bot_offset=self._bot_offset,
            start_bridge_process=self.start_bridge_process,
            close_cmd=self._cmd_close,
            market_buy_cmd=self._cmd_market_price_buy,
            market_sell_cmd=self._cmd_market_price_sell,
            restart_app_cmd=self.restart_app,
        )

    async def restart_app(self, args: list) -> str:
        """Restart the FOREX Trader app process (5-second delay so reply can send)."""
        return await _cmd_restart_app_impl(args, self._bot_offset)

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


# Compatibility alias. The class was SimulationEngine until M4's final step;
# the name predated the app doing real broker work and had stopped being
# true. This keeps any caller that still imports the old name pointing at
# the same object, so the rename cannot half-apply.
SimulationEngine = TradingRuntime

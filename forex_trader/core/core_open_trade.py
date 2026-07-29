"""Trade opening -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.open_trade, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-open-trade-migration/020-*.md.

Places an order via bridge.place_order (Python bridge path) or the EA
bridge's open_trade (EA-managed path) -- the real MT5 order-placement
calls, unchanged from the original. This module places no order itself;
it only calls whatever `bridge` (and whatever ea_bridge/sync singletons
are configured) its caller supplies.

Takes `bridge` as an explicit parameter instead of self._bridge/
self.get_fresh_tick(). ea_bridge, sync.server, sync.client, db_module, and
core_risk_governor.is_trading_paused (pack 1) are imported and used
directly -- all already accessed via module-level get_instance()
singletons or db_module in the original, so no new state-carrier class is
needed here (unlike pack 10's CloseTradeContext).
"""
from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core.core_risk_governor import is_trading_paused
from forex_trader.core.models import (
    Tick, STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER,
    STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_FIXED_RR,
    STRATEGY_ADAPTIVE_RUNNER_2,
)

log = logging.getLogger(__name__)

# ── EA-ladder lookup tables (verbatim copies -- also used elsewhere in
# engine.py by the signal_climber/reversal_runner handlers, out of scope here) ──

_CLIMBER_PCTS: dict[int, list[float]] = {
    1: [1.00],
    2: [0.40, 0.60],
    3: [0.30, 0.30, 0.40],
    4: [0.20, 0.25, 0.25, 0.30],
    5: [0.20, 0.15, 0.15, 0.20, 0.30],
    6: [0.20, 0.15, 0.15, 0.15, 0.20, 0.15],
    7: [0.20, 0.10, 0.10, 0.15, 0.15, 0.20, 0.10],
    8: [0.20, 0.10, 0.10, 0.10, 0.15, 0.15, 0.10, 0.10],
}

_GDVR_PCTS: dict[int, list[float]] = {
    1: [1.00],
    2: [0.30, 0.70],
    3: [0.15, 0.25, 0.60],
    4: [0.10, 0.15, 0.25, 0.50],
    5: [0.10, 0.10, 0.15, 0.25, 0.40],
    6: [0.08, 0.08, 0.12, 0.17, 0.20, 0.35],
    7: [0.07, 0.07, 0.10, 0.13, 0.13, 0.20, 0.30],
    8: [0.05, 0.05, 0.10, 0.10, 0.15, 0.15, 0.15, 0.25],
}

# EA handoff: the three _run_tp_ladder-shaped strategies send their close-%
# table and BE-trigger position to the EA at trade-open time (see
# ea_bridge.EABridge.open_trade's pcts/be_at_pos params) rather than the EA
# hardcoding its own copy — Python stays the single source of truth, so a
# tuning change here takes effect on EA-managed trades with no MQL5 rebuild.
_EA_LADDER_PCTS = {
    STRATEGY_SIGNAL_CLIMBER: _CLIMBER_PCTS,
    STRATEGY_REVERSAL_RUNNER: _GDVR_PCTS,
    STRATEGY_ADAPTIVE_RUNNER: _GDVR_PCTS,
    # Adaptive Runner 2's own stated 8-TP schedule (5/5/10/10/15/15/15/25) is
    # an exact match for _GDVR_PCTS -- reuses the same table rather than
    # duplicating it, same as Adaptive Runner does.
    STRATEGY_ADAPTIVE_RUNNER_2: _GDVR_PCTS,
}
_EA_LADDER_BE_AT_POS = {
    STRATEGY_SIGNAL_CLIMBER: 0,
    STRATEGY_REVERSAL_RUNNER: 1,
    STRATEGY_ADAPTIVE_RUNNER: 0,
    STRATEGY_ADAPTIVE_RUNNER_2: 1,   # BE at TP2, not TP1
}
# EA-side SL-trail rule -- see ManageLadder() in ForexTraderBridge.mq5.
# "prev_tp" (the default the EA already had, sent when this key is absent)
# trails to the single immediately-previous TP price. Adaptive Runner 2 is
# the first strategy to need a different rule -- see run_tp_ladder()'s
# sl_rule parameter (core_run_tp_ladder.py) for the Python-side equivalent,
# which this table must stay in lockstep with.
_EA_LADDER_TRAIL_MODE = {
    STRATEGY_ADAPTIVE_RUNNER_2: "midpoint_lag2",
}


async def open_trade(
    bridge: Any,
    signal_id: str, direction: str, entry_low: float, entry_high: float,
    stop_loss: float,
    tp1=None, tp2=None, tp3=None, tp4=None, tp5=None,
    tp6=None, tp7=None, tp8=None,
    lot_size: float = 0.01, tick: Optional[Tick] = None,
    strategy: str = STRATEGY_SCALE_OUT,
    tg_source: Optional[str] = None,
    mt5_tp_override: Optional[float] = None,
) -> dict:
    # Remote stand-down — the Local/Remote sync feature's mutual-exclusion
    # gate. Deliberately separate from the manual pause flag below: pause
    # can be cleared by the user's Resume button at any time, which must
    # NEVER be able to re-arm this node while the other node believes it
    # still holds control. Only RESUME over the sync channel clears this.
    # No-op (near-zero cost) on any install that hasn't configured sync —
    # get_instance() returns None until sync.server.init() has run.
    try:
        from forex_trader.sync import server as _sync_srv_mod
        _srv = _sync_srv_mod.get_instance()
        if _srv is not None and _srv.is_standing_down():
            raise ValueError(
                "Trading stood down — the paired Local/Remote node has taken "
                "control (see Settings > Remote Node)."
            )
    except ImportError:
        pass

    # Mirror of the check above, for the client (Mac) role — the VPS-side
    # is_standing_down() check only ever fires on whichever node runs the
    # sync SERVER (the VPS), so without this the Mac's own open_trade()
    # was never actually gated by the Local/Remote toggle at all: setting
    # the Mac to "Remote" mode stopped its Breakout/Bounce/Reversal Engine sub-
    # engines but left its own Telegram-signal execution running
    # unconditionally, causing the same incoming signal (both nodes run
    # separate Telethon sessions on the same Telegram account) to open
    # duplicate real trades on the shared MT5 account from both sides at
    # once. Only applies if this Mac has actually been paired with a VPS
    # (a standalone install with no pairing configured is never gated).
    try:
        from forex_trader.sync.protocol import TRADER_REMOTE_VPS
        from forex_trader.sync.client import SyncClient
        _host, _, _ = SyncClient.load_config()
        if _host and db_module.get_active_trader() == TRADER_REMOTE_VPS:
            # Centralized signal generation (Settings > Remote Node): this
            # node keeps analyzing (should_generate_signals_here() only
            # ever returns False on the VPS side), but the VPS has
            # stopped analyzing entirely under this mode, so forward the
            # already-fully-resolved trade there for execution instead of
            # just failing. Mirrors the manual Market Order button's
            # MSG_MARKET_ORDER forwarding (sync/client.py:send_market_order)
            # but carries this call's full parameter set, since the VPS
            # calls open_trade() directly rather than re-resolving
            # anything via open_trade_from_signal.
            _rs = await db_module.to_db_thread(db_module.get_risk_settings)
            if _rs.get("centralized_signal_gen_enabled"):
                from forex_trader.sync.client import get_instance as _sync_cli_instance
                _cli = _sync_cli_instance()
                _ack = await _cli.send_signal_order(
                    signal_id=signal_id, direction=direction,
                    entry_low=entry_low, entry_high=entry_high, stop_loss=stop_loss,
                    tp1=tp1, tp2=tp2, tp3=tp3, tp4=tp4, tp5=tp5,
                    tp6=tp6, tp7=tp7, tp8=tp8,
                    lot_size=lot_size, strategy=strategy, tg_source=tg_source,
                )
                # _ack is the raw {"type": "signal_order_ack", "result": {...}}
                # or {"type": ..., "error": "..."} message — callers (
                # open_trade_from_signal, open_manual_market_order) expect
                # this call to return the same shape as a local open_trade()
                # (a plain {"trade_id", "mt5_ticket", ...} dict) or raise on
                # failure, exactly like a local execution would. Returning
                # the ack envelope unwrapped previously left result["trade_id"]
                # missing (KeyError further down every forwarded call).
                if _ack.get("error"):
                    raise RuntimeError(f"VPS rejected forwarded trade: {_ack['error']}")
                _result = dict(_ack.get("result") or {})
                _result["executed_remotely"] = True
                return _result
            raise ValueError(
                "Trading stood down — the VPS is the active trader "
                "(see Settings > Remote Node)."
            )
    except ImportError:
        pass

    # Trading pause — blocks MT5 order placement only; signals and generators continue.
    if await db_module.to_db_thread(is_trading_paused):
        _pause_until = db_module.get_app_config("trade_pause_until")
        try:
            _until_str = time.strftime("%H:%M", time.localtime(float(_pause_until)))
            _pause_msg = f"Trading paused until {_until_str} — MT5 order blocked."
        except Exception:
            _pause_msg = "Trading paused — MT5 order blocked."
        log.warning("[Pause] %s (signal=%s)", _pause_msg, signal_id[:8])
        raise ValueError(_pause_msg)

    # Global circuit breaker — blocks all live MT5 execution.
    _cb = db_module.get_circuit_breaker_state()
    if _cb["is_active"]:
        _cb_mins = int(_cb["remaining_secs"] // 60) + 1
        raise ValueError(
            f"Trade blocked — circuit breaker active. "
            f"Live trading resumes in approximately {_cb_mins} min."
        )

    # Max-open-trades gate — lives here (the function that actually inserts
    # the trade row) rather than in the callers, so it's checked against
    # whichever node's table is about to receive the INSERT.
    rs = await db_module.to_db_thread(db_module.get_risk_settings)
    with db_module.db() as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()[0]
    if open_count >= int(rs.get("max_open_trades", 1)):
        raise ValueError(f"Max open trades reached ({rs.get('max_open_trades', 1)})")

    # Use caller-supplied tick if available — callers on the critical path
    # (open_trade_from_signal, _process_instant_entry) already hold a fresh
    # tick so fetching again wastes one bridge round-trip.
    if tick is None:
        tick = await bridge.get_fresh_tick()
    if tick is None:
        raise RuntimeError("No live price available")

    if mt5_tp_override is not None:
        # Adaptive Runner ladder legs (see open_adaptive_runner_ladder):
        # each leg is its own MT5 position with its own genuine resting
        # broker-side TP, set at open time — not managed by Python
        # tick-polling like every other strategy's ladder.
        mt5_tp = mt5_tp_override
    elif strategy == STRATEGY_BE_RUNNER:
        mt5_tp = tp8 or tp7 or tp6 or tp5 or tp4 or tp3 or tp2 or tp1
    elif strategy == STRATEGY_FIXED_RR:
        # Fixed R:R's whole design is one broker stop and one broker
        # target -- nothing polls it, so MT5 still closes the trade
        # correctly if the app or the EA is down. tp1 carries the
        # computed target (see core_open_trade_from_signal, which
        # rewrites both levels to be exact from the real fill).
        mt5_tp = tp1
    else:
        mt5_tp = None

    # Hand off to the companion MQL5 EA when it's enabled, connected, and
    # this strategy's SL/TP/partial-close rules are ones it can run
    # natively on OnTick — see forex_trader.core.ea_bridge for why DPM
    # never qualifies. Falls straight through to the existing bridge
    # path (unchanged) if the EA is off, unreachable, or the strategy
    # isn't portable, so this is purely additive — no existing behaviour
    # changes when the EA isn't in the picture.
    trade_id = str(uuid.uuid4())[:16]
    now      = time.time()
    managed_by = "python"
    mt5_ticket = None
    entry_price = None
    _is_template = ea_templates.is_template_override(strategy)
    ea_rs = await db_module.to_db_thread(db_module.get_risk_settings)
    if bool(ea_rs.get("ea_bridge_enabled", 0)) and mt5_tp_override is None:
        try:
            from forex_trader.core import ea_bridge as _ea_mod
            _ea = _ea_mod.get_instance()
            # TEMP diagnostic — every adaptive_runner trade today has
            # gone Python-only despite ea_bridge_enabled=1, no [EA] log
            # line at all, cause not yet identified (2026-07-17).
            log.info(
                "[EA-diag] handoff check strategy=%s ea_instance=%s ea_healthy=%s portable=%s",
                strategy, _ea is not None,
                _ea.is_ea_healthy() if _ea is not None else None,
                _ea.is_strategy_portable(strategy) if _ea is not None else None,
            )
            if _ea is not None and _ea.is_ea_healthy() and _ea.is_strategy_portable(strategy):
                _tps = {n: v for n, v in enumerate(
                    [tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8], start=1
                ) if v is not None}
                _ea_pcts = None
                _ea_be_at_pos = None
                _ea_trail_mode = None
                _ea_template = None
                if strategy in _EA_LADDER_PCTS and _tps:
                    _ea_table = _EA_LADDER_PCTS[strategy]
                    _ea_pcts = _ea_table.get(len(_tps), _ea_table[max(_ea_table)])
                    _ea_be_at_pos = _EA_LADDER_BE_AT_POS[strategy]
                    _ea_trail_mode = _EA_LADDER_TRAIL_MODE.get(strategy)
                elif _is_template:
                    _ea_template = ea_templates.get_ea_template(
                        ea_templates.template_name_from_override(strategy)
                    )
                    if _ea_template is not None:
                        # Anchor TP (2026-07-29): the template's tp{n}_pips
                        # is now AUTHORITATIVE for every level -- entry ±
                        # N pips from the actual fill reference price,
                        # replacing the signal's own TP prices entirely
                        # rather than only filling gaps it left. Explicit
                        # user directive: an EA Template channel's targets
                        # come from the template, full stop, so the same
                        # channel behaves identically regardless of which
                        # provider's message shape triggered the trade.
                        # tp{n}_pct is unchanged -- it was always template-
                        # only, since a signal states TP prices but never
                        # how much to close at each one.
                        #
                        # A template with no pips configured at any level
                        # sends NO take-profit at all (an empty _tps),
                        # rather than silently reviving the discarded
                        # signal levels as an implicit fallback -- that
                        # would defeat the point of this being explicit and
                        # authoritative. tpsl_mode/mode still govern
                        # whether/how this ladder is used by the EA.
                        _sign = 1 if direction.upper() == "BUY" else -1
                        _ref_price = tick.ask if direction.upper() == "BUY" else tick.bid
                        _tps = {}
                        for n in range(1, 9):
                            _pips = float(_ea_template.get(f"tp{n}_pips", 0.0))
                            if _pips > 0:
                                _tps[n] = _ref_price + _sign * _pips
                        if _tps:
                            _tpl_pcts = [float(_ea_template.get(f"tp{n}_pct", 0.0))
                                         for n in sorted(_tps)]
                            if any(p > 0 for p in _tpl_pcts):
                                _ea_pcts = _tpl_pcts
                _ea_lot = lot_size
                if _ea_template is not None and _ea_template.get("mode") == "grid":
                    # Global Parameters > Fixed Lot Size (Grid) -- used for
                    # EACH leg the EA stages in HandleOpenTemplateGrid, in
                    # place of whatever lot_size normal (non-grid) sizing
                    # computed above. 0 (default) leaves lot_size untouched.
                    _grid_lot = float(ea_rs.get("strategy_lot_size_grid", 0))
                    if _grid_lot > 0:
                        _ea_lot = _grid_lot
                ea_ack = await _ea.open_trade(
                    trade_id, direction.upper(), _ea_lot, stop_loss, _tps, strategy,
                    pcts=_ea_pcts, be_at_pos=_ea_be_at_pos, trail_mode=_ea_trail_mode,
                    template=_ea_template,
                    # The signal's own entry zone -- grid-mode templates stage
                    # their legs across it instead of stepping away from
                    # current price. See EABridge.open_trade's zone_low/
                    # zone_high handling for why.
                    zone_low=entry_low, zone_high=entry_high,
                )
                if ea_ack.get("type") == "trade_opened":
                    mt5_ticket  = ea_ack.get("ticket")
                    entry_price = float(ea_ack.get("fill_price", 0))
                    managed_by  = "ea"
                    log.info("[EA] order placed: ticket=%s dir=%s lots=%s @ %s (strategy=%s)",
                             mt5_ticket, direction, _ea_lot, entry_price, strategy)
                elif _is_template:
                    raise RuntimeError(f"EA rejected template order: {ea_ack.get('error')}")
                else:
                    log.warning("[EA] open_trade rejected: %s — falling back to Python bridge",
                                ea_ack.get("error"))
            elif _is_template:
                raise RuntimeError(
                    "EA Template strategies require a connected, healthy EA — "
                    "none is available right now"
                )
        except Exception as _ea_e:
            if _is_template:
                raise
            log.warning("[EA] handoff failed (%s) — falling back to Python bridge", _ea_e)

    if _is_template and managed_by != "ea":
        # Should be unreachable (every path above either sets managed_by='ea'
        # or raises for a template strategy) -- fail loudly rather than let a
        # template trade silently fall through to the Python bridge path,
        # which has no idea how to run Grid/Stealth/Anchor/Trail management.
        raise RuntimeError("EA Template strategy resolved with no EA management — refusing to open")

    if managed_by != "ea":
        mt5_result  = await bridge.place_order(direction, lot_size, stop_loss, mt5_tp,
                                               f"sig:{signal_id[:8]}")
        mt5_error   = mt5_result.get("error")

        # Raise immediately — do NOT record a phantom trade if MT5 rejected the order
        if mt5_error:
            log.error("MT5 order failed: %s", mt5_error)
            raise RuntimeError(f"MT5 order rejected: {mt5_error}")

        mt5_ticket  = mt5_result.get("ticket")
        entry_price = float(mt5_result.get("fill_price") or
                            (tick.ask if direction.upper() == "BUY" else tick.bid))
        log.info("MT5 order placed: ticket=%s dir=%s lots=%s @ %s",
                 mt5_ticket, direction, lot_size, entry_price)

    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,tg_source,
                managed_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, mt5_ticket, direction.upper(), entry_low, entry_high, entry_price,
             lot_size, lot_size, stop_loss, tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8,
             "open", now,
             0.0, 0.0, 0.0, 0.0,
             strategy, tg_source, managed_by),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='active' WHERE signal_id=?", (signal_id,)
        )

    return {
        "trade_id":    trade_id,
        "mt5_ticket":  mt5_ticket,
        "entry_price": entry_price,
        "strategy":    strategy,
        "managed_by":  managed_by,
    }

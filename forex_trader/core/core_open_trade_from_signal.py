"""Open trade from signal (back half) -- extracted verbatim (no logic
changes) from the tail of core/engine.py's SimulationEngine.
open_trade_from_signal (atomic claim, the open_trade call, and the 6
strategy-specific POST-fill bridge.modify_order overrides), as part of the
core/engine.py migration series. See
docs/todo/refactor/core-open-trade-from-signal-migration/020-*.md.

Calls bridge.modify_order and ea_bridge.EABridge.update_trade -- real MT5
order-modification calls, unchanged from the original. This module places
no order and modifies no order itself; it only calls whatever `bridge`
(and whatever ea_bridge singleton is configured) its caller supplies.

Calls core_signal_resolution.resolve_open_trade_params (pack 12) then
core_open_trade.open_trade (pack 11). Reuses pack 12's _gdvr_sl_dist/
_adaptive_sl_dist/_adaptive_final_tp_dist and point-distance constants
rather than duplicating them a third time. The pre-fill entry-mid fallback
values the original computed as front-half locals (only used when
result["entry_price"] is falsy -- dead in practice since open_trade
always populates a real float) are recomputed fresh from sig["entry_low"]/
sig["entry_high"] here -- same formula, same signal row, behaviorally
identical. background_open_commentary is taken as an optional injected
async callable (default no-op), same pattern as pack 10's
schedule_profit_sync/background_close_commentary.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core.core_open_trade import open_trade
from forex_trader.core.core_signal_resolution import (
    resolve_open_trade_params,
    _gdvr_sl_dist, _adaptive_sl_dist, _adaptive_final_tp_dist,
    _CONSERVATIVE_SL_PT, _CONSERVATIVE_TP1_PT,
    _SCALP_RUNNER_SL_PT, _SCALP_RUNNER_TP1_PT, _SCALP_RUNNER_TP2_PT,
)
from forex_trader.core.models import (
    Tick,
    STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_GD_VIP_RUNNER, STRATEGY_ADAPTIVE_RUNNER, STRATEGY_TRAIL_STOP,
)

log = logging.getLogger(__name__)


async def open_trade_from_signal(
    bridge: Any,
    signal_id: str,
    lot_size_override: Optional[float] = None,
    tick: Optional[Tick] = None,
    age_lot_mult: float = 1.0,
    dpm_candles: Optional[list] = None,
    starting_balance: float = 1000.0,
    background_open_commentary: Optional[Callable[[str, dict, Tick], Awaitable[None]]] = None,
) -> dict:
    resolved = await resolve_open_trade_params(
        bridge, signal_id,
        lot_size_override=lot_size_override, tick=tick, age_lot_mult=age_lot_mult,
        dpm_candles=dpm_candles, starting_balance=starting_balance,
    )
    sig               = resolved["sig"]
    strategy          = resolved["strategy"]
    lot_size          = resolved["lot_size"]
    stop_loss_to_use  = resolved["stop_loss_to_use"]
    tick              = resolved["tick"]

    # ── Atomic claim ─────────────────────────────────────────────────────
    # A signal can be opened by two callers at once — the direct caller
    # (e.g. Signal Generator _execute_live) and the pending-signal watcher,
    # which both pass the read-only checks above during the async MT5 fill
    # window before any trade row exists. Claim the signal with a single
    # conditional UPDATE; SQLite's writer lock guarantees only one caller
    # flips 'pending'/'active' → 'activating'. The loser bails out here.
    with db_module.db() as conn:
        _claimed = conn.execute(
            "UPDATE vantage_signals SET status='activating' "
            "WHERE signal_id=? AND status IN ('pending','active')",
            (signal_id,),
        ).rowcount
    if not _claimed:
        raise ValueError(f"Signal {signal_id} is already being opened — duplicate suppressed")

    try:
        result = await open_trade(
            bridge,
            signal_id=signal_id,
            direction=sig["direction"],
            entry_low=float(sig["entry_low"]),
            entry_high=float(sig["entry_high"]),
            stop_loss=stop_loss_to_use,
            tp1=sig.get("tp1"), tp2=sig.get("tp2"), tp3=sig.get("tp3"),
            tp4=sig.get("tp4"), tp5=sig.get("tp5"),
            tp6=sig.get("tp6"), tp7=sig.get("tp7"), tp8=sig.get("tp8"),
            lot_size=lot_size, tick=tick, strategy=strategy,
            tg_source=sig.get("source_name") or None,
        )
    except Exception:
        # Restore so a transient failure (e.g. MT5 reject) stays retryable.
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_signals SET status='pending' "
                "WHERE signal_id=? AND status='activating'",
                (signal_id,),
            )
        raise

    _entry_mid = (float(sig["entry_low"]) + float(sig["entry_high"])) / 2
    _sign = 1.0 if sig["direction"].upper() == "BUY" else -1.0

    # ── Conservative: post-fill SL/TP override ──────────────────────────
    # Set exact SL (fill ∓ 5) and TP1 (fill ± 3); clear all signal TPs.
    if strategy == STRATEGY_CONSERVATIVE and result.get("trade_id"):
        _fill      = float(result.get("entry_price", _entry_mid))
        exact_sl   = round(_fill - _sign * _CONSERVATIVE_SL_PT, 2)
        exact_tp1  = round(_fill + _sign * _CONSERVATIVE_TP1_PT, 2)
        with db_module.db() as conn:
            conn.execute(
                """UPDATE vantage_simulated_trades
                   SET stop_loss=?, tp1=?, tp2=NULL, tp3=NULL, tp4=NULL, tp5=NULL,
                       tp6=NULL, tp7=NULL, tp8=NULL
                   WHERE trade_id=?""",
                (exact_sl, exact_tp1, result["trade_id"]),
            )
        mt5_tkt = result.get("mt5_ticket")
        if mt5_tkt:
            try:
                await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
            except Exception as _e:
                log.warning("[%s] modify_order SL sync failed: %s", strategy, _e)
        result["stop_loss"] = exact_sl
        result["tp1"] = exact_tp1
        log.info(
            "[%s] trade_id=%s fill=%.2f SL=%.2f(-%.0fpt) TP1=%.2f(+%.0fpt)",
            strategy,
            result["trade_id"][:8], _fill, exact_sl, _CONSERVATIVE_SL_PT, exact_tp1, _CONSERVATIVE_TP1_PT,
        )
        if result.get("managed_by") == "ea":
            try:
                from forex_trader.core import ea_bridge as _ea_mod
                _ea = _ea_mod.get_instance()
                if _ea is not None:
                    await _ea.update_trade(result["trade_id"], {1: exact_tp1})
            except Exception as _e:
                log.warning("EA update_trade after %s fill failed: %s", strategy, _e)

    # ── Scalp Runner: post-fill SL/TP1/TP2 override ─────────────────────
    # Set exact SL (fill ∓ 10), TP1 (fill ± 3, closes 50%) and TP2
    # (fill ± 4, moves SL to entry + starts the trail); clear tp3-8.
    elif strategy == STRATEGY_SCALP_RUNNER and result.get("trade_id"):
        _fill     = float(result.get("entry_price", _entry_mid))
        exact_sl  = round(_fill - _sign * _SCALP_RUNNER_SL_PT, 2)
        exact_tp1 = round(_fill + _sign * _SCALP_RUNNER_TP1_PT, 2)
        exact_tp2 = round(_fill + _sign * _SCALP_RUNNER_TP2_PT, 2)
        with db_module.db() as conn:
            conn.execute(
                """UPDATE vantage_simulated_trades
                   SET stop_loss=?, tp1=?, tp2=?, tp3=NULL, tp4=NULL, tp5=NULL,
                       tp6=NULL, tp7=NULL, tp8=NULL
                   WHERE trade_id=?""",
                (exact_sl, exact_tp1, exact_tp2, result["trade_id"]),
            )
        mt5_tkt = result.get("mt5_ticket")
        if mt5_tkt:
            try:
                await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
            except Exception as _e:
                log.warning("[%s] modify_order SL sync failed: %s", strategy, _e)
        result["stop_loss"] = exact_sl
        result["tp1"] = exact_tp1
        result["tp2"] = exact_tp2
        log.info(
            "[%s] trade_id=%s fill=%.2f SL=%.2f(-%.0fpt) TP1=%.2f(+%.0fpt) TP2=%.2f(+%.0fpt)",
            strategy,
            result["trade_id"][:8], _fill, exact_sl, _SCALP_RUNNER_SL_PT, exact_tp1,
            _SCALP_RUNNER_TP1_PT, exact_tp2, _SCALP_RUNNER_TP2_PT,
        )
        if result.get("managed_by") == "ea":
            try:
                from forex_trader.core import ea_bridge as _ea_mod
                _ea = _ea_mod.get_instance()
                if _ea is not None:
                    await _ea.update_trade(result["trade_id"], {1: exact_tp1, 2: exact_tp2})
            except Exception as _e:
                log.warning("EA update_trade after %s fill failed: %s", strategy, _e)

    # ── Conservative Trial: post-fill SL/TP override ─────────────────────
    # Recalculate exact SL and all 6 TPs from the actual fill price, then
    # update the DB row and sync the MT5 SL via modify_order.
    elif strategy == STRATEGY_CONSERVATIVE_TRIAL and result.get("trade_id"):
        _fill      = float(result.get("entry_price", _entry_mid))
        _ct_sl_pts = round(100.0 / (lot_size * 100.0), 1)
        exact_sl   = round(_fill - _sign * _ct_sl_pts, 2)
        # TP offsets (pts from fill): 5, 10, 14, 20, 27, 35
        ct_tps = [round(_fill + _sign * off, 2) for off in (5.0, 10.0, 14.0, 20.0, 27.0, 35.0)]
        with db_module.db() as conn:
            conn.execute(
                """UPDATE vantage_simulated_trades
                   SET stop_loss=?, tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?,
                       tp7=NULL, tp8=NULL
                   WHERE trade_id=?""",
                (exact_sl,
                 ct_tps[0], ct_tps[1], ct_tps[2], ct_tps[3], ct_tps[4], ct_tps[5],
                 result["trade_id"]),
            )
        mt5_tkt = result.get("mt5_ticket")
        if mt5_tkt:
            try:
                await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
            except Exception as _e:
                log.warning("[conservative_trial] modify_order SL sync failed: %s", _e)
        result["stop_loss"] = exact_sl
        result.update({f"tp{i+1}": ct_tps[i] for i in range(6)})
        log.info(
            "[conservative_trial] trade_id=%s fill=%.2f SL=%.2f TPs=%s",
            result["trade_id"][:8], _fill, exact_sl,
            "/".join(f"{v:.2f}" for v in ct_tps),
        )

    # ── GD VIP Runner: post-fill exact SL override (TPs untouched) ───────
    # Recompute the widened SL from the actual fill price (slippage
    # correction); the signal's own TP ladder is left exactly as opened.
    elif strategy == STRATEGY_GD_VIP_RUNNER and result.get("trade_id"):
        _fill      = float(result.get("entry_price", _entry_mid))
        _gv_sl_pt2 = _gdvr_sl_dist(abs(_entry_mid - float(sig["stop_loss"])))
        exact_sl   = round(_fill - _sign * _gv_sl_pt2, 2)
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                (exact_sl, result["trade_id"]),
            )
        mt5_tkt = result.get("mt5_ticket")
        if mt5_tkt:
            try:
                await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
            except Exception as _e:
                log.warning("[gd_vip_runner] modify_order SL sync failed: %s", _e)
        result["stop_loss"] = exact_sl
        log.info(
            "[gd_vip_runner] trade_id=%s fill=%.2f SL=%.2f(-%.1fpt, stated=%.1fpt)",
            result["trade_id"][:8], _fill, exact_sl, _gv_sl_pt2,
            abs(_entry_mid - float(sig["stop_loss"])),
        )

    # ── Adaptive Runner: post-fill exact SL override (TPs untouched) ─────
    # Recompute the widened SL from the actual fill price (slippage
    # correction); the signal's own TP ladder is left exactly as opened.
    elif strategy == STRATEGY_ADAPTIVE_RUNNER and result.get("trade_id"):
        _fill              = float(result.get("entry_price", _entry_mid))
        _ar_final_tp_dist2 = _adaptive_final_tp_dist(sig, _fill, _sign > 0)
        _ar_sl_pt2         = _adaptive_sl_dist(
            abs(_entry_mid - float(sig["stop_loss"])), _ar_final_tp_dist2,
        )
        exact_sl = round(_fill - _sign * _ar_sl_pt2, 2)
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                (exact_sl, result["trade_id"]),
            )
        mt5_tkt = result.get("mt5_ticket")
        if mt5_tkt:
            try:
                await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
            except Exception as _e:
                log.warning("[adaptive_runner] modify_order SL sync failed: %s", _e)
        result["stop_loss"] = exact_sl
        log.info(
            "[adaptive_runner] trade_id=%s fill=%.2f SL=%.2f(-%.1fpt, stated=%.1fpt)",
            result["trade_id"][:8], _fill, exact_sl, _ar_sl_pt2,
            abs(_entry_mid - float(sig["stop_loss"])),
        )

    # ── Trail Stop: post-fill SL + TP1-TP8 override ─────────────────────
    # SL = fill ∓ trail_stop_sl_pts; TP1-TP8 = fill ± (n × trail_dist).
    # TP1 is the activation trigger for the trailing SL; TP2-TP8 are markers.
    elif strategy == STRATEGY_TRAIL_STOP and result.get("trade_id"):
        rs          = db_module.get_risk_settings()
        _trail_dist = float(rs.get("trailing_stop_distance", 3.0))
        _ts_sl_pts  = float(rs.get("trail_stop_sl_pts", 5.0))
        _fill       = float(result.get("entry_price", _entry_mid))
        exact_sl    = round(_fill - _sign * _ts_sl_pts, 2)
        ts_tps      = [round(_fill + _sign * (n * _trail_dist), 2) for n in range(1, 9)]
        with db_module.db() as conn:
            conn.execute(
                """UPDATE vantage_simulated_trades
                   SET stop_loss=?, tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?, tp7=?, tp8=?
                   WHERE trade_id=?""",
                (exact_sl,
                 ts_tps[0], ts_tps[1], ts_tps[2], ts_tps[3],
                 ts_tps[4], ts_tps[5], ts_tps[6], ts_tps[7],
                 result["trade_id"]),
            )
        mt5_tkt = result.get("mt5_ticket")
        if mt5_tkt:
            try:
                await bridge.modify_order(int(mt5_tkt), sl=exact_sl, tp=None)
            except Exception as _e:
                log.warning("[trail_stop] modify_order SL sync failed: %s", _e)
        result["stop_loss"] = exact_sl
        result.update({f"tp{i+1}": ts_tps[i] for i in range(8)})
        log.info(
            "[trail_stop] trade=%s %s fill=%.2f SL=%.2f(-%s pts) "
            "trail=%.1f pts  TP1=%.2f .. TP8=%.2f",
            result["trade_id"][:8], sig["direction"], _fill, exact_sl,
            _ts_sl_pts, _trail_dist, ts_tps[0], ts_tps[7],
        )

    if not result.get("executed_remotely") and background_open_commentary:
        asyncio.create_task(background_open_commentary(result["trade_id"], sig, tick))
    # else: the VPS actually placed this trade (centralized signal
    # generation forwarded it there) — result["trade_id"] refers to a row
    # in the VPS's own DB, not this node's, so background_open_commentary
    # would just fail to find it. _handle_signal_order on the VPS side
    # schedules the equivalent commentary/notification itself once it has
    # placed the trade, since it's the node that actually holds that row.
    return result

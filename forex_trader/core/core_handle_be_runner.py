"""BE Runner TP/SL strategy handler -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._handle_be_runner, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-be-runner-handler-migration/020-*.md.

Calls bridge.modify_order -- a real MT5 order-modification call, unchanged
from the original. This module modifies no order itself; it only calls
whatever `bridge` its caller supplies.

Takes `bridge`, `dpm_candles`, and pack 17's handle_scale_out collaborator
set (`tp_cache`/`scale_out_last_fail`/`close_full_after_tps`) explicitly
-- the ADX-ranging fallback calls core_handle_scale_out.handle_scale_out
directly with all of them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import dpm_engine
from forex_trader.core import telegram_alerts
from forex_trader.core.core_handle_scale_out import handle_scale_out
from backend.src.services.risk.strategy_params import get_strategy_params
from forex_trader.core.core_tp_trigger_tracking import TPCache
from backend.src.utils.models import MAX_TP, STRATEGY_BE_RUNNER, Tick

log = logging.getLogger(__name__)


async def handle_be_runner(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    scale_out_last_fail: dict,
    dpm_candles: Optional[list] = None,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    # ADX gate: BE Runner only in trending markets — fall back to Scale Out when ranging.
    if dpm_candles:
        _adx_threshold = get_strategy_params(STRATEGY_BE_RUNNER)["adx_ranging_threshold"]
        _adx = dpm_engine.compute_adx(dpm_candles)
        if _adx < _adx_threshold:
            log.debug("[be_runner] trade=%s ADX %.1f < %.1f (ranging) — falling back to scale_out",
                      trade["trade_id"][:8], _adx, _adx_threshold)
            await handle_scale_out(
                trade, tick, bridge, tp_cache, scale_out_last_fail,
                close_full_after_tps=close_full_after_tps,
            )
            return

    direction   = trade["direction"].upper()
    entry_price = float(trade["entry_price"])
    current_sl  = float(trade["stop_loss"])
    mt5_ticket  = trade.get("mt5_ticket")
    trade_id    = trade["trade_id"]
    tp_vals: list[tuple[int, float]] = [
        (i, float(trade.get(f"tp{i}"))) for i in range(1, MAX_TP + 1) if trade.get(f"tp{i}") is not None
    ]
    if not tp_vals:
        return
    sl_ladder = [entry_price] + [v for _, v in tp_vals]
    best_step = 0
    for step_idx, (tp_num, tp_val) in enumerate(tp_vals, 1):
        cleared = (direction == "BUY" and tick.bid >= tp_val) or \
                  (direction == "SELL" and tick.ask <= tp_val)
        if cleared:
            best_step = step_idx
        else:
            break
    if best_step == 0:
        return
    target_sl = sl_ladder[best_step - 1]
    should_update = (direction == "BUY" and target_sl > current_sl) or \
                    (direction == "SELL" and target_sl < current_sl)
    if not should_update:
        return
    if mt5_ticket:
        await bridge.modify_order(int(mt5_ticket), sl=target_sl, tp=None)
    trigger_tp_num = tp_vals[best_step - 1][0]
    def _apply():
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
                (target_sl, trade_id),
            )
            conn.execute(
                "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
                " VALUES (?,?,?,?,?,?)",
                (trade_id, time.time(), 0.0, target_sl, 0.0, f"TP{best_step}_SL_LOCKED"),
            )
    await db_module.to_db_thread(_apply)
    asyncio.create_task(telegram_alerts.send_message(
        telegram_alerts.fmt_sl_moved(trade, trigger_tp_num, target_sl),
        trade_id, "sl_moved",
    ))

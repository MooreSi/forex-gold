"""Scale Out TP/SL strategy handler -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._handle_scale_out, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-scale-out-handler-migration/020-*.md.

Calls bridge.partial_close/bridge.modify_order -- real MT5 order-close/
modify calls, unchanged from the original. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

Takes `bridge`, a TPCache (pack 5), and `scale_out_last_fail: dict` (the
per-(trade_id, tp_num) retry-cooldown tracker -- externally owned, same
treatment as pack 10's CloseTradeContext dicts) explicitly. Reuses
core_tp_trigger_tracking.check_tp_hits/get_remaining_lots (pack 5),
core_partial_close.partial_close_trade (pack 9). close_full_after_tps
(fired via asyncio.create_task, fire-and-forget in the original when a
partial close empties the position) is taken as an optional injected
async callable, default no-op -- it's a large, separate piece of work
(residual-position verification, profit sync, a full Telegram close
notification) deserving its own future pack, same pattern as pack 10's
schedule_profit_sync/background_close_commentary.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_partial_close import partial_close_trade
from forex_trader.core.core_tp_trigger_tracking import TPCache, check_tp_hits, get_remaining_lots
from forex_trader.core.models import MAX_TP, Tick

log = logging.getLogger(__name__)

# Per-TP close percentages: TP1 banks the most, then tapers. TP5+ (index not
# in dict) closes all remaining lots.
_SCALE_OUT_PCTS = {1: 0.40, 2: 0.30, 3: 0.20, 4: 0.10}
_SCALE_OUT_RETRY_COOLDOWN_S = 30  # min seconds between partial-close retries after a failure


async def handle_scale_out(
    trade: dict,
    tick: Tick,
    bridge: Any,
    tp_cache: TPCache,
    scale_out_last_fail: dict,
    close_full_after_tps: Optional[Callable[[str, Optional[int], float], Awaitable[None]]] = None,
) -> None:
    tp_hits = await check_tp_hits(tp_cache, trade, tick)
    # Determine the last defined TP so it always closes all remaining lots
    last_tp_num = max(
        (n for n in range(1, MAX_TP + 1) if trade.get(f"tp{n}") is not None),
        default=None,
    )
    for (trade_id, tp_num) in tp_hits:
        tp_price  = trade.get(f"tp{tp_num}")
        if tp_price is None:
            continue
        remaining = await db_module.to_db_thread(get_remaining_lots, trade_id)
        if remaining <= 0:
            continue
        # Tiered close schedule: 40/30/20/10 — last TP always closes all remaining
        if tp_num == last_tp_num:
            lots_to_close = remaining
        else:
            pct = _SCALE_OUT_PCTS.get(tp_num, 0.0)
            if pct <= 0:
                lots_to_close = remaining  # TP5+ with remaining lots
            else:
                lots_to_close = round(float(trade["lot_size"]) * pct, 4)
                lots_to_close = min(lots_to_close, remaining)
        if lots_to_close <= 0:
            continue
        fail_key   = (trade_id, tp_num)
        last_fail  = scale_out_last_fail.get(fail_key, 0.0)
        if time.time() - last_fail < _SCALE_OUT_RETRY_COOLDOWN_S:
            continue
        actual_price = float(tp_price)
        mt5_ticket   = trade.get("mt5_ticket")
        if mt5_ticket:
            mt5_res = await bridge.partial_close(int(mt5_ticket), lots_to_close)
            if mt5_res.get("success"):
                actual_price = float(mt5_res.get("close_price", tp_price))
                lots_to_close = float(mt5_res.get("lots_closed", lots_to_close))
                scale_out_last_fail.pop(fail_key, None)
            elif mt5_res.get("error") or mt5_res.get("success") is False:
                scale_out_last_fail[fail_key] = time.time()
                log.warning("[scale_out] MT5 partial close failed ticket=%s tp=%d: %s "
                            "(will retry in %ds)",
                            mt5_ticket, tp_num, mt5_res, _SCALE_OUT_RETRY_COOLDOWN_S)
                continue
        try:
            res = await partial_close_trade(trade_id, lots_to_close, actual_price, f"TP{tp_num}")
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, actual_price, lots_to_close,
                                           res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
            if res.get("auto_closed") and mt5_ticket and close_full_after_tps:
                asyncio.create_task(close_full_after_tps(trade_id, mt5_ticket, actual_price))
        except Exception as exc:
            log.warning("[scale_out] Partial close failed: %s", exc)
            continue
        if tp_num == 1 and not trade.get("sl_moved_to_be"):
            entry_price = float(trade["entry_price"])
            if mt5_ticket:
                await bridge.modify_order(int(mt5_ticket), sl=entry_price, tp=None)
            def _apply_scale_out_be():
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
                        (entry_price, trade_id),
                    )
            await db_module.to_db_thread(_apply_scale_out_be)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_sl_moved(trade, 1, entry_price),
                trade_id, "sl_moved_be",
            ))

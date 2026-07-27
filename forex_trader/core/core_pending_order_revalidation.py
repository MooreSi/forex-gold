"""Periodic re-validation of resting broker-side pending limit orders
(vantage_pending_orders, status='working') -- Telegram Limit Runner, manual
limit orders, and Reversal Engine's limit-order toggle all place a genuine
MT5 BuyLimit/SellLimit and then just wait for either a fill or expiry.

Unlike a market-order signal (core/momentum_exhaustion.py's other caller,
core_signal_resolution.py/breakout_signal_live_execute.py/
reversal_engine_live_execute.py), a resting pending order has no fill-time
gate at all -- this EA doesn't use OnTradeTransaction (every lifecycle event
is polled, see ForexTraderBridge.mq5's CheckPendingOrders), so MT5's own
matching engine fills it directly the moment price touches, with no
round-trip back to Python. A fixed expiry alone doesn't cover the case the
setup goes bad well before that expiry -- this periodically re-checks each
resting order against the same momentum/exhaustion re-check used at fill
time, and cancels the order at the broker if conditions have invalidated it,
rather than leaving it to either fill blind or wait out its full TTL.

EA Template grid legs are deliberately out of scope here: they never get
their own vantage_pending_orders row (each leg lives only in the EA's own
g_pending[], see core_ea_templates.py's module docstring and
ea_bridge.py's _promote_grid_leg_fill) -- Python has no per-leg price/ticket
visibility to re-validate individually without a larger protocol change.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from forex_trader.core import database as db_module
from forex_trader.core.momentum_exhaustion import check_momentum_exhaustion

log = logging.getLogger(__name__)

# Grace period before a freshly-placed order is eligible for re-check --
# avoids reacting to the same momentary noise the order was placed against.
_REVALIDATE_GRACE_S = 300


async def revalidate_pending_orders(bridge: Any) -> None:
    from forex_trader.core import ea_bridge as ea_bridge_mod
    ea = ea_bridge_mod.get_instance()
    if ea is None or not ea.is_ea_healthy():
        return

    def _fetch_working():
        with db_module.db() as conn:
            return [db_module.row_to_dict(r) for r in conn.execute(
                "SELECT * FROM vantage_pending_orders WHERE status='working'"
            ).fetchall()]
    orders = await db_module.to_db_thread(_fetch_working)
    if not orders:
        return

    candles = await bridge.get_candles("M5", 80)
    if not candles:
        return
    from forex_trader.core.dpm_engine import compute_atr
    atr = compute_atr(candles[-20:], period=14)
    if atr <= 0:
        return

    now = time.time()
    for order in orders:
        created = float(order.get("created_at", 0) or 0)
        if now - created < _REVALIDATE_GRACE_S:
            continue
        ticket = order.get("ea_ticket")
        if not ticket:
            continue
        direction = (order.get("direction") or "").upper()
        ok, reason = check_momentum_exhaustion(direction, candles, atr)
        if ok:
            continue
        log.info(
            "[PendingRevalidate] cancelling %s %s @ %s -- %s",
            order.get("trade_id"), direction, order.get("price"), reason,
        )
        try:
            await ea.cancel_pending_order(
                order["trade_id"], int(ticket), f"revalidation: {reason}",
            )
        except Exception as e:
            log.warning("[PendingRevalidate] cancel failed for %s: %s", order.get("trade_id"), e)

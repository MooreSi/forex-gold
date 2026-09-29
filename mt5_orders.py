"""The orders MetaTrader still holds for one symbol. Read-only.

Standard library only, like mt5_terminal.py: mt5_bridge.py imports this, and
on macOS the bridge runs under Wine's Python, which has none of the app's
dependencies. The in-process bridge (backend/src/services/broker/mt5_native.py)
calls it with the same module handles.

Why it exists (bug 070, 2026-09-29): an order MT5 has sent and not heard back
about -- "started" in the Trade tab, volume "0.02 / 0" -- is neither a position
nor a deal. Before this, positions and deals were all the app could read, so
such an order was "no trace", and a placeholder waiting for it was written off
as never filled while it could still fill.

The state is passed through as MT5's number rather than interpreted. Every
caller asks one question -- "does MT5 still hold an order for this trade?" --
and a resting limit leg and a market order awaiting an answer both mean yes.
"""
from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("mt5_bridge")


def read(mt5: Any, symbol: str, ensure_connected: Callable[[], bool]) -> Optional[list]:
    """Every order the terminal lists for `symbol`, or None if it cannot say.

    None, never [], on a disconnected terminal or an API error: MetaTrader5's
    orders_get returns None on an error and an empty tuple for "no orders",
    and a caller that took the first for the second would write off a trade
    whose order is still on its way.
    """
    if not ensure_connected():
        return None
    try:
        orders = mt5.orders_get(symbol=symbol)
    except Exception as e:
        log.warning("get_orders error: %s", e)
        return None
    if orders is None:
        return None
    return [{
        "ticket":         o.ticket,
        "type":           o.type,
        "state":          o.state,
        "comment":        o.comment,
        "volume":         o.volume_initial,
        "volume_current": o.volume_current,
        "price_open":     o.price_open,
        "sl":             o.sl,
        "tp":             o.tp,
        "setup_time":     o.time_setup,
    } for o in orders]

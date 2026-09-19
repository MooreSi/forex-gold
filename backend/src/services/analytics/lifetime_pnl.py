"""Equity minus what was actually paid in: the account's whole-life P&L.

The only number on screen that answers "am I up or down overall". Everything
else is windowed: `total_net_pnl` on the Analysis tab is the P&L of closed
trades over N days, and open positions are not in it at all. This is equity --
which already includes floating P&L, swap and commission -- against the money
the owner actually deposited.

Deposits come from MT5's balance operations (deal `type` 2). A positive one is
money paid in, a negative one is money taken out, and the net of the two is
what "paid in" means. Summing the absolute values instead would make the
figure fall every time a profit was withdrawn.

**Cached, because the header polls every five seconds.** Ten years of deal
history fetched on every poll is bugs/030 again -- three panels each calling
the bridge on their own timer produced 388 round-trips in 25 seconds. Only the
deposit total is cached: equity is passed in by the caller on every read,
because a frozen P&L on a moving account is worse than no P&L.

A failure is never cached. "Unknown" held for five minutes would hide a bridge
that came back thirty seconds later.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

log = logging.getLogger(__name__)

# Only the figure itself. `net_deposited` is the intermediate step and
# `reset_cache` is a seam -- both are reachable, neither is the module's
# purpose, and a declared surface is a statement of what a module is FOR.
__all__ = ["since_inception"]

# MT5 deal type 2 is a balance operation: deposit, withdrawal, credit,
# correction. Types 0 and 1 are the buy and sell deals of real trades.
_BALANCE_DEAL = 2

# The whole account's life. A 30-day window would miss the opening deposit on
# any account older than a month, which is every account this matters for.
_ALL_HISTORY_DAYS = 3650

_CACHE_SECS = 300.0

_cached: Optional[float] = None
_cached_at: float = 0.0


def reset_cache() -> None:
    """Forget the deposit total. A test seam, and the switch path's hook:
    pointing the app at another account makes the previous total meaningless."""
    global _cached, _cached_at
    _cached = None
    _cached_at = 0.0


async def net_deposited(engine: Any) -> Optional[float]:
    """Money paid in minus money taken out, or None if it cannot be read."""
    global _cached, _cached_at

    now = time.monotonic()
    if _cached is not None and (now - _cached_at) < _CACHE_SECS:
        return _cached

    try:
        deals = await engine.get_deal_history(_ALL_HISTORY_DAYS)
    except Exception as exc:
        log.debug("[lifetime_pnl] deal history unavailable: %s", exc)
        return None
    if not deals:
        return None

    total = 0.0
    seen = False
    for deal in deals:
        if int(deal.get("type", -1)) != _BALANCE_DEAL:
            continue
        # Signed on purpose. A withdrawal is negative and belongs in the net.
        total += float(deal.get("profit", 0) or 0.0)
        seen = True

    if not seen:
        return None

    _cached, _cached_at = total, now
    return total


async def since_inception(engine: Any, equity: float) -> Optional[float]:
    """`equity - net_deposited`, or None when the deposits are not known.

    None rather than a zero or a bare equity figure: an account whose deposit
    history cannot be read would otherwise show its entire balance as profit,
    which is the most flattering possible wrong answer.
    """
    deposited = await net_deposited(engine)
    if not deposited:
        return None
    return float(equity) - deposited

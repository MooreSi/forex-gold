"""The EA opens nothing on a node that is not the active trader.

`open_trade()` has always refused on a stood-down node, but two routes reach
the EA without passing through it: the Limit Runner (place_pending_order) and
its Entry Realignment fallback (EA open_trade). On 2026-09-28 the stood-down
Mac placed a real BUY LIMIT on the shared demo account that way (tg_id=31099,
ticket 2104195879), which filled and ran to TP with neither node tracking it.

So the check sits here, on the two EA calls that create exposure, rather than
on each caller -- docs/system/rules/20-trading-safety.md, "Gate the funnel,
not the callers". Closing, modifying and restoring are not gated: those act
on exposure that already exists and must keep working on either node.
"""
from __future__ import annotations

import logging

log = logging.getLogger("ea_bridge")


class StoodDownError(ValueError):
    """Raised instead of sending. Its message carries "stood down", the
    phrase every caller already treats as an expected deferral."""


def refuse_unless_active_trader() -> None:
    """Raise StoodDownError when this node must not open anything.

    Fails open, as node_roles documents: an unpaired install has nothing to
    conflict with, and an error in the check must not silently stop a
    standalone node trading.
    """
    try:
        from backend.src.services.cluster import node_roles
        active = node_roles.is_active_trader_node()
    except Exception as e:
        log.debug("[EABridge] trader-role check failed, allowing: %s", e)
        return
    if not active:
        raise StoodDownError(
            "Trading stood down — the other node is the active trader "
            "(see Settings > Remote Node)."
        )

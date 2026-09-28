"""How many positions the broker holds, for the admin console's heartbeat.

The console's "N trades open" used to be the count of status='open' rows in
this machine's database. That is what the app believes, and it can be wrong
for up to a day: an EA Template placeholder that never filled stays an open
row (ticket 0) until the no-fill expiry writes it off. Found live 2026-09-28,
"2 trades open" on a VPS whose MT5 had no positions at all. The console looks
at a machine from outside, so it reports the broker's own list instead.

Read only. Nothing here can place, close or modify anything.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

# The heartbeat waits on this, and a stalled read must not hold it up.
_READ_TIMEOUT_S = 5.0


def _engine() -> Any:
    from backend.src.app import get_engine
    return get_engine()


async def broker_position_count(timeout_s: float = _READ_TIMEOUT_S) -> Optional[int]:
    """Positions the broker reports, or None when it cannot be read.

    None is "unknown", never 0: saying "0 open" about a machine that has
    live positions is worse than saying we cannot tell. An empty list is
    only believed when the bridge also says it is connected -- the same
    ambiguity position_sync guards against before it closes anything.
    """
    try:
        engine = _engine()
        bridge = getattr(engine, "_bridge", None) if engine is not None else None
        if bridge is None:
            return None
        positions = await asyncio.wait_for(bridge.get_positions(), timeout_s)
        if positions is None:
            return None
        if not positions:
            health = await asyncio.wait_for(bridge.get_health(), timeout_s)
            if not health.get("connected", False):
                return None
        return len(positions)
    except Exception as e:
        log.debug("[RemoteClient] broker position count unavailable: %s", e)
        return None

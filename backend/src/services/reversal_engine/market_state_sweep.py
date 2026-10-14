"""The Reversal engine's market-state sweep, on the research loop's minute
timer. docs/todo/017 item 4; the work is `market/market_state_sweep`.

The bridge is the running engine's, found the way `xasset_sweep` finds it.
Only candle reads. Nothing here places, closes or modifies a trade.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backend.src.services.market.market_state_sweep import sweep_one_day
from backend.src.services.reversal_engine import market_state_repo as repo

_skip_days: set[str] = set()
_UNSET = object()


def _engine_bridge() -> Optional[Any]:
    from backend.src.services.reversal_engine.study_schedule import _bridge_of_running_engine
    return _bridge_of_running_engine()


async def market_state_sweep(engine: Any, bridge: Any = _UNSET,
                             bridge_getter: Optional[Callable[[], Any]] = None) -> int:
    """One pass: the newest unmeasured day of Reversal signals, if any."""
    if bridge is _UNSET:
        bridge = (bridge_getter or _engine_bridge)()
    return await sweep_one_day(bridge, repo.signals_missing_state, repo.store_state,
                               _skip_days, "RE-State")

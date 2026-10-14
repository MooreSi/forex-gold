"""The Breakout engine's market-state sweep, run from the research loop's
minute timer beside the Reversal one (the breakout excursion sweep already
runs there). docs/todo/017 item 4; the work is `market/market_state_sweep`.

The bridge is the running Breakout engine's, found the way
`excursion_backfill.run` finds it, so this engine's database is written only
by this engine's code. Only candle reads. Nothing here places, closes or
modifies a trade.
"""
from __future__ import annotations

from typing import Any, Callable, Optional

from backend.src.services.breakout_signal import market_state_repo as repo
from backend.src.services.market.market_state_sweep import sweep_one_day

_skip_days: set[str] = set()
_UNSET = object()


def _engine_bridge() -> Optional[Any]:
    try:
        from backend.src.services.breakout_signal import breakout_signal_service as _svc
        engine = _svc.get_instance()
        return getattr(engine, "_bridge", None) if engine else None
    except Exception:                             # noqa: BLE001
        return None


async def market_state_sweep(engine: Any, bridge: Any = _UNSET,
                             bridge_getter: Optional[Callable[[], Any]] = None) -> int:
    """One pass: the newest unmeasured day of Breakout signals, if any."""
    if bridge is _UNSET:
        bridge = (bridge_getter or _engine_bridge)()
    return await sweep_one_day(bridge, repo.signals_missing_state, repo.store_state,
                               _skip_days, "BO-State")

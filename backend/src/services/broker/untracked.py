"""Untracked MT5 position detection -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine.get_untracked_mt5_positions, as part
of the core/engine.py migration series. See
docs/todo/refactor/core-untracked-positions-migration/020-*.md.

Takes `bridge` as an explicit parameter (anything exposing sync
is_configured() -> bool and async get_positions() -> list[dict], matching
MT5BridgeClient's real shape) instead of reading self._bridge. Calls
core_trade_reporting.get_open_trades() (pack 3) directly instead of
self.get_open_trades().
"""
from __future__ import annotations

from typing import Any

from backend.src.services.analytics import reporting as core_trade_reporting


async def get_untracked_mt5_positions(bridge: Any) -> list[dict]:
    """Return live MT5 positions that the app has no open trade record for.
    These are positions opened directly in MT5 (not via the app's signal system).
    Each dict is the raw bridge position payload plus a `_untracked=True` marker."""
    if not bridge.is_configured():
        return []
    try:
        live = await bridge.get_positions() or []
    except Exception:
        return []
    if not live:
        return []
    tracked_tickets = {
        int(t["mt5_ticket"])
        for t in core_trade_reporting.get_open_trades()
        if t.get("mt5_ticket")
    }
    return [dict(p, _untracked=True) for p in live if int(p["ticket"]) not in tracked_tickets]

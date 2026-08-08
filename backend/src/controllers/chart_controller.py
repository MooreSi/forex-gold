"""Chart page's API."""
from __future__ import annotations

from typing import Any

from backend.src.services.cluster import node as _node
from backend.src.services.risk import settings as _risk

__all__ = ["get_active_trader", "get_risk_settings", "get_open_trades"]


def get_active_trader() -> str:
    return _node.get_active_trader()


def get_risk_settings() -> dict:
    return _risk.get()


async def get_open_trades(engine: Any) -> list[dict]:
    """The page polls this from a ui.timer -- the engine read is dispatched
    off the loop by the positions service."""
    from backend.src.services.trading import engine_reads as _reads
    return await _reads.open_trades(engine)

"""Chart page's API (M3 page drain)."""
from __future__ import annotations

from typing import Any

from backend.src.db import database as db_module


def get_active_trader() -> str:
    return db_module.get_active_trader()


def get_risk_settings() -> dict:
    return db_module.get_risk_settings()


async def get_open_trades(engine: Any) -> list[dict]:
    """The page polls this from a ui.timer -- keep the read off the loop."""
    return await db_module.to_db_thread(engine.get_open_trades)

"""Off-loop reads of the runtime's own trade/signal tables.

The pages poll all three of these from `ui.timer` callbacks. They used to go
through `run_db(engine.get_open_trades)` -- the page naming the method and the
controller supplying a worker thread. Named here instead, so the set of engine
reads the UI can perform is a list you can read rather than anything callable.

The engine is passed in rather than fetched: the composition root owns that
singleton, and a service that reached for it would need the whole app booted
to be testable.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.src.db.database import to_db_thread

__all__ = ["open_trades", "open_positions_view", "signals", "tg_signals"]


async def open_trades(engine: Any) -> list[dict]:
    return await to_db_thread(engine.get_open_trades)


async def open_positions_view(engine: Any) -> list[dict]:
    """The Positions table's rows: the app's open trades carrying the broker's
    running P&L, plus anything open at the broker it has no record of.

    Composed here rather than inside the runtime so the DATABASE read still
    goes off the event loop -- the same hop `open_trades` above makes. The
    bridge half is already async.
    """
    return await engine.get_open_positions_view(await open_trades(engine))


async def signals(engine: Any, status: Optional[str] = None) -> list[dict]:
    return await to_db_thread(engine.get_signals, status=status)


async def tg_signals(engine: Any, limit: int = 50) -> list[dict]:
    return await to_db_thread(engine.get_tg_signals, limit)

"""Realised-P&L reads for the History page's hourly heat-map and 24h figure.

`session_for_hour` was reached as `db_module._session_for_hour` -- a private
name, through the re-export shim, from a controller. It is a real part of this
service's surface, so it is public here.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.analytics import read_repo as _repo

__all__ = ["hourly_grid", "hourly_grid_async", "session_for_hour",
           "realised_pnl_last_24h", "signal_execution_lags"]


def hourly_grid(days: int):
    return _repo.get_hourly_pnl_grid(days)


async def hourly_grid_async(days: int):
    return await to_db_thread(_repo.get_hourly_pnl_grid, days)


def session_for_hour(hour: int) -> str:
    """Which trading session an hour-of-day belongs to (Asian/London/NY)."""
    return _repo._session_for_hour(hour)


def realised_pnl_last_24h(cutoff: float) -> float:
    return _repo.fetch_realised_pnl_last_24h(cutoff)


def signal_execution_lags(db_path: str) -> list:
    return _repo.fetch_signal_execution_lags(db_path)

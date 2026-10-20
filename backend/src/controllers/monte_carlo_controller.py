"""Analysis > Risk: the Monte Carlo on the window's own trades (docs/todo/024).

Its own controller rather than one more operation on `history_controller`,
which sits at the 200-line controller ceiling.
"""
from __future__ import annotations

from backend.src.services.analytics import monte_carlo as _monte_carlo

__all__ = ["monte_carlo"]


async def monte_carlo(engine, days: int, horizon):
    """Resampled next-N-trade outcomes from the window's closed trades."""
    return await _monte_carlo.for_window(engine, days, horizon)

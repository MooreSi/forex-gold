"""Broker fee rates for the History page's cost columns.

`_platform_fee_rate` lives in `mt5_performance`; the page reached it through a
re-export on the runtime facade and then dispatched it off-loop via `run_db`.
Named here so the History page depends on a fee service, not on the runtime.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.broker import mt5_performance as _perf

__all__ = ["platform_fee_rate", "apply_fee"]


async def platform_fee_rate():
    return await to_db_thread(_perf._platform_fee_rate)


def apply_fee(*args, **kwargs):
    return _perf._apply_fee(*args, **kwargs)

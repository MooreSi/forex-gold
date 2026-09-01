"""The trading clock, with the configured offset applied.

`backend/src/utils/trading_clock` holds the arithmetic and knows nothing about
settings -- utils depend on nothing above them. This is the thin layer that
reads `trading_clock_offset_min` and hands the answer over, so every caller
gets the same clock without each one loading settings itself.

Default is the machine's own local time, which is the user's own local time on
the user's own machine. An offset is configured only where the machine is not
where its user is -- a VPS. See utils/trading_clock for the reasoning and
docs/simon-handover/020 for what is still open.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from backend.src.utils.trading_clock import (
    configured_offset_minutes, local_from_timestamp, local_now, local_timestamp,
)

__all__ = ["offset_minutes", "now", "from_timestamp", "to_timestamp"]


def _rs() -> dict:
    from backend.src.services.risk.risk_settings_repo import get_risk_settings
    try:
        return get_risk_settings() or {}
    except Exception:
        # Read on the path that decides whether to trade. Falling back to the
        # machine's own clock is what every single-machine install wants
        # anyway, so a settings read failure must not stop the check.
        return {}


def offset_minutes() -> Optional[int]:
    return configured_offset_minutes(_rs())


def now() -> datetime:
    """Now, on the trading clock. The drop-in for a bare `datetime.now()`."""
    return local_now(offset_minutes())


def from_timestamp(epoch: float) -> datetime:
    """A stored epoch time on the trading clock."""
    return local_from_timestamp(epoch, offset_minutes())


def to_timestamp(wall: datetime) -> float:
    """The epoch second for a naive trading-clock time."""
    return local_timestamp(wall, offset_minutes())

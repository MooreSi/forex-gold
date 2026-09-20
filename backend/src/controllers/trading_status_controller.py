"""The header's trading-status badge, and the Resume behind it.

Its own controller rather than part of `trading_controller` for the reason the
200-line ceiling exists: that file is full, and this is a distinct concern with
a distinct screen element. It forwards to one service and does nothing else.
"""
from __future__ import annotations

from backend.src.services.risk import trading_status as _status

__all__ = ["trading_status_badge", "resume_trading_all"]


def trading_status_badge() -> dict:
    """Is trading running right now, and if not, what is holding it?

    Four mechanisms can hold an automated entry; the badge reports whichever
    one is doing it, in a fixed order. The order is the design -- see
    services/risk/trading_status.py.
    """
    return _status.badge()


def resume_trading_all() -> dict:
    """Clear whichever hold is actually in force, and report which.

    Not the same as `trading_controller.resume_trading`, which lifts the
    governor's manual pause only. The badge can be reporting a tripped circuit
    breaker or the daily profit target instead, and a Resume that cleared
    neither would be a button that visibly does nothing.
    """
    return _status.resume_all()

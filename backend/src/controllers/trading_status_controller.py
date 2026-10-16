"""The header's trading-status badge, and the Resume behind it.

Its own controller rather than part of `trading_controller` for the reason the
200-line ceiling exists: that file is full, and this is a distinct concern with
a distinct screen element. It forwards to one service and does nothing else.
"""
from __future__ import annotations

from backend.src.services.cluster import remote_control as _remote
from backend.src.services.risk import trading_status as _status

__all__ = ["trading_status_badge", "resume_trading_all", "resume_trading_on_vps",
           "resume_trading_past_goal", "resume_trading_past_goal_on_vps",
           "RemoteControlFailed"]

RemoteControlFailed = _remote.RemoteControlFailed


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


async def resume_trading_on_vps() -> dict:
    """The same Resume, run by the VPS against its own holds.

    For a badge that reports the VPS. Never falls back to this node: its
    holds guard no orders.
    """
    return await _remote.resume_trading_on_peer()


def resume_trading_past_goal() -> dict:
    """"Resume anyway (ignore today's goal)": the same Resume, lifting a
    reached daily goal's halt for the rest of today as well."""
    return _status.resume_all_past_goal()


async def resume_trading_past_goal_on_vps() -> dict:
    """The same override, run by the VPS against its own goal."""
    return await _remote.resume_trading_past_goal_on_peer()

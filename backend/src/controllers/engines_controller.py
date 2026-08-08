"""Engine panels' API -- shared by the Breakout, Reversal and Bounce panels.

The panels used to reach `run_db(fn)` here, handing this layer an arbitrary
callable to run on the DB worker thread. That inverted the dependency: the
page chose the data access and the controller just dispatched it. Each of
those calls is now a named function on the owning engine's service.
"""
from __future__ import annotations

from backend.src.services.breakout_signal import panel_data as breakout
from backend.src.services.reversal_engine import panel_data as reversal
from backend.src.services.risk import settings as _risk
from backend.src.services.test_signal import panel_data as bounce

__all__ = ["breakout", "reversal", "bounce",
           "get_risk_settings", "get_risk_settings_async", "update_risk_settings"]


def get_risk_settings() -> dict:
    return _risk.get()


async def get_risk_settings_async() -> dict:
    return await _risk.get_async()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)

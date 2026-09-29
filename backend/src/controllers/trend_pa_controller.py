"""The Trend PA engine's panel: its report and its backtest button.

Forwards only. `services/trend_pa/panel_data.py` decides which node's numbers
the panel sees and where the backtest runs.
"""
from __future__ import annotations

from backend.src.services.cluster import remote_control as _remote
from backend.src.services.trend_pa import panel_data as _panel

__all__ = ["report", "request_backtest", "RemoteControlFailed"]

RemoteControlFailed = _remote.RemoteControlFailed


async def report() -> dict:
    return await _panel.report()


async def request_backtest() -> dict:
    return await _panel.request_backtest()

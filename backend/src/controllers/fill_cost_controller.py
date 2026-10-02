"""Measured fill cost for the Dashboard. Read-only. Forwards to
backend.src.services.cluster.peer_reports, which reads it from the node that
trades."""
from __future__ import annotations

from backend.src.services.cluster import peer_reports as _reports
from backend.src.services.cluster import remote_control as _remote

__all__ = ["report_async", "RemoteControlFailed"]

RemoteControlFailed = _remote.RemoteControlFailed


async def report_async(*args, **kwargs):
    return await _reports.fill_cost_async(*args, **kwargs)

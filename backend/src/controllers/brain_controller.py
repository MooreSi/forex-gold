"""The brain view: recent entry decisions and the gates' state. Read-only.
Read from the node that trades (services/cluster/peer_reports.py), whose gates
and decision log they are; this Mac's own describe a node placing no orders."""
from __future__ import annotations

from backend.src.services.cluster import peer_reports as _reports
from backend.src.services.cluster import remote_control as _remote

__all__ = ["snapshot_async", "RemoteControlFailed"]

RemoteControlFailed = _remote.RemoteControlFailed


async def snapshot_async(limit: int = 60) -> dict:
    return await _reports.brain_async(limit)

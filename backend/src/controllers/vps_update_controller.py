"""Upgrade the paired VPS from the Mac, and compare both nodes' commits
(owner, 2026-09-27). Forwards to
backend.src.services.cluster.sync._update_sync and the sync client unchanged."""
from __future__ import annotations

from backend.src.services.cluster.sync import _update_sync
from backend.src.services.cluster.sync import client as _client

__all__ = ["update_peer", "version_report"]


async def update_peer(timeout: float = 10.0) -> dict:
    """Ask the VPS to update itself to origin/main and restart."""
    return await _client.get_instance().request_peer_update(timeout=timeout)


def version_report() -> dict:
    """Both nodes' commits and git versions, and whether they match."""
    return _update_sync.current_version_report()

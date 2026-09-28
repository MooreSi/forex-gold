"""Which end of the Mac<->VPS link this process is, for code sending a change.

Every `_forward_*_over_sync` used to ask `client.get_instance()` first. That
never returns None -- it builds a SyncClient on first call -- so on the VPS the
server's broadcast was unreachable: a change made there (a circuit-breaker
trip, an edit over RDP) was queued on a client that never connects, and the
Mac never heard of it (reported live, 2026-09-28).

The server is asked first, and only a LISTENING one counts. `get_instance()`
alone is not enough: it is deliberately kept after a stop, so a Mac that once
pressed "Make this node a VPS" and stopped it would otherwise broadcast to
nobody instead of proposing to its real VPS.
"""
from __future__ import annotations


def listening_server():
    """The sync server when this process is the VPS end of the link, else None."""
    from backend.src.services.cluster.sync import server as _server
    if not _server.is_listening():
        return None
    return _server.get_instance()

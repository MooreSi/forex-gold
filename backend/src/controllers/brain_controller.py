"""The brain view: recent entry decisions and the gates' state. Read-only.
Forwards to backend.src.services.brain.feed unchanged."""
from __future__ import annotations

from backend.src.services.brain import feed as _feed

__all__ = ["snapshot_async"]


async def snapshot_async(*args, **kwargs):
    return await _feed.snapshot_async(*args, **kwargs)

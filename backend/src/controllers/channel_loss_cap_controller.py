"""The per-channel daily loss cap: read the Schedule card's state, store the
caps. Forwards to backend.src.services.risk.channel_loss_cap unchanged."""
from __future__ import annotations

from backend.src.services.risk import channel_loss_cap as _cap

__all__ = ["state_async", "set_caps"]


async def state_async(*args, **kwargs):
    return await _cap.state_async(*args, **kwargs)


def set_caps(*args, **kwargs):
    """Store the default cap and the per-channel overrides. Gates entries."""
    return _cap.set_caps(*args, **kwargs)

"""The Breakout engine's tuning experiment ledger: read the card, approve or
reject a proposal, switch owner approval on or off. Forwards to
backend.src.services.breakout_signal.tuning_ledger unchanged."""
from __future__ import annotations

from backend.src.services.breakout_signal import tuning_ledger as _ledger

__all__ = ["state_async", "approve_async", "reject_async", "set_approval_required_async"]


async def state_async(*args, **kwargs):
    return await _ledger.state_async(*args, **kwargs)


async def approve_async(*args, **kwargs):
    """Start a proposed experiment. Changes a live engine parameter."""
    return await _ledger.approve_async(*args, **kwargs)


async def reject_async(*args, **kwargs):
    return await _ledger.reject_async(*args, **kwargs)


async def set_approval_required_async(*args, **kwargs):
    """Whether AI adjustments wait for the owner (on) or apply as before (off)."""
    return await _ledger.set_approval_required_async(*args, **kwargs)

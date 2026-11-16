"""Feedback the user writes in the app's Feedback popup.

Forwards to `services/feedback`. Nothing here touches an order or the broker.
"""
from __future__ import annotations

from backend.src.services.feedback import service as _service

__all__ = ["submit", "completed_notices", "mark_seen", "MAX_MESSAGE"]

MAX_MESSAGE = _service.MAX_MESSAGE


def submit(kind: str, message: str) -> dict:
    """Queue or record the feedback. Raises ValueError with a user-readable reason."""
    return _service.submit(kind, message)


def completed_notices() -> list[dict]:
    """This install's feedback the owner has marked Completed, not yet seen."""
    return _service.completed_notices()


def mark_seen(entry_id: str) -> bool:
    """The sender closed the popup. False when there is no such notice."""
    return _service.mark_seen(entry_id)

"""Feedback the user writes in the app's Feedback popup.

Forwards to `services/feedback`. Nothing here touches an order or the broker.
"""
from __future__ import annotations

from backend.src.services.feedback import service as _service

__all__ = ["submit", "completed_notices", "mark_seen", "unread_count",
           "unread_feedback", "mark_read", "MAX_MESSAGE"]

MAX_MESSAGE = _service.MAX_MESSAGE


def submit(kind: str, message: str, email: str = "") -> dict:
    """Queue or record the feedback. Raises ValueError with a user-readable
    reason. `email` is the sender's optional reply address."""
    return _service.submit(kind, message, email=email)


def completed_notices() -> list[dict]:
    """This install's feedback the owner has marked Completed, not yet seen."""
    return _service.completed_notices()


def mark_seen(entry_id: str) -> bool:
    """The sender closed the popup. False when there is no such notice."""
    return _service.mark_seen(entry_id)


def unread_count() -> int:
    """New feedback for the admin node's title-bar alert; 0 elsewhere."""
    return _service.unread_count()


def unread_feedback() -> list[dict]:
    """The entries behind that alert, newest first."""
    return _service.unread_feedback()


def mark_read(ids: list[str]) -> int:
    """The owner opened the alert: these ids are read."""
    return _service.mark_read(ids)

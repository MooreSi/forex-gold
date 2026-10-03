"""Feedback the user writes in the app's Feedback popup.

Forwards to `services/feedback`. Nothing here touches an order or the broker.
"""
from __future__ import annotations

from backend.src.services.feedback import service as _service

__all__ = ["submit", "MAX_MESSAGE"]

MAX_MESSAGE = _service.MAX_MESSAGE


def submit(kind: str, message: str) -> dict:
    """Queue or record the feedback. Raises ValueError with a user-readable reason."""
    return _service.submit(kind, message)

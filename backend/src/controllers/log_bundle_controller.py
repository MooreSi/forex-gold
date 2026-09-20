"""The support log bundle.

Its own controller rather than part of `settings_controller`: that file is
close to the 200-line ceiling, and this is a distinct operation with a distinct
screen button. It names one operation and forwards it to one service.
"""
from __future__ import annotations

from backend.src.services.diagnostics import log_bundle as _bundle

__all__ = ["build_log_bundle"]


def build_log_bundle(days: int = _bundle.DEFAULT_DAYS) -> dict:
    """The filtered logs for the last `days`, as text, ready to download."""
    return _bundle.build(days=days)

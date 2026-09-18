"""Shapes the News tab reads.

The calendar's own event dict carries both `ts` (a float) and `dt` (a timezone
-aware datetime built from it). Only `ts` crosses the wire: two representations
of one instant is two chances for them to disagree, and the browser formats the
time anyway.
"""
from __future__ import annotations

from pydantic import BaseModel


class NewsEvent(BaseModel):
    title: str
    currency: str
    impact: str
    ts: float
    forecast: str
    previous: str
    score: float


class BlackoutSettings(BaseModel):
    """Whether automated entries are held around an economic release.

    `enabled` defaults True in the calendar, which was found on 2026-09-04 to
    be doing nothing because the page wrote four keys that `load()` did not
    name — so switching the blackout OFF in the UI had no effect at all. The
    settings write is round-tripped through the same reader on save for that
    reason; see `tests/api/routers/test_news.py`.
    """
    enabled: bool
    impact: str
    minutes_before: int
    minutes_after: int


class BlackoutUpdate(BaseModel):
    enabled: bool
    minutes_before: int
    minutes_after: int
    # Which releases the window applies to. `get_blackout_settings` falls back
    # to its default on an unknown value, which is why the response is read
    # back rather than echoed.
    impact: str = "high"


class CurrentEvent(NewsEvent):
    """An event whose blackout window we are inside, with the window's own
    numbers attached by the calendar."""
    model_config = {"extra": "allow"}

    mins_remaining: float | None = None
    mins_to_event: float | None = None


class NewsState(BaseModel):
    """Everything the News tab renders, in one read.

    The banner, the settings and the event list are always shown together and
    are all cheap, so they travel together — one shared poll rather than three
    that can disagree about whether a blackout is running.
    """
    events: list[NewsEvent]
    current: CurrentEvent | None
    blackout: BlackoutSettings
    pause: dict

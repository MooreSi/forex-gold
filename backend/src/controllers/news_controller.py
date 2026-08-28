"""The economic calendar the News page and the header badge read.

Forwards to backend.src.utils.news_calendar unchanged, plus the one config
write the page makes for its blackout settings.

get_events() and invalidate_cache() can trigger a fetch from the calendar
feed; the rest read what is already cached. Nothing here decides whether to
trade around an event -- the risk governor does that, from the same data.
"""
from __future__ import annotations

from backend.src import config as _config
from backend.src.utils import news_calendar as _news

__all__ = [
    "get_events", "get_current_event", "get_blackout_settings",
    "invalidate_cache", "save_config",
]


def get_events(*args, **kwargs):
    """Upcoming calendar events. May fetch if the cache is stale."""
    return _news.get_events(*args, **kwargs)


def get_current_event(*args, **kwargs):
    """The event in progress right now, if any. Cache read."""
    return _news.get_current_event(*args, **kwargs)


def get_blackout_settings(*args, **kwargs):
    return _news.get_blackout_settings(*args, **kwargs)


def invalidate_cache(*args, **kwargs):
    """Drop the cached feed so the next read re-fetches."""
    return _news.invalidate_cache(*args, **kwargs)


def save_config(*args, **kwargs):
    """Persist the News page's blackout settings."""
    return _config.save_config(*args, **kwargs)

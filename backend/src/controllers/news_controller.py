"""The economic calendar the News page and the header badge read.

Forwards to backend.src.utils.news_calendar unchanged.

get_events() and invalidate_cache() can trigger a fetch from the calendar
feed; the rest read what is already cached. Nothing here decides whether to
trade around an event -- the risk governor does that, from the same data.

There was a `save_config` here until 2026-09-18. It forwarded to
`_config.save_config`, which **does not exist** -- `backend.src.config` offers
`save_to_yaml` -- so any caller would have taken an AttributeError. It never
had one: the News page wrote its blackout settings through
`settings_controller.save_config`, which works, so this was a broken duplicate
of an operation the layer already had. Found by
`tests/controllers/test_controller_forwarding.py`, which resolves every
forwarder's target and could not find this one.
"""
from __future__ import annotations

from backend.src.utils import news_calendar as _news

__all__ = [
    "get_events", "get_current_event", "get_blackout_settings",
    "news_pause_state", "invalidate_cache",
]


def get_events(*args, **kwargs):
    """Upcoming calendar events. May fetch if the cache is stale."""
    return _news.get_events(*args, **kwargs)


def get_current_event(*args, **kwargs):
    """The event in progress right now, if any. Cache read."""
    return _news.get_current_event(*args, **kwargs)


def get_blackout_settings(*args, **kwargs):
    return _news.get_blackout_settings(*args, **kwargs)


def news_pause_state(*args, **kwargs):
    """Whether trading is paused for news right now, for the header's box."""
    return _news.news_pause_state(*args, **kwargs)


def invalidate_cache(*args, **kwargs):
    """Drop the cached feed so the next read re-fetches."""
    return _news.invalidate_cache(*args, **kwargs)


"""
Live economic calendar filter.

Thin delegate to core/news_calendar.py, which owns the feed, the cache, the
gold-relevance scoring and the blackout settings. This module used to fetch and
parse the ForexFactory feed itself, on a second cache with a second copy of the
parsing — and it read the currency out of a `currency` key the feed does not
publish (it is `country`), so it matched nothing and the blackout never fired.
Kept as a module so existing call sites keep working.
"""
from __future__ import annotations

from typing import Optional

from backend.src.utils.news_calendar import (
    get_blackout_settings,
    get_current_event as _get_current_event,
    invalidate_cache,
    is_high_impact_window as _is_high_impact_window,
)

__all__ = [
    "get_blackout_settings",
    "get_current_event",
    "invalidate_cache",
    "is_high_impact_window",
]


def get_current_event(
    minutes_before: Optional[int] = None,
    minutes_after: Optional[int] = None,
) -> Optional[dict]:
    """
    Details of the blackout window we are currently inside, or None.
    Defaults to the configured blackout padding when not given explicitly.
    """
    return _get_current_event(minutes_before, minutes_after)


def is_high_impact_window(
    minutes_before: Optional[int] = None,
    minutes_after: Optional[int] = None,
) -> bool:
    """True when new entries should be suppressed for news."""
    return _is_high_impact_window(minutes_before, minutes_after)

"""News tab — the economic calendar and its blackout window.

Read-only except for the blackout settings, which are a config write and not a
money action: the risk governor decides whether to hold an order, from the same
data. This layer does not.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter

from backend.src.api.schemas.news import BlackoutSettings, BlackoutUpdate, NewsState
from backend.src.controllers import news_controller as news_ctl
from backend.src.controllers import settings_controller as settings_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/news", tags=["news"])

# The config keys the blackout reads. Named here because writing a key the
# reader does not look at is exactly the bug this feature already had: the page
# saved four keys, `load()` named none of them, and switching the blackout off
# did nothing at all (found 2026-09-04, on by default the whole time).
BLACKOUT_KEYS = ("news_blackout_enabled", "news_blackout_impact",
                 "news_blackout_minutes_before", "news_blackout_minutes_after")


@router.get("/state", response_model=NewsState)
async def state() -> dict:
    return {
        "events": news_ctl.get_events(),
        "current": news_ctl.get_current_event(),
        "blackout": news_ctl.get_blackout_settings(),
        "pause": news_ctl.news_pause_state(),
    }


@router.put("/blackout", response_model=BlackoutSettings)
async def set_blackout(body: BlackoutUpdate) -> dict:
    """Save the blackout window, then read it back through the calendar.

    The response is what `get_blackout_settings()` now reports, not what was
    sent: it clamps the minutes and falls back on an unknown impact, so echoing
    the request would show the operator a setting the engine is not using.
    """
    settings_ctl.save_config({
        "news_blackout_enabled": bool(body.enabled),
        # The impact level was in the schema and in the browser's types from
        # the start and was never actually written, so the picker could not
        # have worked -- the same shape as the 2026-09-04 bug this endpoint's
        # docstring is about, one key along.
        "news_blackout_impact": str(body.impact),
        "news_blackout_minutes_before": int(body.minutes_before),
        "news_blackout_minutes_after": int(body.minutes_after),
    })
    return news_ctl.get_blackout_settings()


@router.post("/refresh", response_model=NewsState)
async def refresh() -> dict:
    """Drop the cached feed and read it again. The Refresh button."""
    news_ctl.invalidate_cache()
    return await state()

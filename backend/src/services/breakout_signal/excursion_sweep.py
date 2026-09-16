"""The breakout excursion backfill, on the nightly timer.

It is measurement, and the settlement-break slot already exists for exactly
that: `reversal_engine/study_schedule.py` runs the reversal engine's research
study there, and that study already backfills the reversal engine's own
excursions in the same pass. This engine's numbers belong in the same window
and on the same clock.

Not a button, deliberately. The reversal engine's study was button-only and
went four days stale while the AI tuner kept presenting its numbers as
current; the same shape of gap here would be worse, because the breakout
engine has no excursion history at all to go stale from.

22:00 Europe/London for the reasons written up in `study_schedule`: 17:00 New
York is 21:00 UTC under EDT and 22:00 UTC under EST, London local tracks the
shift both ways, and over the seven days to 2026-09-16 the engine's own
analysis log produced 2 signals in that hour against 17-72 in every other.

Its own app_config date key and its own try/except in the loop, so it and the
two jobs beside it cannot take each other's day down.

**Nothing here places, closes or modifies a trade.**
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend.src.db import database as db_module

log = logging.getLogger("breakout_signal")

LAST_RUN_KEY = "bo_excursion_last"

# `>=` not `==`, for the reason the nightly sweep beside it already paid for:
# the caller sleeps 60s per cycle PLUS however long the jobs before it took,
# so the single minute 22:00 is easy to step over.
SLOT_HOUR = 22


async def breakout_excursion_sweep(engine: Any, now: Optional[datetime] = None,
                                   runner=None) -> None:
    """One check of the timer. Backfills at most once per calendar day.

    `engine` is accepted and unused so this matches the signature the minute
    loop calls its jobs with; the backfill finds its own bridge through the
    running breakout engine.
    """
    if now is None:
        now = datetime.now(ZoneInfo("Europe/London"))
    if now.hour < SLOT_HOUR:
        return

    if await db_module.to_db_thread(db_module.is_remote_node):
        return

    date_str = now.strftime("%Y-%m-%d")
    if db_module.get_app_config(LAST_RUN_KEY) == date_str:
        return

    if runner is None:
        from backend.src.services.breakout_signal import excursion_backfill
        runner = excursion_backfill.run

    try:
        result = await runner()
    except Exception as e:                          # noqa: BLE001
        log.warning("[BO-Engine] excursion sweep failed, day not marked: %s", e)
        return

    if (result or {}).get("error"):
        # The engine was not running, so nothing was measured. Marking the day
        # done would burn the single daily run on a pass that read nothing.
        log.info("[BO-Engine] excursion sweep skipped: %s", result["error"])
        return

    db_module.set_app_config(LAST_RUN_KEY, date_str)
    log.info("[BO-Engine] excursion sweep: %s", (result or {}).get("summary"))

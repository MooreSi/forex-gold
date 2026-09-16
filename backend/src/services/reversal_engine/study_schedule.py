"""The phase-1 research study, on a timer instead of a button.

The study (`research_lab.run_study`) is the only producer of the reach
distribution and the exit-policy sweep, and it is the only place they are
stored. `ai_tuner.gather_evidence` READS that stored summary; it cannot
refresh it. So for as long as the study was button-only, the evidence the
tuner showed a model was however old the last time somebody pressed it.

On 2026-09-16 that was four days: the stored sweep was from 2026-09-12 and
was still being presented as the current reach evidence. The same file
already records what missing evidence costs -- the 2026-09-11 run "proposed
a 2.0x ATR target that neither the reach data nor the sweep supports. It
reasoned correctly from half a picture." Stale evidence is that failure
with a slower fuse.

**Nothing here places, closes or modifies a trade.** The study reads history
and writes two measurement columns (`mfe_pts`, `mae_pts`, write-once via
`measure_repo`) plus TCA cost rows. It is deliberately NOT wired to anything
that acts: `_ai_tune_loop` remains off behind `re_ai_tuning_enabled`, and
whether an AI may write live capability switches unattended is the owner's
decision, not a side effect of scheduling a report.

## Why 22:00 Europe/London

Not "late", specifically the daily settlement break. 17:00 New York is
21:00 UTC under EDT and 22:00 UTC under EST, and London local time tracks
that shift in both directions -- which is why the nightly Telegram sweep
already uses London time rather than UTC. Measured over the seven days to
2026-09-16, the engine's own `re_analysis_log` produced **2 signals in the
21:00 UTC hour** against 17-72 in every other hour of the day. It is the
one hour where a study's several hundred bridge round trips are not
competing with signal dispatch for the same broker connection.

That is the whole mitigation for the load, and it is worth being plain
about the limit: bugs/030 moved the study's CPU-heavy arithmetic off the
event loop on 2026-09-12 and deliberately left its database and bridge
reads where they were. They still run on the loop. They await properly so
they cannot freeze dispatch the way the 21.5s sweep did, but ~250
sequential `get_ticks_range` calls at ~0.19s each will saturate the bridge
for minutes. Scheduling it into the quiet hour is what makes that
acceptable; it is not the same thing as making it cheap.

It shares the minute timer the nightly research sweep already runs on
(`research_loop`) rather than claiming an asyncio task of its own.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from backend.src.db import database as db_module

log = logging.getLogger(__name__)

# The app_config key that makes the run idempotent across restarts, exactly
# as `re_research_last` does for the nightly Telegram sweep.
LAST_RUN_KEY = "re_study_last"

# From this hour onward, London local. `>=` rather than `==`, and for a
# reason this codebase has already paid for once: the caller sleeps 60s per
# cycle PLUS however long the sweep before it took, so the checks drift and
# the single minute 22:00 is easy to step straight over. 2026-08-09, 08-14
# and 08-15 have no nightly research row from exactly that. The date key
# above is what stops a wider window double-running.
SLOT_HOUR = 22


def _bridge_of_running_engine() -> Optional[Any]:
    """The broker connection the engine is actually trading on.

    Same source the Run study button uses. The study has to read the
    history of that connection; a second one opened here would be reaching
    around the engine to answer questions about the engine.
    """
    try:
        from backend.src.services.reversal_engine import \
            reversal_engine_service as _svc
        engine = _svc.get_instance()
        return getattr(engine, "_bridge", None) if engine else None
    except Exception:                             # noqa: BLE001
        return None


async def reversal_engine_study_sweep(engine: Any, now: Optional[datetime] = None,
                                      study_runner=None) -> None:
    """One check of the timer. Runs the study at most once per calendar day.

    `engine` is accepted and unused so this matches the signature the minute
    loop calls its jobs with; the study needs the bridge, not the runtime.
    """
    if now is None:
        now = datetime.now(ZoneInfo("Europe/London"))
    if now.hour < SLOT_HOUR:
        return

    # Same gate the nightly sweep uses, and for the same reason: the study
    # reads through the local engine's bridge and writes its measurement
    # columns to the local database. A remote node running it duplicates the
    # broker load to measure a book it is not keeping.
    if await db_module.to_db_thread(db_module.is_remote_node):
        return

    date_str = now.strftime("%Y-%m-%d")
    if db_module.get_app_config(LAST_RUN_KEY) == date_str:
        return

    bridge = _bridge_of_running_engine()
    if bridge is None:
        # Deliberately NOT marking the day done. Claiming it here would burn
        # the single daily run on a pass that measured nothing, which is the
        # staleness this schedule exists to stop.
        log.info("[RE-Study] no engine bridge available; will retry next minute")
        return

    if study_runner is None:
        from backend.src.services.reversal_engine import research_lab
        study_runner = research_lab.run_study

    try:
        report = await study_runner(bridge)
    except Exception as e:                        # noqa: BLE001
        # The study degrades rather than fails by design, so reaching here
        # means something outside it broke. The day stays unclaimed and the
        # next minute tries again.
        log.warning("[RE-Study] nightly study failed, day not marked: %s", e)
        return

    db_module.set_app_config(LAST_RUN_KEY, date_str)
    log.info("[RE-Study] ran: %s closed trades, %s excursions, %s paths",
             (report or {}).get("n_closed"), (report or {}).get("n_excursions"),
             (report or {}).get("n_paths"))

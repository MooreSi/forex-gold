"""Reversal Engine nightly research sweep -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine._reversal_engine_research_loop's per-cycle
check-and-run body, as part of the core/engine.py migration series. See
docs/todo/refactor/core-reversal-research-migration/020-*.md.

Gates a nightly ML-feature research job -- no MT5 order is ever placed,
closed, or modified.

`research_runner` is taken as an injectable async callable (defaulting to
the real `telegram_research.run_nightly_research`) -- the actual pipeline is
a large, separate-module dependency (Telegram history read, Claude
synthesis, ML retrain, email) out of scope for this extraction, same
"optional injected async callable" pattern used throughout this cluster.
It needs the full engine instance, so `engine` is forwarded through
unchanged, exactly as the original passed `self`.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Optional
from zoneinfo import ZoneInfo

from forex_trader.core import database as db_module

log = logging.getLogger(__name__)


async def reversal_engine_research_sweep(engine: Any, now: Optional[datetime] = None,
                                  research_runner=None) -> None:
    if now is None:
        now = datetime.now(ZoneInfo("Europe/London"))
    _is_local = not await db_module.to_db_thread(db_module.is_remote_node)
    # Any time from 22:00 onward, not the single minute 22:00 exactly.
    #
    # The caller sleeps 60s per cycle plus however long the sweep itself took,
    # so the checks drift and minute 0 is easy to step straight over; and an
    # app restarted at 22:00:30 missed that day outright, since the window had
    # already passed by the time the loop's own 90s settle delay finished.
    # 2026-08-09, 08-14 and 08-15 have no research row for exactly this reason,
    # which leaves ref_discipline_score / ref_aggression_score -- two live ML
    # features -- running on whatever the last successful night wrote.
    #
    # The app_config date key already makes this idempotent, so a wider window
    # cannot double-run: it is the same guard that stopped a restart near 22:00
    # firing twice, just now doing the work it was always able to do.
    if _is_local and now.hour >= 22:
        date_str = now.strftime("%Y-%m-%d")
        if db_module.get_app_config("re_research_last") != date_str:
            if research_runner is None:
                from forex_trader.reversal_engine import telegram_research
                research_runner = telegram_research.run_nightly_research
            result = await research_runner(engine)
            if result.get("ran"):
                db_module.set_app_config("re_research_last", date_str)
            log.info("[RE-Research] nightly run result: %s", result)

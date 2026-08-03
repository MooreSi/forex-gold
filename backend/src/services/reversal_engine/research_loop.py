"""Daily Reversal Engine Telegram research sweep (22:00 Europe/London).

Moved off the runtime in M4 B9e. The runtime keeps a shell that owns the
asyncio task; this owns what the task does.

`is_running` is a CALLABLE, not a bool. The flag it reads is flipped by
shutdown() while the loop is awaiting, so a captured value would leave the
loop spinning after the app was told to stop.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable


# `engine` is threaded straight through to the sweep, which still takes an
# engine-shaped object -- named here rather than hidden behind `self`.
import asyncio

from backend.src.services.reversal_engine.research import reversal_engine_research_sweep as _reversal_engine_research_sweep_impl


log = logging.getLogger(__name__)


async def reversal_engine_research_loop(engine: Any, is_running: Callable[[], bool]) -> None:
    """Once a day at 22:00 Europe/London, read the day's Gold Diggers
    REF + GD2 Telegram messages (text + chart images) and have Claude
    synthesise the real trader's risk-management/entry-logic behaviour
    into two scores that feed Reversal Engine's ML model (ml_engine.py's
    ref_discipline_score / ref_aggression_score features), force an
    immediate retrain, and email a summary. See
    reversal_engine/telegram_research.py for the full pipeline. Checked
    every minute like the ORB report job above — zoneinfo handles the
    BST/GMT switch automatically. Dedup'd by date via app_config so a
    restart near 22:00 can't fire it twice the same day.

    Gated to the physical local node only (is_remote_node(), same gate
    Reversal Engine's own signal generator uses) — NOT _is_active_trader_node().
    The ML model this enriches/retrains (re_ml_batch.pkl/re_ml_online.pkl)
    is a per-node file, never auto-synced between Mac and VPS, and GD
    Copy's signal generation is now local-node-only regardless of which
    side executes trades — so this must follow generation, not execution,
    or it retrains a model nothing is using. Both nodes still only ever
    run this once (is_remote_node() is unconditional, unlike the old
    active-trader check which could migrate), so no duplicate email risk.
    """
    await asyncio.sleep(90)  # let the app settle before the first check
    while is_running():
        try:
            await _reversal_engine_research_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_reversal_engine_research_loop error: %s", e)
        await asyncio.sleep(60)

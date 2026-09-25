"""The Reversal Engine's daily jobs (22:00 Europe/London), on one timer.

The Telegram research sweep this module was built for, and the phase-1
research study (`study_schedule`), which was button-only until 2026-09-16
and had gone four days stale while the AI tuner kept presenting its numbers
as current. Also the meta-labeller's daily refit (`meta_label_schedule`),
which is NOT tied to 22:00 -- its model lives in memory, so a restarted app
fits on the first pass.

They share this minute timer rather than each claiming an asyncio task in
runtime.py, and they share the hour for the same reason: 22:00 London is the
daily settlement break, the one hour where the study's several hundred
bridge round trips are not competing with signal dispatch. Each keeps its
own app_config date key and its own try/except, so neither can take the
other's day down with it.

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
from backend.src.services.reversal_engine.study_schedule import (
    reversal_engine_study_sweep as _reversal_engine_study_sweep_impl,
)
from backend.src.services.breakout_signal.excursion_sweep import (
    breakout_excursion_sweep as _breakout_excursion_sweep_impl,
)
from backend.src.services.reversal_engine.meta_label_schedule import (
    meta_label_refit_sweep as _meta_label_refit_sweep_impl,
)
from backend.src.services.reversal_engine.xasset_sweep import (
    xasset_sweep as _xasset_sweep_impl,
)
from backend.src.services.reversal_engine.tpl_label import (
    tpl_label_sweep as _tpl_label_sweep_impl,
)
from backend.src.services.reversal_engine.edge_model import (
    edge_model_refit_sweep as _edge_model_refit_sweep_impl,
)


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
        # Two independent daily jobs sharing one timer, each with its own
        # try/except and its own app_config date key. Telegram being down
        # must not take the research study's day with it, and vice versa --
        # a single shared except would have exactly that effect.
        try:
            await _reversal_engine_research_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_reversal_engine_research_loop error: %s", e)
        try:
            await _reversal_engine_study_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_reversal_engine_study_sweep error: %s", e)
        try:
            await _breakout_excursion_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_breakout_excursion_sweep error: %s", e)
        # Not in the 22:00 slot: the model lives in memory, so a restarted
        # app fits on its first pass. See meta_label_schedule.
        try:
            await _meta_label_refit_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_meta_label_refit_sweep error: %s", e)
        # Cross-asset context, one day per pass, newest first.
        # docs/todo/reversal-engine/230.
        try:
            await _xasset_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_xasset_sweep error: %s", e)
        # The template's own exits for every triggered signal, one day per
        # pass, newest first; then the edge model's daily refit on them.
        # docs/todo/reversal-engine/240.
        try:
            await _tpl_label_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_tpl_label_sweep error: %s", e)
        try:
            await _edge_model_refit_sweep_impl(engine)
        except asyncio.CancelledError:
            break
        except Exception as e:
            log.warning("_edge_model_refit_sweep error: %s", e)
        await asyncio.sleep(60)

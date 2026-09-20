"""The AI Analysis tab's one call: everything the model reads, gathered once.

The NiceGUI page assembled this in the browser layer -- tick, candles, the
last day's parsed Telegram signals, MT5 performance, the strategy catalogue,
H1/M15 candles for volatility and the measured TP-ladder reach -- and then
made **one** model call that answered every metric together: sentiment, price
range, drivers, risks, levels and a strategy recommendation. That is what this
module is. One question costs one call and produces one coherent view; a
question per metric costs several and produces several that can disagree.

**Every piece of evidence is optional.** They are context for the prompt, not
preconditions: a bridge that will not answer for H1 candles must reduce what
the model knows, never turn into "analysis failed". The prompt itself, and the
shape of the answer, belong to `claude_ai.request_market_analysis`.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from backend.src.services.ai import claude_ai as _ai
from backend.src.services.risk import app_config as _config

log = logging.getLogger(__name__)

# Where the last analysis is kept, so reopening the tab costs nothing. The
# NiceGUI page used this key and the same wrapper shape; keeping both means an
# install that switches between the two checkouts keeps its last research.
LAST_RESEARCH_KEY = "ai_last_research"

# One day of parsed signals, as the page has always sent.
_SIGNAL_WINDOW_SECS = 86_400
# The window MT5 performance is measured over, in days.
_PERFORMANCE_DAYS = 90


def _load_config() -> dict:
    import backend.src.config as cfg_module
    return cfg_module.load()


def _recent_signals(cutoff: float) -> list:
    from backend.src.services.analytics import reporting
    return reporting.recent_tg_signals(cutoff)


def _ladder_reach() -> dict:
    from backend.src.services.analytics import reporting
    return reporting.strategy_ladder_reach()


def _strategy_catalogue() -> list:
    # Built fresh on every run so a template saved seconds ago is already
    # recommendable -- the whole reason this is not a list built at import.
    from backend.src.services.positions import core_strategy_catalogue
    return core_strategy_catalogue.build_catalogue()


async def _optional(what: str, coro) -> Any:
    """Await something the analysis would like but can manage without."""
    try:
        return await coro
    except Exception as e:
        log.debug("[AI research] %s unavailable: %s", what, e)
        return None


def _optional_sync(what: str, fn) -> Any:
    try:
        return fn()
    except Exception as e:
        log.debug("[AI research] %s unavailable: %s", what, e)
        return None


async def run_research(engine, timeout: int = 60) -> dict:
    """Gather the evidence, ask the model once, keep the answer. **Billable.**

    Raises whatever the provider raises. A failed call is never stored: a
    stored failure would be served on every later open as though it were an
    analysis.
    """
    cfg = _load_config()

    tick = await _optional("tick", engine.get_tick())
    candles = await _optional("M5 candles", engine.get_candles("M5", 50)) or []
    performance = await _optional(
        "MT5 performance", engine.compute_mt5_performance(_PERFORMANCE_DAYS),
    ) or {}
    # Volatility context (2026-09-04): without it the prompt argues from each
    # template's configured rungs and recommends ladders the trail closes out
    # two rungs in.
    h1 = await _optional("H1 candles", engine.get_candles("H1", 30))
    m15 = await _optional("M15 candles", engine.get_candles("M15", 30))

    signals = _optional_sync(
        "recent signals", lambda: _recent_signals(time.time() - _SIGNAL_WINDOW_SECS),
    ) or []
    strategies = _optional_sync("strategy catalogue", _strategy_catalogue) or []
    ladder = _optional_sync("ladder reach", _ladder_reach) or {}

    analysis = await _ai.request_market_analysis(
        tick=tick,
        candles=candles,
        recent_signals=signals,
        performance=performance,
        cfg=cfg,
        timeout=timeout,
        strategies=strategies,
        h1_candles=h1,
        m15_candles=m15,
        ladder_reach=ladder,
    )

    saved_at = str(analysis.get("generated_at")
                   or datetime.now(timezone.utc).isoformat())
    _store(analysis, saved_at)
    return {"analysis": analysis, "saved_at": saved_at}


def _store(analysis: dict, saved_at: str) -> None:
    try:
        _config.set(LAST_RESEARCH_KEY,
                    json.dumps({"data": analysis, "saved_at": saved_at}))
    except Exception as e:
        # Failing to remember an analysis must not lose the one in hand.
        log.debug("[AI research] could not store the analysis: %s", e)


def last_research() -> dict:
    """The analysis this install last ran, or nothing. Asks no model.

    Its own read on purpose: opening the tab shows what was last found without
    spending anything, which is what makes the Research button a decision
    rather than a toll.
    """
    raw: Optional[str] = None
    try:
        raw = _config.get(LAST_RESEARCH_KEY)
    except Exception as e:
        log.debug("[AI research] could not read the stored analysis: %s", e)
    if not raw:
        return {"analysis": None, "saved_at": ""}
    try:
        wrapper = json.loads(raw)
        data = wrapper.get("data")
    except Exception:
        # Unreadable is the same as absent. Half-decoding it would put a
        # fragment of an old analysis on screen under a fresh timestamp.
        return {"analysis": None, "saved_at": ""}
    if not isinstance(data, dict):
        return {"analysis": None, "saved_at": ""}
    return {"analysis": data, "saved_at": str(wrapper.get("saved_at") or "")}

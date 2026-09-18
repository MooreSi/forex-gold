"""Is trading stopped, why, and until when — for a screen to say so.

Two independent things stop automated entries and they are stored separately:

  * the **risk governor** writes `trade_pause_until` with `risk_halt_reason`
    (drawdown, daily loss, give-back, a `/pause` from Telegram);
  * the **circuit breaker** writes `circuit_breaker_active_until` onto the
    risk-settings row after a run of losing trades.

The NiceGUI header badge read both. The React header read only the governor
between 2026-09-18 and this module, so a tripped circuit breaker was invisible
on every screen but Settings > Diagnostics — an operator looking at the header
would have believed automated entries were running while they were being
refused.

**This is a read for display, and deliberately NOT the enforcement path.**
`governor.is_trading_paused()` is the last line of defence: `open_trade` checks
it before both send paths and it fails CLOSED, reporting PAUSED when the
database cannot be read, because a protective halt that stops protecting on a
transient error is worse than no halt. That function is untouched here, and
this module ASKS it rather than re-deriving the answer — a second, laxer copy
of that logic would eventually be the one somebody wired into a decision.

This one fails the other way. It renders on every header refresh, so an error
costs the explanation, never the halt.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.risk import circuit_breaker_repo as _breaker
from backend.src.services.risk import governor as _governor

log = logging.getLogger(__name__)

__all__ = ["summary"]


def _pause_until() -> float:
    """When the governor's halt expires, or 0.

    Read separately from `is_trading_paused()` because that answers the
    question and this one only decorates it. A failure here loses the resume
    time and keeps the halt.
    """
    try:
        return float(db_module.get_app_config("trade_pause_until") or 0)
    except Exception:
        return 0.0


def summary() -> dict:
    """`{paused, reason, until, source}` covering both halts.

    `until` is the LATER of the two when both are in force: trading resumes
    when the last of them expires, and reporting the earlier one tells the
    operator to expect entries that will still be refused.

    `source` is "governor", "circuit-breaker", "both" or "". It is separate
    from the text so a screen can choose an icon without parsing prose.
    """
    reasons: list[str] = []
    untils: list[float] = []
    sources: list[str] = []

    try:
        if _governor.is_trading_paused():
            sources.append("governor")
            # An older row may have the pause and no stored text. Reporting
            # "not paused" because the TEXT is missing is the worst possible
            # reading of a halt.
            reasons.append(_governor.halt_reason()
                           or "trading is halted by a risk guard")
            untils.append(_pause_until())
    except Exception as exc:
        log.debug("[pause_status] governor unreadable: %s", exc)

    try:
        breaker = _breaker.get_circuit_breaker_state() or {}
        if breaker.get("is_active"):
            sources.append("circuit-breaker")
            losses = int(breaker.get("consec_losses") or 0)
            reasons.append(
                f"circuit breaker: {losses} consecutive losing trades")
            untils.append(float(breaker.get("active_until") or 0))
    except Exception as exc:
        log.debug("[pause_status] circuit breaker unreadable: %s", exc)

    if not sources:
        return {"paused": False, "reason": "", "until": None, "source": ""}

    resume: Optional[float] = max(untils) if any(untils) else None
    return {
        "paused": True,
        "reason": " · ".join(reasons),
        "until": resume,
        "source": "both" if len(sources) > 1 else sources[0],
    }

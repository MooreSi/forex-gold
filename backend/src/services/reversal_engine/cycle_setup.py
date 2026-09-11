"""Per-cycle setup for the reversal engine: what the generator is told.

Split out of `reversal_engine_service.py`, which was five lines under its
800-line ceiling. These two belong together and neither belongs in the
orchestrator: one decides the context a signal's geometry is built from, the
other fetches the extra levels that context might include. Both are pure or
near-pure, which is the point -- the single decision that changes a signal's
stop and targets is testable without a bridge, a database or a running loop.

See docs/todo/reversal-engine/200 sections 1.1 and 4.1.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from backend.src.services.risk import capability_gates as _caps

_log = logging.getLogger("reversal_engine")


def cycle_context(atr: float, adx: float, htf: str, session: str,
                  price: float, rs: dict) -> dict:
    """The per-cycle context handed to `signal_generator.build_signal`.

    Module-level and pure so the one decision that changes a signal's
    geometry is testable without a bridge, a database and a running loop.

    `atr_barriers` is None unless the owner has turned it on, and
    `build_signal` treats None as "use the level-score stop this engine has
    always used" -- so a default settings row produces a byte-identical
    signal. See docs/todo/reversal-engine/200 section 1.1.
    """
    return {
        "atr":             atr,
        "adx":             adx,
        "htf_bias":        htf,
        "h1_bias":         htf,  # use H1 as proxy
        "session":         session,
        "price_at_signal": price,
        "atr_barriers":    _caps.atr_barrier_config(rs or {}),
    }


# The intraday anchor for VWAP, the volume profile and the initial balance.
# 07:00 UTC, the London open: it is the start of gold's real liquidity day,
# it is where the session's participants start measuring from, and anchoring
# at midnight UTC instead would fold eight hours of thin Asian trade into
# every reference price the levels are built from.
SESSION_ANCHOR_HOUR_UTC = 7


def session_anchor(now: float) -> float:
    """The most recent London open at or before `now`."""
    dt = datetime.fromtimestamp(float(now), tz=timezone.utc)
    anchor = dt.replace(hour=SESSION_ANCHOR_HOUR_UTC, minute=0, second=0,
                        microsecond=0)
    if anchor > dt:
        anchor -= timedelta(days=1)
    return anchor.timestamp()


async def liquidity_map_levels(bridge, now: float) -> list[dict]:
    """Previous-day/week, opening, initial-balance, VWAP and value-area
    levels, or [].

    Never raises: a missing series must cost the cycle its extra levels, not
    the cycle itself. Returns [] on any failure, which is exactly what the
    engine did before these levels existed.
    """
    try:
        from backend.src.services.market import liquidity_map as lmap
        d1 = await bridge.get_candles("D1", 15)
        # 07:00 UTC to now is at most 17 hours; 220 M5 candles covers it
        # with room for the broker's own gaps.
        m5 = await bridge.get_candles("M5", 220)
        return lmap.as_candidate_levels(d1 or [], now,
                                        intraday_candles=m5 or [],
                                        session_open_ts=session_anchor(now))
    except Exception as e:                        # noqa: BLE001
        _log.debug("[RE-Engine] liquidity map unavailable: %s", e)
        return []


def filter_blocked_types(candidates: list, rs: dict) -> list:
    """Drop candidates whose level type the owner has refused.

    A candidate with NO type is kept. An untyped level is a bug somewhere
    upstream, and dropping it here would hide that bug while quietly
    reducing what the engine trades -- the combination this repo's rules
    exist to prevent.
    """
    blocked = _caps.blocked_level_types(rs or {})
    if not blocked:
        return candidates
    out = []
    for c in candidates or []:
        t = str(c.get("type") or "").strip().lower()
        if t and t in blocked:
            continue
        out.append(c)
    return out

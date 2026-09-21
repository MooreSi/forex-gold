"""The header's trading-status badge: is trading actually running right now?

One always-visible indicator, because there are four separate mechanisms that
can hold an automated entry and an operator should not have to visit four
screens to find out which one is doing it.

**The order of the states is the design, not a preference.** Each of the lower
three is a true statement that would be misleading on its own:

1. `halted`        -- the circuit breaker tripped, or a manual pause is in
                      force. Needs a human to Resume and can last the day,
                      so it outranks everything.
2. `profit_target` -- the day earned its target and automated entries are
                      held for the rest of it. Below a halt (that is a loss
                      guard, this is a win), above a blackout (that lifts
                      itself, this needs a Resume).
3. `news_blackout` -- entries held for a news window, which lifts itself.
4. `ok`            -- nothing is holding anything.

`ok` is reachable only when none of the others apply. A badge saying so while
every entry is being held is a false all-clear, which is the complaint that put
states 2 and 3 into the NiceGUI header in the first place.

The `ok` label reads "Trading Active", not "Circuit Breaker OK". The breaker is
one of four mechanisms this reports and naming it in the all-clear made the
header look like a breaker readout -- so the owner read the other three states
as breaker messages too (report, 2026-09-21). The badge reports the STATUS; the
detail line names the mechanism.

There is a fifth, `unknown`, for when the breaker itself cannot be read. It
exists because the alternative is reporting an all-clear on no evidence, and
that is the one failure this badge must never make.

Nothing here places an order, closes one, or reaches a broker.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime

log = logging.getLogger(__name__)

_CONFIG_KEY = "trade_pause_until"


# Deferred imports, for the same circular-import reason as schedule_options:
# risk_settings_repo imports db.database, which imports it back.
def _breaker_state() -> dict:
    from backend.src.services.risk.circuit_breaker_repo import get_circuit_breaker_state
    return get_circuit_breaker_state()


def _trade_pause_until() -> float:
    from backend.src.services.risk.app_config_repo import get_app_config
    try:
        return float(get_app_config(_CONFIG_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0


def _daily_target_state() -> dict:
    from backend.src.services.risk.schedule import daily_profit_target_state
    return daily_profit_target_state()


def _news_state() -> dict:
    from backend.src.utils.news_calendar import news_pause_state
    return news_pause_state()


def _halt_reason() -> str:
    """Why the governor says trading is stopped.

    `pause_status.summary()` rather than the old `trading_halt_reason`, which
    was replaced on 2026-09-18 precisely because it asked the risk governor
    only -- the circuit breaker writes a different key, so a tripped breaker
    reached no screen but Settings > Diagnostics. This badge must not repeat
    that.
    """
    from backend.src.services.risk import pause_status
    return str(pause_status.summary().get("reason") or "")


def _until_text(ts: float) -> str:
    try:
        return datetime.fromtimestamp(ts).strftime("%d %b %H:%M")
    except (OSError, OverflowError, ValueError):
        return "?"


def _safe(fn, fallback):
    """A collaborator failing must cost its own state, not the badge.

    Used for everything EXCEPT the breaker read -- see `badge`.
    """
    try:
        return fn()
    except Exception as exc:
        log.debug("[TradingStatus] %s failed: %s", getattr(fn, "__name__", fn), exc)
        return fallback


def badge() -> dict:
    """What the header shows, as one read.

    `can_resume` is whether a human pressing Resume would change anything --
    false for a news blackout, which lifts itself, so the UI does not offer a
    button that would do nothing.
    """
    try:
        breaker = _breaker_state()
    except Exception as exc:
        # Deliberately not `_safe`. Every other state degrades to "not
        # holding anything", which is a safe default for a badge. This one
        # does not: an unreadable breaker reported as OK is a false
        # all-clear on the mechanism that blocks real orders.
        log.warning("[TradingStatus] circuit breaker unreadable: %s", exc)
        return {
            "state": "unknown",
            "label": "Trading Status Unknown",
            "detail": "The circuit breaker could not be read, so this cannot "
                      "confirm trading is running.",
            "until": None, "resume_ts": None, "can_resume": False,
        }

    now = time.time()
    breaker_active = bool(breaker.get("is_active"))
    pause_until = _safe(_trade_pause_until, 0.0)
    manual_paused = pause_until > now

    if breaker_active or manual_paused:
        until = max(
            float(breaker.get("active_until") or 0) if breaker_active else 0.0,
            pause_until if manual_paused else 0.0,
        )
        reasons = []
        if manual_paused:
            reasons.append(_safe(_halt_reason, "") or "Trading paused")
        if breaker_active:
            reasons.append(
                f"Circuit breaker active "
                f"({breaker.get('losses_threshold')} consecutive losses)"
            )
        return {
            "state": "halted",
            "label": f"Trading Paused until {_until_text(until)}",
            "detail": " / ".join(r for r in reasons if r),
            "until": until, "resume_ts": None, "can_resume": True,
        }

    target = _safe(_daily_target_state, {"reached": False})
    if target.get("reached"):
        pnl = float(target.get("pnl") or 0)
        goal = float(target.get("target") or 0)
        return {
            "state": "profit_target",
            "label": "Profit Target Reached",
            "detail": (f"Daily profit target reached (${pnl:.2f} of ${goal:.2f}) "
                       f"— automated entries are held for the rest of today."),
            "until": None, "resume_ts": None, "can_resume": True,
        }

    news = _safe(_news_state, {"paused": False})
    if news.get("paused"):
        return {
            "state": "news_blackout",
            "label": "News Blackout",
            "detail": str(news.get("detail") or news.get("label") or ""),
            "until": None,
            # The browser counts down against its own clock, so the number
            # moves between polls instead of freezing at whatever the
            # calendar last said.
            "resume_ts": news.get("resume_ts"),
            # It lifts itself. Offering a Resume would imply otherwise.
            "can_resume": False,
        }

    return {
        "state": "ok",
        "label": "Trading Active",
        "detail": "Nothing is holding automated entries.",
        "until": None, "resume_ts": None, "can_resume": False,
    }


def resume_all() -> dict:
    """Clear whichever of the three holds is actually in force.

    Mirrors the NiceGUI Resume dialog, and mirrors it deliberately: the badge
    can be reporting any of three different mechanisms, and a Resume that only
    cleared the governor would leave the operator pressing a button that
    visibly does nothing while the breaker is the thing holding entries.

    **Re-reads rather than trusting the badge.** The click arrives seconds
    after the poll that drew it, and clearing a halt that has since lifted
    itself -- or missing one that has just tripped -- is exactly the stale-state
    bug guarded against everywhere else.

    The daily target is lifted for TODAY only; that is what the service does
    and the response says so. Per-window targets and the window hours are
    separate gates and are left alone.

    Returns what was actually cleared, so the caller can say so rather than
    claiming a blanket "resumed".
    """
    cleared = []

    try:
        if _breaker_state().get("is_active"):
            from backend.src.services.risk.circuit_breaker_repo import reset_circuit_breaker
            reset_circuit_breaker()
            cleared.append("circuit_breaker")
    except Exception as exc:
        log.warning("[TradingStatus] could not reset the circuit breaker: %s", exc)

    try:
        if _trade_pause_until() > time.time():
            from backend.src.services.risk import manual_pause
            manual_pause.resume()
            cleared.append("manual_pause")
    except Exception as exc:
        log.warning("[TradingStatus] could not lift the manual pause: %s", exc)

    try:
        if _daily_target_state().get("reached"):
            from backend.src.services.risk.schedule import resume_past_daily_profit_target
            resume_past_daily_profit_target()
            cleared.append("daily_profit_target")
    except Exception as exc:
        log.warning("[TradingStatus] could not lift the daily target: %s", exc)

    return {"cleared": cleared, "status": badge()}

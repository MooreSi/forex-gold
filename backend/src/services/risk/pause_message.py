"""What a Telegram signal alert says while trading is paused.

One wording for every route a paused signal can take (owner, 2026-10-05). With
the daily goal reached, an out-of-zone signal was announced as "Signal queued
... Will auto-activate when price returns to zone" and an in-zone one as
"Auto-execution failed: Trading paused until 22:00 -- MT5 order blocked: ...".
Both had in fact been left `pending`, and neither would open while paused.

The headline is the header badge's own (`trading_status.halt_label`), so the
phone and the dashboard name the same pause in the same words.

Display only. Nothing here decides whether anything trades:
`governor.is_trading_paused` does, and `open_trade` enforces it.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.risk import governor as _gov
from backend.src.services.risk.trading_status import GOAL_REASON_PREFIX, halt_label

__all__ = ["signal_not_executed", "limit_not_placed",
           "current_pause", "reword_result"]


def headline(until: float, reason: str) -> str:
    """'⏸️ Goal Achieved Paused until 05 Oct 22:00 (Daily goal secured: ...)'."""
    only_goal = reason.startswith(GOAL_REASON_PREFIX)
    if until and until > 0:
        head = halt_label(until, only_goal)
    else:
        head = "Goal Achieved Paused" if only_goal else "Trading Paused"
    return f"⏸️ {head} ({reason})" if reason else f"⏸️ {head}"


def signal_queued(until: float, reason: str) -> str:
    """The signal row is left `pending`; the watcher may still open it."""
    return (f"{headline(until, reason)}. Signal queued, not executed: it opens "
            f"only if trading resumes while it is still valid.")


def signal_not_executed(until: float, reason: str) -> str:
    """Auto-execution is off as well; nothing was queued for this signal."""
    return f"{headline(until, reason)}. Signal received, not executed."


def limit_not_placed(until: float, reason: str) -> str:
    return f"{headline(until, reason)}. Limit order not placed."


def current_pause() -> Optional[tuple[float, str]]:
    """(until, reason) while the governor's pause is in force, else None.

    `is_trading_paused` fails CLOSED, so an unreadable database reads as
    paused here too: the alert then names a pause with no reason, which is
    what `open_trade` will do with the order.
    """
    if not _gov.is_trading_paused():
        return None
    from backend.src.db import database as db_module
    try:
        until = float(db_module.get_app_config("trade_pause_until") or 0)
    except Exception:
        until = 0.0
    return until, _gov.halt_reason()


def reword_result(result: dict) -> dict:
    """`scan_auto_execute`'s result, its alert reworded if the pause is why.

    Two exits mean "paused" and said otherwise: the out-of-zone queue
    ("Signal queued ... Will auto-activate when price returns to zone") and
    the in-zone attempt that `open_trade` refused ("Auto-execution failed:
    Trading paused ..."). Both leave the signal `pending`, so both get
    `signal_queued`. A grid template's refused placement is not queued, so it
    gets `signal_not_executed`. Every other reason -- schedule, news, slots --
    is its own true story and is left alone, as is anything executed.
    """
    if result.get("executed"):
        return result
    text = str(result.get("skip_reason") or "")
    refused = "trading paused" in text.lower()
    if text.startswith("Signal queued") or (
            text.startswith("Auto-execution failed:") and refused):
        build = signal_queued
    elif text.startswith("Grid template order failed:") and refused:
        build = signal_not_executed
    else:
        return result
    pause = current_pause()
    if pause is None:
        return result
    return {**result, "skip_reason": build(*pause)}

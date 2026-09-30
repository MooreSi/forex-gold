"""The daily profit goal: stop new entries once today's profit is secured.

Risk > Stopping for the day > Daily goal (owner, 2026-09-29,
docs/todo/risk/020). Once realised P&L since the start of the broker day
reaches the goal, trading stops until the next broker day. Open trades are
left alone; only NEW entries stop. With trades still open the goal is only
HELD, not secured: it becomes the day's halt when the last one closes with the
day still at the goal, and lifts when their losses took it back under (owner,
2026-09-30; `apply_daily_goal`, `lift_hold`).

`pct` is a percentage of the day's OPENING balance -- the balance now minus
what the day has realised -- so the same 2% asks for more dollars as the
account grows. That is what makes it compound over days and weeks, which is
what the owner asked for. `usd` is a fixed amount.

The halt is the same `trade_pause_until` + `risk_halt_reason` pair the
daily-loss limit and the give-back guard write, so `open_trade`'s pause check,
the Resume button and the status line need no special case. Resume restarts
this goal's window (see `governor.rearm_risk_guards`), otherwise the day is
already past the goal and the next check halts again.

**Where it runs.** The other two daily halts are evaluated inside
`record_close`, which is the frozen close path; this one is not allowed there.
It runs from the position monitor cycle instead, at most every
`SWEEP_EVERY_S`, on whichever node is trading. So a close that reaches the goal
halts within one sweep, and an entry that arrives inside that window can still
open. The spec records that limit.

Not to be confused with Trading > Schedule's daily target
(`risk/schedule.py`), which is dollars only and applies only while the trading
schedule is switched on.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.analytics import todays_realised as _todays
from backend.src.services.risk import governor as _gov
from backend.src.services.risk import settings as _risk

log = logging.getLogger(__name__)

__all__ = ["sweep", "SweepState", "progress", "header_progress", "goal_standing"]

# How often the monitor cycle may evaluate the goal. The cycle itself runs
# every 1-5s; the goal reads the day's closes and, in % mode, the account
# balance from the bridge, neither of which needs doing every second.
SWEEP_EVERY_S = 5.0

# Written by `governor.rearm_risk_guards` on a manual Resume.
BASELINE_KEY = "daily_goal_baseline_ts"


def _window_start() -> float:
    """The broker day's start, or the last manual Resume if that is later."""
    day_start = _gov.rg_day_start_ts()
    try:
        baseline = float(db_module.get_app_config(BASELINE_KEY) or 0)
    except (TypeError, ValueError):
        baseline = 0.0
    return max(day_start, baseline)


def _goal_usd(rs: dict, realised: float, balance: Optional[float]) -> Optional[float]:
    """The goal in dollars, or None when it cannot be judged or is off."""
    if not bool(int(rs.get("daily_goal_enabled", 0) or 0)):
        return None
    value = float(rs.get("daily_goal_value", 0) or 0)
    if value <= 0:
        return None
    if str(rs.get("daily_goal_mode") or "pct") == "usd":
        return value
    if balance is None or balance <= 0:
        return None
    day_open = balance - realised
    if day_open <= 0:
        return None
    return day_open * value / 100.0


def check_daily_goal(rs: dict, balance: Optional[float]) -> Optional[str]:
    """The reason to stop for the day, or None."""
    realised, _peak = _gov.day_pnl_and_peak(_window_start())
    goal = _goal_usd(rs, realised, balance)
    if goal is None or realised < goal:
        return None
    if str(rs.get("daily_goal_mode") or "pct") == "usd":
        of = f"${goal:.2f}"
    else:
        of = f"${goal:.2f} ({float(rs['daily_goal_value']):g}% of the day's opening balance)"
    return f"Daily goal secured: +${realised:.2f} today vs a goal of {of}"


def progress(rs: dict, balance: Optional[float],
             realised: Optional[float] = None) -> Optional[dict]:
    """The dashboard's "Today's Goal": the goal and today's realised, in dollars.

    None when the goal is switched off, or is a percentage and the balance is
    unknown (a guessed dollar goal is worse than none). Same window as the
    halt, so the figure and the stop agree. `realised` is the broker's figure
    (MT5, the same one the Calendar shows); without it the local table is used.
    """
    if realised is None:
        realised, _peak = _gov.day_pnl_and_peak(_window_start())
    goal = _goal_usd(rs, realised, balance)
    if goal is None:
        return None
    return {"goal_usd": round(goal, 2), "achieved_usd": round(realised, 2)}


async def header_progress(engine: Any, balance: Optional[float],
                          day: Any) -> Optional[dict]:
    """`progress` for the header poll, on MT5's realised figure.

    None when the goal is off (MT5 is not asked), when MT5 cannot answer (a
    local-table $0.00 would be a wrong answer), or on any failure: a display
    figure on a 5s poll must never take the header down.
    """
    try:
        rs = _risk.get()
        if not bool(int(rs.get("daily_goal_enabled", 0) or 0)):
            return None
        realised = await _todays.for_today(engine, day)
        if realised is None:
            return None
        return progress(rs, balance, realised)
    except Exception:
        return None


HOLD_PREFIX = "Daily goal reached"


def _write_halt(reason: str) -> None:
    until = _gov.rg_day_start_ts() + 86400.0
    with db_module.db():
        db_module.set_app_config("trade_pause_until", str(until))
        db_module.set_app_config("risk_halt_reason", reason)


def _holding() -> bool:
    """Is the halt in force this goal's own hold (not another guard's)?"""
    return _gov.is_trading_paused() and _gov.halt_reason().startswith(HOLD_PREFIX)


def apply_daily_goal(rs: dict, balance: Optional[float],
                     open_positions: Optional[int] = 0) -> bool:
    """Stop new entries once the goal is reached. True if it wrote a halt.

    The goal is SECURED (halt until the next broker day) only when nothing is
    open. Reached with trades still open, it is a HOLD: no new entries, the
    same pause pair with a "waiting" reason. Owner, 2026-09-30: open trades
    that close at a loss can take the day back under the goal, and then it
    should trade on. So the hold becomes the day's halt when the last trade
    closes with the day still at the goal, and lifts (see `lift_hold`) when it
    is not. `open_positions` None means MT5 could not say: hold, never secure.

    Leaves another guard's halt alone: whichever guard stopped the day first
    is the one the operator needs to read.
    """
    reason = check_daily_goal(rs, balance)
    if not reason:
        return False
    if _gov.is_trading_paused() and not _holding():
        return False
    if open_positions == 0:
        _write_halt(reason)
        log.warning("[RG] %s — new entries stopped until the next broker day", reason)
        return True
    waiting = ("open trades" if open_positions is None
               else f"{open_positions} open trade{'s' if open_positions != 1 else ''}")
    hold = f"{HOLD_PREFIX} ({reason.split(': ', 1)[-1]}), waiting for {waiting} to close"
    if _gov.halt_reason() != hold:
        _write_halt(hold)
        log.warning("[RG] %s — no new entries until they do", hold)
    return True


def goal_standing(rs: dict, balance: Optional[float]) -> bool:
    """Is today's goal reached, or its hold still waiting on open trades?

    What `positions/goal_breakeven` protects. The hold counts even after a
    loss took the day back under the goal: the trades it waits for are the
    ones still able to take it further under.
    """
    if not bool(int(rs.get("daily_goal_enabled", 0) or 0)):
        return False
    return _holding() or check_daily_goal(rs, balance) is not None


def lift_hold(rs: dict, balance: Optional[float],
              open_positions: Optional[int]) -> bool:
    """The trades the hold waited for closed and took the day under the goal:
    lift it. True if lifted.

    Only when flat and known flat, only this goal's own hold, and only with a
    balance: while the hold stood, the close-time daily-loss and give-back
    guards saw "already paused" and wrote nothing, so they are re-run here
    exactly as `close_trade` runs them, and the first to fire takes over.
    """
    if open_positions != 0 or balance is None or not _holding():
        return False
    if check_daily_goal(rs, balance):
        return False
    with db_module.db():
        db_module.set_app_config("trade_pause_until", "0")
        db_module.set_app_config("risk_halt_reason", "")
        if bool(rs.get("risk_governor_enabled", 0)):
            _gov.rg_apply_halts_on_close(rs, balance)
        _gov.apply_giveback_guard_on_close(rs)
        _gov.apply_daily_loss_halt_on_close(rs, balance)
    if _gov.is_trading_paused():
        log.warning("[RG] daily goal hold replaced: %s", _gov.halt_reason())
    else:
        log.warning("[RG] open trades closed under the daily goal — trading resumes")
    return True


async def _open_positions(bridge: Any) -> Optional[int]:
    get = getattr(bridge, "get_positions", None)
    if get is None:
        return None
    try:
        positions = await get()
    except Exception:
        return None
    return None if positions is None else len(positions)


@dataclass
class SweepState:
    last_check: float = float("-inf")


async def sweep(state: SweepState, bridge: Any, rs: dict,
                now: Optional[float] = None) -> None:
    """One monitor-cycle evaluation, throttled. Never raises: the monitor
    cycle also manages open trades, and a goal check must not cost that."""
    now = time.time() if now is None else now
    if now - state.last_check < SWEEP_EVERY_S:
        return
    state.last_check = now
    try:
        if not bool(int(rs.get("daily_goal_enabled", 0) or 0)):
            return
        holding = await db_module.to_db_thread(_holding)
        balance = None
        if holding or str(rs.get("daily_goal_mode") or "pct") != "usd":
            acc = await bridge.get_account()
            balance = float((acc or {}).get("balance") or 0) or None
        # All positions on the account, from MT5: the broker is the record of
        # what is open, and counting a manual one only holds for longer.
        open_positions = await _open_positions(bridge)
        if await db_module.to_db_thread(apply_daily_goal, rs, balance, open_positions):
            return
        if holding:
            await db_module.to_db_thread(lift_hold, rs, balance, open_positions)
    except Exception as e:
        log.debug("[DailyGoal] sweep failed: %s", e)

"""Scheduled illiquidity: the windows where the spread is the trade.

Section 5.6 of `docs/todo/reversal-engine/200`. `utils/news_calendar.py`
already tiers events by impact, weights them for gold and blacks out around
them. What nothing covers is illiquidity that arrives on a CLOCK rather than
a calendar:

  * **the daily rollover**, when swap is applied, the book thins and gold
    spreads routinely go to several times normal
  * **the weekend reopen**, which carries the gap and the widest spreads of
    the week
  * **month and quarter end**, where a large part of the flow is rebalancing
    rather than price discovery

None of these is news and none of them is visible to a news filter. All are
pure functions of the clock, which is why they belong here and not in the
calendar.

**Config() describes the windows; it does not decide whether they are
enforced.** The app-level toggle that turns this check on is separate and
defaults off, so installing this module changes nothing about what trades.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(frozen=True)
class Config:
    rollover_enabled: bool = True
    # Broker rollover on this feed sits at 22:00 UTC. The window opens a few
    # minutes early because the spread starts widening before the print.
    rollover_start_hhmm: tuple[int, int] = (21, 55)
    rollover_end_hhmm: tuple[int, int] = (22, 10)

    weekend_reopen_enabled: bool = True
    # From Sunday 21:00 UTC until this many hours into Monday.
    reopen_start_hhmm: tuple[int, int] = (21, 0)
    reopen_settle_hours: float = 4.0

    # Off by default: it costs whole trading days, and the evidence that
    # month-end flow hurts THIS strategy has not been gathered yet. Turn it
    # on when the attribution says to.
    period_end_enabled: bool = False
    period_end_from_hour: int = 12
    quarter_end_only: bool = False


def _minutes(dt: datetime) -> int:
    return dt.hour * 60 + dt.minute


def _hhmm(pair: tuple[int, int]) -> int:
    return pair[0] * 60 + pair[1]


def _is_last_day_of_month(dt: datetime) -> bool:
    return (dt + timedelta(days=1)).month != dt.month


def check(now_ts: float, cfg: Config) -> tuple[bool, str]:
    """`(allowed, reason)`, the same contract as `check_news_blackout` and
    `check_trading_schedule`, so a call site that already gates on those can
    gate on this in the same two lines."""
    dt = datetime.fromtimestamp(float(now_ts), tz=timezone.utc)
    mins = _minutes(dt)

    # The reopen is checked first because it overlaps the rollover window,
    # and "the week just opened" is the more useful thing to be told.
    if cfg.weekend_reopen_enabled:
        start = _hhmm(cfg.reopen_start_hhmm)
        settle_mins = cfg.reopen_settle_hours * 60
        if dt.weekday() == 6 and mins >= start:
            return False, ("Weekend reopen - the first hours of the week carry "
                           "the gap and the widest spreads")
        if dt.weekday() == 0:
            since_open = (24 * 60 - start) + mins
            if since_open <= settle_mins:
                return False, ("Weekend reopen - still inside the settle "
                               "window after Sunday's open")

    if cfg.rollover_enabled:
        if _hhmm(cfg.rollover_start_hhmm) <= mins < _hhmm(cfg.rollover_end_hhmm):
            return False, ("Daily rollover - the book is at its thinnest and "
                           "the spread at its widest")

    if cfg.period_end_enabled and dt.hour >= cfg.period_end_from_hour:
        if _is_last_day_of_month(dt):
            is_quarter = dt.month in (3, 6, 9, 12)
            if is_quarter or not cfg.quarter_end_only:
                return False, ("Period end - a large share of this flow is "
                               "rebalancing rather than price discovery")

    return True, ""

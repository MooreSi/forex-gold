"""The Telegram panel's Balance screen: account state plus realised P&L over
today, this week broken out by day, and this month rolled up.

Replaces the old split where Balance showed three account numbers and a
separate Daily button showed today's P&L. Two buttons for one question ("how
is the account doing") meant neither answered it: Balance had no history at
all, and Daily could not say whether today was normal for the week.

Realised P&L is bucketed by CLOSE time, not open time -- a trade opened
Thursday and closed Friday is Friday's result, because that is the day the
money actually moved. (core_trading_schedule buckets by open_time instead,
deliberately: its windows gate *entries*, so a window owns the trades it
started.)

Per-trade P&L prefers mt5_profit, the broker's own figure including swap and
commission, and falls back to net_pnl only when the broker has not reported
one -- same rule the daily summary used.

Deliberately NOT reported: an end-of-day account balance per day. Nothing
stores a daily balance snapshot, so it could only be derived by walking the
current balance back through trade history, which silently goes wrong the
moment money is deposited or withdrawn mid-week. Realised P&L per day is
exact and needs no such assumption.

Read-only: nothing here places, closes or modifies an order.
"""
from __future__ import annotations

import logging
from backend.src.services.risk import clock as _clock
from datetime import datetime, timedelta
from typing import Any

from backend.src.db import database as db_module
from backend.src.services.trading.fees_sizing import pnl as _pnl
from backend.src.services.trading.sim_account import get_sim_account
from backend.src.services.analytics.reporting import get_open_trades

log = logging.getLogger(__name__)

_DAY_LABEL = "%a %d %b"


def _signed(value: float) -> str:
    return f"{'+' if value >= 0 else '-'}${abs(value):,.2f}"


def _trade_pnl(trade: dict) -> float:
    raw = trade.get("mt5_profit")
    return float(raw if raw is not None else trade.get("net_pnl", 0) or 0)


def _day_start(moment: datetime) -> datetime:
    return moment.replace(hour=0, minute=0, second=0, microsecond=0)


def closed_since(cutoff: float) -> list[dict]:
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT close_time, net_pnl, mt5_profit FROM vantage_simulated_trades "
            "WHERE status='closed' AND close_time >= ? ORDER BY close_time",
            (cutoff,),
        ).fetchall()
    return [db_module.row_to_dict(r) for r in rows]


class _Bucket:
    """Running total for one period."""

    __slots__ = ("pnl", "count", "wins", "losses")

    def __init__(self) -> None:
        self.pnl = 0.0
        self.count = 0
        self.wins = 0
        self.losses = 0

    def add(self, value: float) -> None:
        self.pnl += value
        self.count += 1
        if value > 0:
            self.wins += 1
        elif value < 0:
            self.losses += 1

    def summary(self) -> str:
        """'+$30.00  (3 trades, 2W/1L, 67%)' -- or a dash when nothing closed."""
        if not self.count:
            return "—"
        rate = round(self.wins / self.count * 100)
        return (f"{_signed(self.pnl)}  ({self.count} "
                f"trade{'s' if self.count != 1 else ''}, "
                f"{self.wins}W/{self.losses}L, {rate}%)")


def period_totals(now: datetime | None = None) -> dict:
    """Realised P&L bucketed into today, each day of this week, the week, and
    the month. One query covers all four -- the month always starts on or
    before the week for any date except when a week spans a month boundary,
    which is why the cutoff is the earlier of the two."""
    now = now or _clock.now()
    today_start = _day_start(now)
    week_start = today_start - timedelta(days=now.weekday())   # Monday
    month_start = today_start.replace(day=1)
    cutoff = min(week_start, month_start)

    days = {week_start + timedelta(days=i): _Bucket() for i in range(7)}
    week, month, today = _Bucket(), _Bucket(), _Bucket()

    # _clock.to_timestamp, not cutoff.timestamp(): `cutoff` is trading-clock
    # wall time, and a naive .timestamp() always reads it as the MACHINE's
    # zone -- which is wrong on any machine given an explicit offset.
    for trade in closed_since(_clock.to_timestamp(cutoff)):
        closed_at = float(trade.get("close_time") or 0)
        if not closed_at:
            continue
        value = _trade_pnl(trade)
        # Close times are stored as epoch seconds. They have to be converted
        # on the same clock the boundaries use, or the buckets are one
        # machine's days wearing another's labels.
        stamp = _clock.from_timestamp(closed_at)
        day = _day_start(stamp)
        if day in days:
            days[day].add(value)
            week.add(value)
        if stamp >= month_start:
            month.add(value)
        if day == today_start:
            today.add(value)

    return {
        "now": now, "today": today, "week": week, "month": month,
        "days": days, "week_start": week_start, "month_start": month_start,
    }


def _week_lines(totals: dict) -> list[str]:
    today_start = _day_start(totals["now"])
    lines = []
    for day, bucket in sorted(totals["days"].items()):
        label = day.strftime(_DAY_LABEL)
        # A day still to come and a day that traded nothing both have no P&L,
        # but they are not the same news -- printing one dash for both would
        # read Sunday's "hasn't happened" as "flat".
        body = "to come" if day > today_start else bucket.summary()
        marker = "   ← today" if day == today_start else ""
        lines.append(f"{label}:  {body}{marker}")
    return lines


async def build_balance_report(bridge: Any, now: datetime | None = None) -> str:
    """The whole Balance screen."""
    account = None
    try:
        account = await bridge.get_account()
    except Exception as e:
        log.debug("[Balance] account unavailable: %s", e)

    if account and float(account.get("balance") or 0) > 0:
        balance = float(account.get("balance", 0))
        equity = float(account.get("equity", 0))
        free_margin = float(account.get("margin_free", 0))
        mode = "MT5 Live" if db_module.get_app_config("account_env") == "live" else "MT5 Demo"
    else:
        balance = float(get_sim_account().get("balance", 0))
        equity = free_margin = balance
        mode = "Simulation"

    open_trades = get_open_trades()
    open_pnl = 0.0
    try:
        tick = await bridge.get_tick()
    except Exception:
        tick = None
    if tick:
        for t in open_trades:
            open_pnl += _pnl(
                t["direction"], float(t["entry_price"]),
                tick.bid if t["direction"] == "BUY" else tick.ask,
                float(t["remaining_lots"]),
            )

    totals = period_totals(now)
    now = totals["now"]

    lines = [
        f"*XAUUSD FOREX Trader — {mode}*",
        "",
        "*Account*",
        f"Balance:     ${balance:,.2f}",
        f"Equity:      ${equity:,.2f}",
        f"Free Margin: ${free_margin:,.2f}",
    ]
    if open_trades:
        n = len(open_trades)
        lines.append(f"Open P&L:    {_signed(open_pnl)}  "
                     f"({n} trade{'s' if n != 1 else ''})")

    lines += [
        "",
        f"*Today — {now.strftime(_DAY_LABEL)}*",
        totals["today"].summary(),
        "",
        f"*This Week — from {totals['week_start'].strftime(_DAY_LABEL)}*",
    ]
    lines += _week_lines(totals)
    lines.append(f"*Week total:*  {totals['week'].summary()}")

    lines += [
        "",
        f"*This Month — {now.strftime('%B %Y')}*",
        f"*Month total:* {totals['month'].summary()}",
    ]
    return "\n".join(lines)

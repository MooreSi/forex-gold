"""The Analysis tab's equity curve, drawn from the trade table's own rows.

**Realised P&L over the window, not account equity.** Calling it equity and
starting it at the account balance would mean inventing where the account
stood when the window opened, and that figure is nowhere in the deal history.
A curve whose zero is a guess can be read as a loss when the account never
moved. The header's whole-life P&L answers "where is the account overall";
this answers "what did the trading do over these N days".

It takes the rows `trade_table.closed_trades` already produced rather than
reading the broker again. Two independent reads of a moving account are two
chances for the curve and the table beneath it to disagree on screen, and a
disagreement there is the kind of thing that gets both distrusted.

Nothing here reads the broker, the database, or the clock.
"""
from __future__ import annotations

from typing import Any

__all__ = ["build"]


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def build(rows: list[dict]) -> dict:
    """`{points, net, peak, max_drawdown, trades}` from closed-trade rows.

    `points` starts at zero, so the curve has a baseline to be read against,
    and then carries one point per trade in time order. The table above it
    sorts newest-first; a curve drawn in that order runs backwards and looks
    like a mirror image of the month.

    Drawdown is measured from the running PEAK, not from zero. Up 80 and back
    to 30 is a 50 drawdown, not a 30 profit with nothing wrong.
    """
    usable = []
    for row in rows or []:
        ts = _number(row.get("close_ts"))
        pnl = _number(row.get("pnl"))
        # A close time of 0 is a row whose closing deal carried no timestamp.
        # Placed on the axis it lands in 1970 and drags the whole curve flat.
        if not ts or pnl is None:
            continue
        usable.append((ts, pnl))

    if not usable:
        # An empty curve, not a flat line at zero: a flat line reads as
        # "traded all month and broke even".
        return {"points": [], "net": 0.0, "peak": 0.0,
                "max_drawdown": 0.0, "trades": 0}

    usable.sort(key=lambda p: p[0])

    running = 0.0
    peak = 0.0
    max_dd = 0.0
    points = [{"ts": usable[0][0], "pnl": 0.0}]

    for ts, pnl in usable:
        running += pnl
        peak = max(peak, running)
        max_dd = max(max_dd, peak - running)
        points.append({"ts": ts, "pnl": round(running, 2)})

    return {
        "points": points,
        "net": round(running, 2),
        "peak": round(peak, 2),
        "max_drawdown": round(max_dd, 2),
        "trades": len(usable),
    }

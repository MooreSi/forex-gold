"""Transaction cost analysis: what a fill actually cost, measured.

Section 5.1 of `docs/todo/reversal-engine/200`.

The app has never measured execution cost. `services/trading/fees_sizing.py`
charges `estimated_slippage_points` -- a constant from fee settings, default
5.0 -- to every trade regardless of what happened, and the spread it charges
is whatever the caller passed in at open. Nothing compares the price asked for
with the price received.

That is not a rounding error on this instrument. The reversal engine's mean
stop is 5.75 points; a 0.6 point round trip is over 10% of R, and
`reversal-engine/020` is trying to explain 0.53 points of leakage per loss
with no measurement of this at all.

**Unmeasured is not free.** Every field here is `None` when the tick that
would have priced it is missing, and `summarise` counts those separately
rather than averaging them in as zero. An average cost that quietly includes
free fills is the wrong number to carry into a decision about stop width.

Measures only. Nothing here changes an order, a size or a stop.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Iterable, Optional

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FillCost:
    trade_id: str
    direction: str
    open_time: float
    requested_price: float
    fill_price: float
    slippage_pts: Optional[float]
    # The two halves of slippage_pts, which they sum to exactly.
    broker_slippage_pts: Optional[float]
    entry_drift_pts: Optional[float]
    spread_open_pts: Optional[float]
    spread_close_pts: Optional[float]
    spread_cost_pts: Optional[float]
    cost_pts: Optional[float]
    cost_r: Optional[float]
    fill_delay_s: Optional[float]
    measured: bool
    bucket: str = ""


def slippage_pts(direction: str, requested: float, filled: float) -> float:
    """Signed, in points: POSITIVE means the fill was worse than asked for.

    Signed rather than absolute because favourable slippage is real and
    common on a resting order. Reporting only the adverse half would
    overstate cost, which misleads in the opposite direction to ignoring it
    but just as effectively.
    """
    sign = 1.0 if str(direction).upper() == "BUY" else -1.0
    return round((float(filled) - float(requested)) * sign, 5)


def quoted_side(tick: Optional[dict], direction: str) -> Optional[float]:
    """The price the broker was quoting for THIS side at that instant.

    A buy is filled at the ask and a sell at the bid. Comparing a fill
    against the mid, or against the wrong side, manufactures half a spread
    of slippage that never happened.
    """
    if not tick:
        return None
    try:
        bid = float(tick.get("bid") or 0.0)
        ask = float(tick.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    if bid <= 0 or ask <= 0:
        return None
    return ask if str(direction).upper() == "BUY" else bid


def _spread(tick: Optional[dict]) -> Optional[float]:
    if not tick:
        return None
    try:
        bid = float(tick.get("bid") or 0.0)
        ask = float(tick.get("ask") or 0.0)
    except (TypeError, ValueError):
        return None
    if bid <= 0.0 or ask <= 0.0 or ask < bid:
        return None
    return round(ask - bid, 5)


async def _tick_at(bridge, ts: Optional[float]) -> Optional[dict]:
    if not ts:
        return None
    try:
        return await bridge.get_tick_at(float(ts))
    except Exception as e:                       # noqa: BLE001
        log.debug("tca: tick_at(%s) failed: %s", ts, e)
        return None


async def measure(bridge, trade: dict, requested_price: float,
                  sl_dist: float, decision_ts: Optional[float] = None,
                  bucket: str = "") -> FillCost:
    """Price one round trip against the ticks that were actually quoted.

    `requested_price` is the price the decision was made at, not the order's
    limit: the question is what the round trip cost relative to the moment
    the system chose to act.
    """
    direction = str(trade.get("direction") or "BUY")
    open_time = float(trade.get("open_time") or 0.0)
    close_time = trade.get("close_time")
    fill = float(trade.get("entry_price") or 0.0)

    open_tick = await _tick_at(bridge, open_time)
    close_tick = await _tick_at(bridge, close_time)
    s_open = _spread(open_tick)
    s_close = _spread(close_tick)

    slip = slippage_pts(direction, requested_price, fill) if requested_price else None

    # One number, two causes, two different fixes. Broker slippage is the
    # fill against what was actually being quoted; entry drift is that
    # quote against the price the decision was made at -- not the broker's
    # doing at all, but the signal chasing, arriving late, or firing at
    # market outside its own zone. They sum to `slip` by construction.
    quoted = quoted_side(open_tick, direction)
    broker_slip = entry_drift = None
    if quoted is not None and fill > 0:
        broker_slip = slippage_pts(direction, quoted, fill)
        if requested_price:
            entry_drift = slippage_pts(direction, requested_price, quoted)

    # Buy at the ask, sell at the bid. Against a mid-to-mid benchmark that is
    # half a spread each way, not a full spread twice.
    spread_cost = None
    if s_open is not None and s_close is not None:
        spread_cost = round((s_open + s_close) / 2.0, 5)

    cost_pts = None
    if spread_cost is not None and slip is not None:
        cost_pts = round(spread_cost + slip, 5)

    cost_r = None
    if cost_pts is not None and float(sl_dist or 0.0) > 0:
        cost_r = round(cost_pts / float(sl_dist), 5)

    delay = None
    if decision_ts and open_time:
        delay = round(open_time - float(decision_ts), 3)

    return FillCost(
        trade_id=str(trade.get("trade_id") or ""), direction=direction,
        open_time=open_time, requested_price=float(requested_price or 0.0),
        fill_price=fill, slippage_pts=slip,
        broker_slippage_pts=broker_slip, entry_drift_pts=entry_drift,
        spread_open_pts=s_open,
        spread_close_pts=s_close, spread_cost_pts=spread_cost,
        cost_pts=cost_pts, cost_r=cost_r, fill_delay_s=delay,
        measured=cost_pts is not None, bucket=bucket,
    )


def summarise(costs: Iterable[FillCost]) -> dict:
    """Mean cost per bucket, with unmeasured fills counted and excluded.

    The `unmeasured` count is the honesty check on every other number in the
    row: a bucket whose cost is built from three of forty fills is not a
    measurement of that bucket.
    """
    out: dict[str, dict] = {}
    for c in costs:
        row = out.setdefault(c.bucket, {"n": 0, "unmeasured": 0,
                                        "_cost_r": 0.0, "_slip": 0.0,
                                        "_spread": 0.0, "_broker": 0.0,
                                        "_drift": 0.0})
        if not c.measured or c.cost_r is None:
            row["unmeasured"] += 1
            continue
        row["n"] += 1
        row["_cost_r"] += c.cost_r
        row["_slip"] += c.slippage_pts or 0.0
        row["_spread"] += c.spread_cost_pts or 0.0
        row["_broker"] += c.broker_slippage_pts or 0.0
        row["_drift"] += c.entry_drift_pts or 0.0

    for row in out.values():
        n = row["n"]
        row["mean_cost_r"] = round(row.pop("_cost_r") / n, 5) if n else None
        row["mean_slippage_pts"] = round(row.pop("_slip") / n, 5) if n else None
        row["mean_spread_pts"] = round(row.pop("_spread") / n, 5) if n else None
        row["mean_broker_slippage_pts"] = round(row.pop("_broker") / n, 5) if n else None
        row["mean_entry_drift_pts"] = round(row.pop("_drift") / n, 5) if n else None
    return out

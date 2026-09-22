"""Closed trades, deal by deal, as the Analysis table shows them.

Built from **MT5's own deal history**, not the local ledger. A trade opened by
hand in the terminal, or by the copier EA, never had a local row, and a table
built from the local ledger would silently omit it while looking complete.

The local database is still where the attribution comes from: which channel a
ticket came from, which strategy ran it, how far the TP ladder actually
reached, its R:R, whether it was a resting order and how long it sat, and which
EA-template group its legs belong to. `ticket_maps` builds those, and this
merges them onto the deals.

**A position is many deals.** One entry deal and one or more exits, which is
what a partial close looks like from the broker's side, so the lots column
shows the breakdown. A position with no entry deal in the window is still
reported -- it was opened before the window started, and dropping it would make
a long-running trade disappear from the day it closed.

`comment_attribution_maps` fills the gaps: positions with no local row of their
own are attributed from the comment the opening order carried, with
`setdefault` so a real local row always wins over an inference.

Nothing here places, closes or modifies anything. It is the read behind one
table.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from backend.src.services.analytics import equity_curve as _curve
from backend.src.services.analytics import formatting as _formatting
from backend.src.services.analytics import labels as _labels
from backend.src.services.analytics import pnl as _pnl
from backend.src.services.analytics import ticket_maps as _maps
from backend.src.services.broker import fees as _fees
from backend.src.services.positions import spread_cache_repo as _spreads
from backend.src.utils.models import CONTRACT_SIZE

log = logging.getLogger(__name__)

__all__ = ["closed_trades"]

# MT5 deal `entry` codes: 0 opens a position, 1/2/3 close or reverse it.
_ENTRY_IN = 0
_ENTRY_OUT = (1, 2, 3)

# A Max TP result is only computed 30 minutes after a close, so a blank cell
# before that is "not yet", not "none". See `_max_tp_cell`.
_MAX_TP_DELAY_SECS = 1800

# Vantage XAUUSD: one pip is a $0.10 price move.
_PIP = 10.0

# The broker stores UTC+3 timestamps as-if-UTC. Imported rather than repeated
# so a row's session and the calendar day it is filed under can never be
# derived from two different readings of the same clock.
_BROKER_OFFSET = _formatting.BROKER_OFFSET


def _session(close_ts: float) -> Optional[str]:
    """Which trading session a close happened in, or None with no stamp.

    None rather than a default: a trade with no close time bucketed into
    whichever session contains hour zero is a quiet session made to look busy,
    and the day view says how many rows it could not place instead.

    The boundaries themselves are `analytics.pnl.session_for_hour`, the same
    ones the hourly heat map reads. A second copy of them -- here or in the
    browser -- would be a second answer to where London ends.
    """
    if not close_ts:
        return None
    try:
        hour = datetime.fromtimestamp(
            float(close_ts) - _BROKER_OFFSET, tz=timezone.utc).hour
    except (TypeError, ValueError, OSError, OverflowError):
        return None
    return _pnl.session_for_hour(hour)


def _direction_and_entry(open_deal: Optional[dict], close_deal: dict) -> tuple[str, float, float]:
    """(direction, entry price, opening lots).

    With no opening deal in the window the direction is inferred from the
    CLOSING deal's type, inverted: a position closed by a sell was a buy.
    """
    if open_deal:
        return ("BUY" if int(open_deal.get("type", 0)) == 0 else "SELL",
                float(open_deal.get("price", 0)),
                float(open_deal.get("volume", 0)))
    return ("SELL" if int(close_deal.get("type", 0)) == 0 else "BUY",
            0.0,
            float(close_deal.get("volume", 0)))


def _max_tp_cell(raw: Optional[str], close_ts: float, now: float) -> str:
    """"" while the 30-minute window is still open, "..." once it has elapsed
    and the sweep has not caught up, else the computed answer.

    Three states, not two: a blank and a "none" mean different things and an
    operator reading "none" too early would conclude a trade never went their
    way when nothing has looked yet.
    """
    if raw is not None:
        return raw
    if close_ts and (now - close_ts) >= _MAX_TP_DELAY_SECS:
        return "..."
    return ""


def _empty(error: str) -> dict:
    """The shape every caller gets, with nothing in it. The curve is present
    and empty rather than absent: a missing key is a crash in the browser."""
    return {"rows": [], "error": error, "curve": _curve.build([])}


async def closed_trades(engine: Any, days: int) -> dict:
    """`{rows, error, curve}` — one row per closed position over `days`.

    `error` is a string when the broker could not be reached, and the rows are
    empty. The two are separate because "no trades in this window" and "the
    bridge is down" look identical in an empty table and call for completely
    different responses.
    """
    try:
        deals = await engine.get_deal_history(days)
    except Exception as exc:
        log.warning("[trade_table] deal history unavailable: %s", exc)
        return _empty(f"MT5 deal history is unavailable: {exc}")
    if deals is None:
        return _empty("MT5 deal history is unavailable.")

    by_position: dict[int, list[dict]] = {}
    for deal in deals:
        pid = deal.get("position_id")
        # None and 0 are balance operations and deposits, not trades.
        if pid:
            by_position.setdefault(int(pid), []).append(deal)

    source = await _maps.source_map(days)
    strategy = await _maps.strategy_map(days)
    max_tp = await _maps.max_tp_map()
    rr = await _maps.rr_map()
    order_type = await _maps.order_type_map(days)
    group = await _maps.group_map()

    # Positions with no local row: attribute from the opening order's comment.
    # setdefault so a real local row always wins over an inference.
    leg_comments = {
        str(ticket): (next((d for d in rows if d.get("entry") == _ENTRY_IN), {})
                      .get("comment") or "")
        for ticket, rows in by_position.items()
    }
    leg_comments = {k: v for k, v in leg_comments.items() if v}
    if leg_comments:
        try:
            c_src, c_strat, c_max_tp = await _maps.comment_attribution_maps(leg_comments)
            for store, extra in ((source, c_src), (strategy, c_strat), (max_tp, c_max_tp)):
                for ticket, value in (extra or {}).items():
                    store.setdefault(ticket, value)
        except Exception as exc:
            log.debug("[trade_table] comment attribution unavailable: %s", exc)

    spreads = _spreads.get_cached_spreads(list(by_position.keys()))
    fee_rate = await _fees.platform_fee_rate()
    now = time.time()

    rows: list[dict] = []
    for ticket, pos_deals in by_position.items():
        closes = [d for d in pos_deals if d.get("entry") in _ENTRY_OUT]
        if not closes:
            # Still open. The open-positions panel owns those.
            continue
        close_deal = max(closes, key=lambda d: d.get("time", 0))
        open_deal = next((d for d in pos_deals if d.get("entry") == _ENTRY_IN), None)

        direction, entry, open_lots = _direction_and_entry(open_deal, close_deal)
        exit_price = float(close_deal.get("price", 0))
        close_ts = float(close_deal.get("time", 0))
        open_ts = float(open_deal.get("time", 0)) if open_deal else 0.0
        pnl, fees = _fees.apply_fee(pos_deals, open_lots, fee_rate)

        kind, pending_at = order_type.get(str(ticket), ("market", None))
        cached = spreads.get(ticket) or {}
        if cached:
            # Already embedded in pnl via MT5's real fill prices; shown as a
            # cost breakdown, never deducted again.
            fees = cached.get("spread_cost_usd", fees)

        rows.append({
            "ticket": ticket,
            "direction": direction,
            "entry_price": entry,
            "exit_price": exit_price,
            "open_ts": open_ts,
            "close_ts": close_ts,
            "lots": open_lots,
            # A partial close is several exit deals. The browser shows the
            # breakdown; it cannot reconstruct it from a single number.
            "close_lots": [float(d.get("volume", 0)) for d in closes],
            "pnl": pnl,
            "fees": fees,
            "pips": ((exit_price - entry) if direction == "BUY" else (entry - exit_price)) * _PIP
                    if entry and exit_price else None,
            "duration_secs": (close_ts - open_ts) if open_ts and close_ts else None,
            "order_type": "Limit" if kind == "limit" else "Market",
            "pending_secs": (open_ts - pending_at) if pending_at and open_ts else None,
            "reason": _labels.parse_reason(close_deal.get("comment") or "", pnl),
            # Which market was open when it closed. The calendar's day view
            # totals by this as well as by signal source -- asked for on
            # 2026-09-22, and the question the Asian-hours losses raised.
            "session": _session(close_ts),
            "source": source.get(str(ticket), ""),
            "strategy": strategy.get(str(ticket), ""),
            "max_tp": _max_tp_cell(max_tp.get(str(ticket)), close_ts, now),
            "rr": rr.get(str(ticket)),
            "spread_points": cached.get("spread_points"),
            # (group name, leg number) for an EA-template grid, so the table
            # can fold sibling legs under one row.
            "group": list(group.get(str(ticket))) if group.get(str(ticket)) else None,
            "contract_size": CONTRACT_SIZE,
        })

    rows.sort(key=lambda r: r["close_ts"], reverse=True)
    # The curve is built from these same rows rather than from a second read.
    # Two independent reads of a moving account are two chances for the curve
    # and the table beneath it to disagree on screen.
    return {"rows": rows, "error": None, "curve": _curve.build(rows)}

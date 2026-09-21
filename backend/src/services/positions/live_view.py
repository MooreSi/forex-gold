"""The Positions table's rows: the app's record, plus the broker's live truth.

Two things this exists to fix, both reported by the owner on 2026-09-21.

**A running P&L.** `vantage_simulated_trades.mt5_profit` is written when a
trade CLOSES -- by `record_close`, the history importer and the profit sync.
While a position is open it is null, so the Positions table's P&L column showed
an em dash on every row. The broker's `/positions` payload already carries a
running `profit` per ticket; this places it on the row as `pnl`.

**The position that was not there.** The table is built from
`status='open'` rows -- the app's own record. A position opened by hand in
MetaTrader, or one whose record was lost, is open at the broker and invisible
here. The operator sees one row and MT5 shows two, with nothing saying which
is right.

Two rules, and both are about not making a display change into a money change:

* **`mt5_profit` is never overwritten.** It is the realised figure, and
  closing, the balance report and the history importer all read it. The running
  number goes on `pnl`, a display field, and nothing persists it.
* **An untracked position is shown and cannot be closed from here.** It has no
  `trade_id` to close against and no record to update afterwards, so it carries
  `untracked: True` and no id at all. The frontend disables the control; the
  missing id is the backstop behind that.

Nothing here places, closes or sizes anything. It is one GET against the
bridge, merged with one read the app already made.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

log = logging.getLogger(__name__)

__all__ = ["build"]


def _ticket(value: Any) -> Optional[int]:
    """A ticket as an int, or None.

    Tickets arrive as ints from MT5 and as whatever SQLite stored on the other
    side. A row with a blank or malformed one simply does not match -- raising
    here would empty the whole table over one bad row.
    """
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number or None


def _running_pnl(position: dict) -> Optional[float]:
    """Profit plus swap: what the position is worth to the account right now.

    Swap is included because MT5's own Profit column includes it, and a figure
    here that disagrees with the terminal is worse than no figure.
    """
    try:
        return round(float(position.get("profit") or 0.0)
                     + float(position.get("swap") or 0.0), 2)
    except (TypeError, ValueError):
        return None


async def _live_positions(bridge: Any) -> list[dict]:
    """Whatever the broker says is open, or an empty list.

    A bridge that is not configured, answers None, or raises costs this view
    its P&L column and its untracked rows -- never the table. The app's own
    record is still worth showing when the broker cannot be reached.
    """
    if bridge is None or not bridge.is_configured():
        return []
    try:
        return list(await bridge.get_positions() or [])
    except Exception as exc:
        log.warning("[positions] could not read live positions: %s", exc)
        return []


def _untracked_row(position: dict) -> dict:
    """A broker position the app has no record of, in the table's own columns.

    Named under the stored column names rather than the short ones because
    `ChartTrade` fills the short names from these, and a row that spoke only
    the short names would be the one row on the screen that behaved
    differently.
    """
    return {
        "trade_id": None,
        "mt5_ticket": _ticket(position.get("ticket")),
        "symbol": position.get("symbol"),
        "direction": str(position.get("type") or "").upper() or None,
        "lot_size": position.get("volume"),
        "entry_price": position.get("open_price"),
        "stop_loss": position.get("sl"),
        "tp1": position.get("tp"),
        "open_time": position.get("open_time"),
        "status": "open",
        "pnl": _running_pnl(position),
        "untracked": True,
        "strategy_label": "—",
        # Says where it came from. An em dash here is the least useful thing
        # this table could report about a position that appeared out of
        # nowhere.
        "source_label": "Opened in MT5 (not tracked)",
    }


async def build(open_trades: list[dict], bridge: Any) -> list[dict]:
    """The rows the Positions table draws, tracked first then untracked.

    `open_trades` is `analytics.reporting.get_open_trades()` -- passed in
    rather than read here, so this stays one function over two inputs and the
    caller keeps deciding which account's records it is looking at.
    """
    live = await _live_positions(bridge)
    by_ticket = {}
    for position in live:
        number = _ticket(position.get("ticket"))
        if number is not None:
            by_ticket[number] = position

    rows = []
    claimed = set()
    for trade in open_trades:
        row = dict(trade)
        number = _ticket(row.get("mt5_ticket"))
        position = by_ticket.get(number) if number is not None else None
        if position is not None:
            claimed.add(number)
            row["pnl"] = _running_pnl(position)
            row["live_price"] = position.get("current_price")
        else:
            # Explicitly null, never 0.0 -- a zero reads as break-even, which
            # is a number an operator would act on.
            row["pnl"] = None
        row["untracked"] = False
        rows.append(row)

    for number, position in by_ticket.items():
        if number not in claimed:
            rows.append(_untracked_row(position))
    return rows

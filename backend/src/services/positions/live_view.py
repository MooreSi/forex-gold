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
from typing import Any, Callable, Optional

log = logging.getLogger(__name__)

# ticket -> the other node's record of that position, or None.
RemoteLookup = Callable[[Any], Optional[dict]]

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


def _paired_node_lookup(ticket: Any) -> Optional[dict]:
    """The VPS's record of `ticket`, from the last sync heartbeat.

    Reads the sync client only if one already exists. `get_instance()` would
    build one on an install that has never been paired, which is not a side
    effect a positions table is entitled to.
    """
    from backend.src.services.cluster.sync import client as _sync_client
    client = getattr(_sync_client, "_instance", None)
    if client is None:
        return None
    return client.get_remote_open_position(ticket)


def _remote_record(lookup: RemoteLookup, ticket: Optional[int]) -> Optional[dict]:
    """The other node's record of a position, or None -- never an exception.

    A failed lookup costs the row its enrichment and nothing else: it is
    still drawn as untracked, which is what it was before this existed.
    """
    if ticket is None:
        return None
    try:
        return lookup(ticket) or None
    except Exception as exc:
        log.debug("[positions] remote lookup for %s failed: %s", ticket, exc)
        return None


def _remote_row(position: dict, remote: dict) -> dict:
    """A position the paired node opened, told apart from a stranger's.

    The Mac and the VPS share one MT5 account, so everything the active VPS
    opens is open at the broker with no row here. The NiceGUI panels looked
    the ticket up in the sync heartbeat and drew the VPS's detail; without
    that, every VPS trade reads "Opened in MT5 (not tracked)" and the operator
    is told to close it in MetaTrader.

    Only the labels come from the heartbeat. Price, lots, stop and P&L stay the
    broker's -- the heartbeat is the VPS's own record and up to 3 s old. And the
    row stays `untracked` with no `trade_id`: the VPS holds the record and
    manages the position, so there is nothing on THIS node to close against.
    """
    from backend.src.services.analytics import labels as _labels

    row = _untracked_row(position)
    source = _labels.trade_source_label(remote.get("tg_source") or "")
    row.update({
        "remote": True,
        "strategy_label": _labels.strategy_display_label(remote.get("strategy") or ""),
        "source_label": f"Remote node: {source}",
        "tg_source": remote.get("tg_source"),
        "remote_trade_id": remote.get("trade_id"),
        "triggered_tps": list(remote.get("triggered_tps") or []),
    })
    return row


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
        "remote": False,
        "strategy_label": "—",
        # Says where it came from. An em dash here is the least useful thing
        # this table could report about a position that appeared out of
        # nowhere.
        "source_label": "Opened in MT5 (not tracked)",
    }


async def build(open_trades: list[dict], bridge: Any,
                remote_lookup: RemoteLookup = _paired_node_lookup) -> list[dict]:
    """The rows the Positions table draws, tracked first then untracked.

    `open_trades` is `analytics.reporting.get_open_trades()` -- passed in
    rather than read here, so this stays one function over two inputs and the
    caller keeps deciding which account's records it is looking at.

    `remote_lookup` finds the paired node's record of a position this node has
    none of. The default reads the sync heartbeat; tests pass their own.
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
            remote = _remote_record(remote_lookup, number)
            rows.append(_remote_row(position, remote) if remote
                        else _untracked_row(position))
    return rows

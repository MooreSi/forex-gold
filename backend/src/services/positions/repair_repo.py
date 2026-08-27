"""SQL for the EA Template placeholder repair (core_template_placeholder_repair).

A placeholder is a row this app wrote for a template leg whose fill event never
reached this node: status open, no ticket, entry_price 0. Repair either adopts
it onto a live leg found at the broker, or writes the real fill onto it before
closing it out.

Both writes land the numbers a P&L is later computed from, so the selection
criteria and the guard on the adopt are the point of this module rather than
incidental detail. Statements moved verbatim from the service.
"""
from __future__ import annotations

from backend.src.db.database import db, row_to_dict


def fetch_template_placeholders() -> list[dict]:
    """Open rows carrying the placeholder defect signature.

    entry_price=0 is the distinguishing half: a row with no ticket but a real
    entry price is a legitimately ticket-less simulated trade, not an
    unpromoted EA Template placeholder, and adopting it onto a broker leg
    would rewrite a real entry.
    """
    with db() as conn:
        return [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades "
            "WHERE status='open' AND (mt5_ticket IS NULL OR mt5_ticket=0) "
            "AND (entry_price IS NULL OR entry_price=0)"
        ).fetchall()]


def adopt_placeholder_onto_leg(
    trade_id: str, ticket: int, entry: float, lots: float,
) -> None:
    """Point a placeholder at a live broker leg found during reconciliation.

    The WHERE clause repeats the placeholder conditions on purpose. The row is
    only a placeholder while it has no ticket; if a fill event landed between
    the scan and this write, the ticket already on the row is the real one and
    overwriting it would point the row at the wrong position.
    """
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET mt5_ticket=?,entry_price=?,"
            "entry_low=?,entry_high=?,lot_size=?,remaining_lots=? "
            "WHERE trade_id=? AND status='open' AND (mt5_ticket IS NULL OR mt5_ticket=0)",
            (ticket, entry, entry, entry, lots, lots, trade_id),
        )


def record_placeholder_fill(
    trade_id: str, ticket: int, entry: float,
    lot_size: float, remaining_lots: float, profit: float,
) -> None:
    """Write the real fill (and the broker's own P&L) onto a placeholder that
    is about to be closed.

    Unguarded, unlike adopt_placeholder_onto_leg: the caller has already
    resolved this row against the broker and is about to call record_close, so
    a ticket appearing meanwhile must not silently skip the write and leave
    record_close computing against a zero entry. mt5_profit is written here so
    it is the authoritative figure for both the P&L and the Telegram message.

    lot_size and remaining_lots are separate parameters rather than one `lots`
    because the caller falls back to a DIFFERENT column for each when the
    broker reports no volume. Collapsing them would quietly equalise a
    partially-closed row.
    """
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET mt5_ticket=?,entry_price=?,"
            "entry_low=?,entry_high=?,lot_size=?,remaining_lots=?,mt5_profit=? "
            "WHERE trade_id=?",
            (ticket, entry, entry, entry, lot_size, remaining_lots, profit, trade_id),
        )

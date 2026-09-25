"""The two counts Set & Forget Auto gates on, read from the trade ledger.

Auto's orders go through `open_manual_market_order`, which records the caller's
`source_name` as `vantage_simulated_trades.tg_source` -- so that column is how
an Auto trade is told apart from a manual one. Read-only.
"""
from __future__ import annotations

from backend.src.db.database import db


def trades_today(source: str, since: float) -> int:
    """How many trades from `source` were opened at or after `since`."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades "
            "WHERE tg_source = ? AND open_time >= ?",
            (source, float(since)),
        ).fetchone()
    return int(row[0] or 0)


def open_positions(source: str) -> int:
    """How many trades from `source` are not closed yet."""
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades "
            "WHERE tg_source = ? AND status != 'closed'",
            (source,),
        ).fetchone()
    return int(row[0] or 0)

"""SQL for the closed-market signal queue (core_closed_market_queue).

Signals that arrive while the market is shut are stored here and replayed at
open. Statements moved verbatim from the service; the try/except logging
around each one stays at the call site, where the fallback value (False, [],
silence) is part of the caller's contract rather than the repo's.
"""
from __future__ import annotations

from backend.src.db.database import db, row_to_dict


def insert_queued_signal(
    tg_id: str, channel_name: str, source_label: str,
    parsed_json: str, queued_at: float,
) -> bool:
    """Queue a signal for replay. True if this call created the row.

    INSERT OR IGNORE, not OR REPLACE: the buffered message is re-scanned every
    cycle, so without the IGNORE the same signal would pile up once per second
    until the market reopened.
    """
    with db() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO vantage_closed_market_queue "
            "(tg_message_id, channel_name, source_label, parsed_json, queued_at, status) "
            "VALUES (?,?,?,?,?,'queued')",
            (tg_id, channel_name, source_label, parsed_json, queued_at),
        )
        return cur.rowcount > 0


def fetch_queued_signals() -> list[dict]:
    """Still-queued signals, oldest first -- replay order is arrival order."""
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM vantage_closed_market_queue WHERE status='queued' "
            "ORDER BY queued_at ASC"
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def set_queued_status(tg_id: str, status: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_closed_market_queue SET status=? WHERE tg_message_id=?",
            (status, tg_id),
        )

"""SQL for the second-message hold table (core_second_message_merge).

Some channels post a bare entry and then a separate TP/SL message. A hold row
buffers the bare signal until the follow-up arrives or the window expires.

The WHERE clauses are the substance of this module rather than incidental
detail: attaching a follow-up to the wrong hold invents levels for a signal
that never got any. Statements moved verbatim from the service.
"""
from __future__ import annotations

from typing import Optional

from backend.src.db import transaction
from backend.src.db.database import db, row_to_dict


def get_waiting_hold(tg_id: str) -> Optional[dict]:
    """The still-waiting hold for a message, or None.

    Restricted to status='waiting' deliberately: the caller reads None as "no
    hold exists yet" and inserts a fresh one, so returning a settled hold
    would replay a signal that has already been handled.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM vantage_second_message_holds WHERE tg_message_id=? AND status='waiting'",
            (tg_id,),
        ).fetchone()
    return row_to_dict(row) if row else None


def insert_hold(
    tg_id: str, channel_name: str, partial_json: str, first_seen_at: float,
) -> None:
    """Begin holding a bare signal.

    OR IGNORE because the buffered message is re-scanned every cycle -- a
    plain INSERT would push first_seen_at forward each pass and the expiry
    window would never close.
    """
    with db() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO vantage_second_message_holds "
            "(tg_message_id, channel_name, partial_json, first_seen_at, status) "
            "VALUES (?,?,?,?,'waiting')",
            (tg_id, channel_name, partial_json, first_seen_at),
        )


def attach_followup_levels(channel_name: str, levels_json: str) -> Optional[str]:
    """Apply a follow-up's levels to the newest still-waiting hold on a
    channel. Returns that hold's tg_message_id, or None if the channel has
    nothing waiting -- the overwhelmingly common case, since most SL/TP-shaped
    chatter is not completing anything.

    Newest only: if a channel posted two bare entries back to back, a single
    follow-up belongs to the most recent one, and fanning it across both would
    invent levels for a signal that never got any.

    The select and the update are one transaction so a second follow-up
    arriving mid-call cannot pick the same hold.
    """
    with transaction() as conn:
        row = conn.execute(
            "SELECT tg_message_id FROM vantage_second_message_holds "
            "WHERE channel_name=? AND status='waiting' AND levels_json IS NULL "
            "ORDER BY first_seen_at DESC LIMIT 1",
            (channel_name,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE vantage_second_message_holds SET levels_json=? WHERE tg_message_id=?",
            (levels_json, row[0]),
        )
    return row[0]


def mark_resolved(tg_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_second_message_holds SET status='resolved' WHERE tg_message_id=?",
            (tg_id,),
        )

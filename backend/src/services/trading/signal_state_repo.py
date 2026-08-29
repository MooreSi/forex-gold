"""Signal lifecycle transitions: what happens to a claim that did not open.

Split out of `trade_repo.py` to keep that file inside its size budget. Pure
move for the three that were already there; `park_signal_unknown` is the one
stage3/020 adds.

They are grouped because they answer one question between them -- after a
failed open, is this signal safe to try again? -- and getting that wrong in
either direction is expensive. Restore something that may have filled and the
scheduler opens it twice; park something that was merely rejected and it needs
a human before it can ever trade.
"""
from __future__ import annotations

from backend.src.db.database import db


def restore_signal_after_failed_open(signal_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='pending' "
            "WHERE signal_id=? AND status='activating'",
            (signal_id,),
        )


def reset_signal_to_pending(signal_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='pending', activated_at=NULL"
            " WHERE signal_id=?",
            (signal_id,),
        )


def park_signal_unknown(signal_id: str, reason: str) -> None:
    """Park an in-flight signal whose send got no answer (stage3/020).

    NOT 'pending' and NOT 'failed'. A send that timed out, returned None, or
    died in transport may well have filled: restoring it to 'pending' hands it
    straight back to the scheduler, which is how a filled order becomes two.
    Only reconciliation (stage3/030) may resolve 'unknown', from broker truth.

    Guarded on status='activating' for the same reason every other transition
    here is -- only the in-flight claim may be parked, or a closed or
    cancelled signal could be resurrected by a late error.

    The reason is appended to notes rather than overwriting them: the
    reconciler needs to know what happened, and whatever was already recorded
    about the signal is not ours to discard.
    """
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals "
            "SET status='unknown', "
            "    notes = CASE WHEN notes IS NULL OR notes='' THEN ? "
            "                 ELSE notes || ' | ' || ? END "
            "WHERE signal_id=? AND status='activating'",
            (f"send outcome unknown: {reason}",
             f"send outcome unknown: {reason}", signal_id),
        )


def park_signal_pending(signal_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='pending' WHERE signal_id=?",
            (signal_id,),
        )

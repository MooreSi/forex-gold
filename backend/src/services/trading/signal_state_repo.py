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

import logging
import time

from backend.src.db.database import db

log = logging.getLogger(__name__)


# The cap and the claim are ONE statement on purpose (stage1 phase2/030).
#
# open_trade reads count_open_trades(), compares it to max_open_trades, then
# awaits a tick, the EA handoff and place_order before inserting the row. Two
# signals arriving together both saw "under the cap" and both placed.
#
# Counting `activating` in a separate check does NOT fix it: with a cap of 1
# and two simultaneous claims, each sees the other and both refuse. A tie
# between equals cannot be broken by counting. Putting the cap inside the
# UPDATE's WHERE works because SQLite serialises writers, so the second claim
# genuinely sees the first has already taken the slot.
_CLAIM_SQL = """
    UPDATE vantage_signals SET status='activating', activated_at=?
     WHERE signal_id=? AND status IN ('pending','active')
       AND (SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open')
         + (SELECT COUNT(*) FROM vantage_signals WHERE status='activating')
         < ?
"""


def _max_open_trades(conn) -> int:
    """The same cap open_trade's backstop uses, read on this connection so the
    claim cannot act on a value that changed underneath it."""
    row = conn.execute(
        "SELECT max_open_trades FROM vantage_risk_settings WHERE id=1").fetchone()
    try:
        return int(row[0]) if row and row[0] is not None else 1
    except (TypeError, ValueError):
        return 1


def claim_signal_activation(signal_id: str) -> int:
    """Atomically claim a signal for opening, if a trade slot is free.

    Returns 1 when the claim is won, 0 otherwise -- and 0 now means one of two
    things, which `explain_failed_claim` tells apart: someone else is already
    opening this signal, or the account is at max_open_trades.

    Stamps activated_at with the CLAIM time, which is what lets
    release_stranded_activations tell an abandoned claim from one still in
    flight. created_at cannot do that job: a signal may sit pending for hours
    before anyone claims it. On success repo.mark_signal_active overwrites it
    with the real activation time.

    The claim holds a slot until the signal moves on -- to 'active' on
    success, 'pending' on a rejection, or 'unknown' for a no-answer send. A
    leaked claim would hold one forever, which is why
    release_stranded_activations exists and had to land first.
    """
    with db() as conn:
        return conn.execute(
            _CLAIM_SQL, (time.time(), signal_id, _max_open_trades(conn)),
        ).rowcount


def explain_failed_claim(signal_id: str) -> str:
    """Why a claim was refused, for the caller's error message.

    Only ever called on the failure path, so the extra read costs nothing in
    the normal case. It is a best-effort explanation, not a second gate -- the
    claim above is the only thing that decides.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?",
            (signal_id,)).fetchone()
        if row is None:
            return f"Signal {signal_id} not found"
        if row[0] not in ("pending", "active"):
            return (f"Signal {signal_id} is already being opened "
                    f"(status {row[0]}) — duplicate suppressed")
        cap = _max_open_trades(conn)
        open_now = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()[0]
        claiming = conn.execute(
            "SELECT COUNT(*) FROM vantage_signals WHERE status='activating'"
        ).fetchone()[0]
    return (f"Max open trades reached ({cap}) — {open_now} open, "
            f"{claiming} being opened right now")


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


def fetch_unknown_signals() -> list[dict]:
    """Signals stage3/020 parked because their send got no answer.

    Only reconciliation reads these: they are deliberately invisible to the
    scheduler, which selects status='pending'.
    """
    from backend.src.db.database import row_to_dict
    with db() as conn:
        return [row_to_dict(r) for r in conn.execute(
            "SELECT signal_id, notes FROM vantage_signals WHERE status='unknown'"
        ).fetchall()]


# A claim older than this cannot still be a live open. open_trade's EA ack
# timeout scales with leg count to a 60s ceiling, so the bound has to clear
# that comfortably -- releasing a claim that is still in flight would open the
# same signal twice, which is far worse than leaving a dead one a few minutes
# longer.
STRANDED_ACTIVATION_SECS = 15 * 60


def release_stranded_activations() -> int:
    """Put abandoned `activating` claims back in the queue. Returns how many.

    `claim_signal_activation` flips a signal to `activating` so only one caller
    may open it, and every normal exit path moves it on again -- `active` on
    success, `pending` on a rejection, `unknown` for a no-answer send
    (stage3/020).

    An ABNORMAL exit leaves it stuck. If the process dies between the claim and
    any of those, the row stays `activating`, and the scheduler only ever
    selects `pending` -- so the signal is not failed, not queued and not
    visible anywhere. It is simply gone. Nothing swept for this before.

    Only `activating` is touched, and only past the age bound. `unknown` in
    particular must never be released here: stage3/020 parks a possibly-filled
    send there, and putting one back in the queue could open a trade that is
    already live.
    """
    # activated_at is the CLAIM time (see trade_repo.claim_signal_activation).
    # created_at would be wrong and dangerously so: a signal can sit pending
    # for hours before anyone claims it, so an old signal claimed one second
    # ago would look stranded and be released straight back into the queue
    # while its open was still running -- opening it twice.
    #
    # A NULL activated_at means the claim predates this column being stamped.
    # Those are swept too: an activating row with no claim time is certainly
    # not one this process is running right now.
    cutoff = time.time() - STRANDED_ACTIVATION_SECS
    with db() as conn:
        n = conn.execute(
            "UPDATE vantage_signals SET status='pending' "
            "WHERE status='activating' "
            "  AND (activated_at IS NULL OR activated_at < ?)",
            (cutoff,),
        ).rowcount
    if n:
        log.warning(
            "[signals] released %d abandoned activation claim(s) back to "
            "pending — the process died mid-open and nothing else would ever "
            "have looked at them again", n,
        )
    return n

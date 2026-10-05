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
#
# ── What counts as a used slot (owner, 2026-09-28) ───────────────────────────
# A slot is a trade that is live, or is about to be: an open position, or a
# market open in flight. A resting order holds NONE, working or withdrawn.
# The owner's words: "max number of trades means maximum number of executed
# trades that are live on mt5 on the active node, excluding limit orders, a
# limit order cannot execute if there are already 3 live trades". One app
# trade is one slot however many legs it has at MT5.
#
# This REVERSES the 2026-09-04 rule ("whether it is a resting order or a
# market order the EA should manage the max number of allowable trades"),
# under which a resting order held a slot from placement. What that rule
# protected against -- N resting orders filling into N positions over the
# cap, because apply_pending_fill inserts an open row with no cap check --
# is now the job of resting_revalidation.enforce_max_open_trades: MT5 cannot
# make a limit order conditional on a position count, so while the book is
# full every resting order is withdrawn from the broker, and re-armed when a
# slot frees. Not counting resting orders is only safe because that exists.
#
# An open row the app has not matched to a fill yet (an EA Template
# placeholder, ticket 0) still counts: it may be a market order filling now.
#
# The in-flight term excludes a claim whose own order is resting ('working'
# or 'withdrawn'): the Reversal Engine's pending path leaves its signal
# 'activating' until the fill, and that is a resting order, not an open in
# flight. Before 2026-09-28 the guard looked at 'working' only, which made a
# withdrawn order's claim count as a slot.
#
# Split in two so the pre-check call sites that already hold their own
# open-trades list add the missing half (count_opens_in_flight) rather than
# re-deriving the whole rule. Two spellings of "how full is the book" is how
# a gate and the message beside it drift apart.
_OPEN_SLOTS_SQL = """
        (SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open')
"""
_IN_FLIGHT_SLOTS_SQL = """
        (SELECT COUNT(*) FROM vantage_signals s WHERE s.status='activating'
             AND s.signal_id IS NOT ?
             AND NOT EXISTS (SELECT 1 FROM vantage_pending_orders p
                              WHERE p.signal_id = s.signal_id
                                AND p.status IN ('working','withdrawn')))
"""
_SLOTS_IN_USE_SQL = f"{_OPEN_SLOTS_SQL} + {_IN_FLIGHT_SLOTS_SQL}"

# Not a slot count: whether anything is at the broker at all. A resting order
# is at stake -- an EA restart forgets it until the next hello restores it --
# even though it holds no slot. See count_book_at_stake.
_RESTING_SQL = """
        (SELECT COUNT(*) FROM vantage_pending_orders WHERE status='working')
"""
_AT_STAKE_SQL = f"{_OPEN_SLOTS_SQL} + {_RESTING_SQL} + {_IN_FLIGHT_SLOTS_SQL}"

# `IS NOT ?` rather than `<> ?` so a NULL exclusion (the usual case -- count
# everything) still matches every row: `signal_id <> NULL` is NULL, never true,
# and would silently count nothing at all.
_NO_EXCLUSION: tuple = (None,)


def _exclusion(signal_id) -> tuple:
    """Params for _IN_FLIGHT_SLOTS_SQL: drop this signal's own claim.

    open_trade runs AFTER the claim that reserved its slot, for the same
    signal -- so counting in-flight claims without excluding the caller's own
    refuses every trade the normal path tries ("max open trades reached (1)"
    with one claim, its own, in flight). Only the in-flight half is
    excluded: an OPEN row on the same signal is a position that exists, and
    dropping it would let one signal hold two slots.
    """
    return (signal_id,) if signal_id is not None else _NO_EXCLUSION

# The claim needs no exclusion: the signal it is claiming is still 'pending' or
# 'active' when this runs, so it cannot yet be holding a slot of its own.
_CLAIM_SQL = f"""
    UPDATE vantage_signals SET status='activating', activated_at=?
     WHERE signal_id=? AND status IN ('pending','active')
       AND ({_SLOTS_IN_USE_SQL}) < ?
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


def count_trade_slots_used(conn=None, exclude_signal_id=None) -> int:
    """Trade slots currently held, against the GUI's Max Open Trades: open
    positions plus market opens in flight. Resting orders hold none (owner,
    2026-09-28).

    The same arithmetic the claim gates on, exposed for the paths that check
    the cap outside a claim -- open_trade's backstop, the resting-order guard
    and the pre-checks that turn "no slot" into a skip reason. One number,
    one definition.

    Pass `conn` to count on a connection that already holds a transaction.
    """
    params = _exclusion(exclude_signal_id)
    if conn is not None:
        return int(conn.execute(f"SELECT {_SLOTS_IN_USE_SQL}", params).fetchone()[0])
    with db() as _conn:
        return int(_conn.execute(f"SELECT {_SLOTS_IN_USE_SQL}", params).fetchone()[0])


def count_opens_in_flight(conn=None, exclude_signal_id=None) -> int:
    """The half of count_trade_slots_used that has no open row yet: market
    opens claimed and being sent.

    For the pre-check call sites that already hold their own open-trades list
    (the scan path is handed one by its caller, and IME/pending-activation each
    read one for other reasons) -- they add this to the length of that list
    rather than counting the book a second way. Until 2026-09-28 this also
    counted resting orders, as `count_slots_not_yet_open`.
    """
    params = _exclusion(exclude_signal_id)
    if conn is not None:
        return int(conn.execute(f"SELECT {_IN_FLIGHT_SLOTS_SQL}", params).fetchone()[0])
    with db() as _conn:
        return int(_conn.execute(f"SELECT {_IN_FLIGHT_SLOTS_SQL}", params).fetchone()[0])


def count_book_at_stake() -> int:
    """Open positions + orders resting at the broker + opens in flight.

    NOT the cap. This answers "is anything at the broker that a restart would
    blind?", for the EA install at startup and the stale-EA reload, which
    defer while it is above zero. It was count_trade_slots_used until resting
    orders stopped holding a slot (2026-09-28); a resting order is still at
    stake.
    """
    with db() as _conn:
        return int(_conn.execute(f"SELECT {_AT_STAKE_SQL}", (None,)).fetchone()[0])


def slots_used_and_cap() -> tuple[int, int]:
    """(slots in use, Max Open Trades), read on one connection so the two
    cannot come from different moments. For the resting-order guard."""
    with db() as _conn:
        return count_trade_slots_used(_conn), _max_open_trades(_conn)


def describe_trade_slots(conn=None) -> str:
    """Where the slots have gone, for a message a human reads.

    Resting orders are named although they hold no slot (owner, 2026-09-28):
    while the book is full they are withdrawn from the broker, and a message
    that did not mention them would leave "why did my limit not fill?"
    unanswered. The two counted kinds are what count_trade_slots_used sums.
    """
    def _counts(c):
        open_now = c.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()[0]
        resting = c.execute(
            "SELECT COUNT(*) FROM vantage_pending_orders WHERE status='working'"
        ).fetchone()[0]
        claiming = c.execute(
            "SELECT COUNT(*) FROM vantage_signals s WHERE s.status='activating' "
            "AND NOT EXISTS (SELECT 1 FROM vantage_pending_orders p "
            "WHERE p.signal_id = s.signal_id "
            "AND p.status IN ('working','withdrawn'))"
        ).fetchone()[0]
        return open_now, resting, claiming

    if conn is not None:
        open_now, resting, claiming = _counts(conn)
    else:
        with db() as _conn:
            open_now, resting, claiming = _counts(_conn)
    return (f"{open_now} open, {claiming} being opened right now "
            f"({resting} resting at the broker, not counted)")


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
            _CLAIM_SQL,
            (time.time(), signal_id, *_NO_EXCLUSION, _max_open_trades(conn)),
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
        detail = describe_trade_slots(conn)
    return f"Max open trades reached ({cap}) — {detail}"


def restore_signal_after_failed_open(signal_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='pending' "
            "WHERE signal_id=? AND status='activating'",
            (signal_id,),
        )


def reset_signal_to_pending(signal_id: str) -> None:
    """Hand a signal back to the scheduler after a failed attempt.

    Excludes 'unknown' (stage3/020, guard added 2026-08-31). This is the
    SECOND door back into the queue -- `restore_signal_after_failed_open` is
    the other -- and it is the one `scan_auto_execute` calls on its way out of
    EVERY exception, including the SendOutcomeUnknown that `_route_failed_open`
    has just parked the signal for one frame below. Without this clause the
    park was written and then immediately overwritten, leaving the signal
    'pending' on the main Telegram path: exactly the state PendingWatcher
    re-activates every 20 seconds, for an order that may already be live.

    Found by driving 020's killer demo end to end; every unit test passed
    because none of them ran the caller.
    """
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='pending', activated_at=NULL"
            " WHERE signal_id=? AND status != 'unknown'",
            (signal_id,),
        )


def park_signal_unknown(signal_id: str, reason: str) -> None:
    """Park an in-flight signal whose send got no answer (stage3/020).

    NOT 'pending' and NOT 'failed'. A send that timed out, returned None, or
    died in transport may well have filled: restoring it to 'pending' hands it
    straight back to the scheduler, which is how a filled order becomes two.
    Only reconciliation (stage3/030) may resolve 'unknown', from broker truth.

    Guarded to the two IN-FLIGHT statuses for the same reason every other
    transition here is -- a closed or cancelled signal must not be resurrected
    by a late error, and a signal that never started must not be parked.

    Both of them, since 2026-08-31. It was 'activating' alone, which is what
    `open_trade_from_signal` leaves a signal in. But the fresh-Telegram-signal
    path does not go through `open_trade_from_signal` at all -- it calls
    `core_open_trade.open_trade` directly -- and there the signal is 'active'
    when the send fails. So on the PRIMARY signal path this UPDATE matched no
    row and the park was a silent no-op, leaving the signal re-claimable
    (`claim_signal_activation` accepts `status IN ('pending','active')`).

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
            "WHERE signal_id=? AND status IN ('activating','active')",
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


def mark_forwarded_signal_active(signal_id: str) -> None:
    """The paired VPS has opened this signal; on this node it is done.

    `open_trade` forwards under centralized signal generation and returns
    before any local `insert_trade`, the only other thing that moves a signal
    to `active`. Left `activating`, release_stranded_activations put it back
    to `pending` 15 minutes later and it was forwarded again (bugs/071 #3).
    A signal cancelled or expired meanwhile is left as it is.
    """
    with db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='active' "
            "WHERE signal_id=? AND status IN ('activating','pending')",
            (signal_id,),
        )


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

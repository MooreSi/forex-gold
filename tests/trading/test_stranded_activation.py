"""A signal claimed for opening, then abandoned, is lost forever.

`claim_signal_activation` atomically flips a signal `pending`/`active` ->
`activating` so only one caller can open it. Every normal exit path puts it
back: success moves it to `active`, a rejection restores `pending`, and
stage3/020 parks a no-answer send as `unknown`.

Nothing covers an ABNORMAL exit. If the process dies between the claim and any
of those -- a crash, a kill, a power cut, a restart during a slow EA handoff --
the row stays `activating`. The scheduler selects `status='pending'`, so it
never looks at it again. The signal is not failed, not queued, not visible: it
is simply gone, and nothing in the app ever mentions it.

There is no sweep for this today. A stranded row is also why the cap fix in
stage1 2/030 cannot simply count `activating` rows -- one stranded claim would
consume a trade slot permanently, which is bugs/016 in a different table.

This is the sweep. It only ever moves a claim that is old enough to be
certainly dead back to `pending`, where the scheduler can see it again.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.trading import signal_state_repo as ssr


def _insert(signal_id="sig-1", status="activating", age_s=0.0,
            created_age_s=None):
    """`age_s` is how long ago the CLAIM was made; `created_age_s` how old the
    signal itself is. They are deliberately separate -- conflating them is the
    bug this file exists to prevent."""
    from backend.src.db.database import db
    now = time.time()
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at,"
            "activated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1, status,
             now - (created_age_s if created_age_s is not None else age_s),
             now - age_s))


def _status(signal_id="sig-1"):
    from backend.src.db.database import db
    with db() as conn:
        row = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?",
                           (signal_id,)).fetchone()
        return row[0] if row else None


class TestTheSweepRecoversAStrandedClaim:
    def test_an_old_activating_signal_goes_back_to_pending(self, fresh_db):
        _insert(age_s=ssr.STRANDED_ACTIVATION_SECS + 60)

        n = ssr.release_stranded_activations()

        assert n == 1
        assert _status() == "pending"

    def test_the_scheduler_can_see_it_again(self, fresh_db):
        """The point of the whole thing: pending is what gets picked up."""
        from backend.src.services.signals import repo as signals_repo
        _insert(age_s=ssr.STRANDED_ACTIVATION_SECS + 60)

        ssr.release_stranded_activations()

        ids = [r["signal_id"]
               for r in signals_repo.get_pending_signals_awaiting_zone_fill()]
        assert "sig-1" in ids

    def test_it_reports_how_many_it_released(self, fresh_db):
        for i in range(3):
            _insert(f"sig-{i}", age_s=ssr.STRANDED_ACTIVATION_SECS + 60)

        assert ssr.release_stranded_activations() == 3


class TestItNeverTouchesALiveOpen:
    def test_a_RECENT_claim_is_left_alone(self, fresh_db):
        """The dangerous direction. An open genuinely in flight -- a slow EA
        handoff can take 60 seconds for a multi-leg template -- must not be
        released back into the queue, or the same signal gets opened twice."""
        _insert(age_s=5.0)

        assert ssr.release_stranded_activations() == 0
        assert _status() == "activating"

    def test_a_claim_just_UNDER_the_threshold_is_left_alone(self, fresh_db):
        _insert(age_s=ssr.STRANDED_ACTIVATION_SECS - 30)

        assert ssr.release_stranded_activations() == 0
        assert _status() == "activating"

    def test_AN_OLD_SIGNAL_CLAIMED_JUST_NOW_IS_LEFT_ALONE(self, fresh_db):
        """The bug I nearly shipped. My first version keyed the sweep on
        created_at -- the SIGNAL's age -- so a signal that had sat pending for
        two hours and was claimed one second ago looked stranded, and would
        have been released back into the queue while its open was still
        running. Opening the same signal twice is exactly what the claim
        exists to prevent, so the sweep would have caused the failure it was
        written to avoid.

        The sweep keys on activated_at, which claim_signal_activation now
        stamps with the claim time."""
        _insert(age_s=1.0, created_age_s=7200.0)

        assert ssr.release_stranded_activations() == 0
        assert _status() == "activating"

    def test_a_claim_with_NO_timestamp_is_swept(self, fresh_db):
        """Rows claimed before activated_at was stamped. An activating row
        with no claim time is certainly not one this process is running."""
        from backend.src.db.database import db
        _insert(age_s=0.0)
        with db() as conn:
            conn.execute("UPDATE vantage_signals SET activated_at=NULL "
                         "WHERE signal_id='sig-1'")

        assert ssr.release_stranded_activations() == 1
        assert _status() == "pending"

    def test_the_threshold_outlasts_the_slowest_legitimate_open(self, fresh_db):
        """Not an arbitrary number. open_trade's EA ack timeout scales with
        leg count to a 60s ceiling, so anything at or below that could still
        be a live open."""
        assert ssr.STRANDED_ACTIVATION_SECS > 60

    @pytest.mark.parametrize("status", ["pending", "active", "closed",
                                        "cancelled", "expired", "unknown"])
    def test_no_other_status_is_touched(self, fresh_db, status):
        """Especially `unknown`: stage3/020 parks a possibly-filled send
        there, and releasing one back to pending would re-open a trade that
        may already be live."""
        _insert(status=status, age_s=ssr.STRANDED_ACTIVATION_SECS + 600)

        ssr.release_stranded_activations()

        assert _status() == status


class TestTheClaimRecordsWhenItHappened:
    """Without a claim timestamp the sweep cannot tell abandoned from
    in-flight, and would have to guess from the signal's own age."""

    def test_claiming_stamps_activated_at(self, fresh_db):
        from backend.src.db.database import db
        from backend.src.services.trading import trade_repo

        _insert(status="pending", age_s=7200.0, created_age_s=7200.0)
        before = time.time()

        assert trade_repo.claim_signal_activation("sig-1") == 1

        with db() as conn:
            stamped = conn.execute(
                "SELECT activated_at FROM vantage_signals WHERE signal_id='sig-1'"
            ).fetchone()[0]
        assert stamped >= before, "the claim did not record when it happened"

    def test_a_losing_claim_stamps_nothing(self, fresh_db):
        """Only one caller may claim. The loser must not move the timestamp,
        or it would keep resetting the winner's clock and a genuinely stranded
        claim would never age out."""
        from backend.src.db.database import db
        from backend.src.services.trading import trade_repo

        _insert(status="activating", age_s=7200.0)

        assert trade_repo.claim_signal_activation("sig-1") == 0

        with db() as conn:
            stamped = conn.execute(
                "SELECT activated_at FROM vantage_signals WHERE signal_id='sig-1'"
            ).fetchone()[0]
        assert time.time() - stamped > 7000


class TestTheReversalEnginesClaimIsSweptSafely:
    """A second claim path exists, and the sweep has to be safe against it.

    `reversal_engine_repo.claim_vantage_signal_activation` is a near-copy of
    the canonical claim -- its own comment says "same pattern
    core_open_trade_from_signal uses" -- and it drifted the moment the
    canonical one started stamping `activated_at`.

    That drift is dangerous in a specific way. The sweep releases a claim whose
    `activated_at` is NULL, on the reasoning that a claim with no recorded time
    cannot be one this process is running. A claim path that never stamps one
    produces exactly that shape, so a reversal-engine open still in flight
    would be released back into the queue on the next reconciliation pass --
    and the signal opened twice. The sweep would cause the failure the claim
    exists to prevent.
    """

    def test_the_reversal_engine_claim_records_when_it_happened(self, fresh_db):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        from backend.src.db.database import db

        _insert(status="pending", age_s=7200.0, created_age_s=7200.0)
        before = time.time()

        assert re_db.claim_vantage_signal_activation("sig-1") == 1

        with db() as conn:
            stamped = conn.execute(
                "SELECT activated_at FROM vantage_signals WHERE signal_id='sig-1'"
            ).fetchone()[0]
        assert stamped is not None, (
            "the reversal engine's claim left activated_at NULL — the sweep "
            "would release it mid-open")
        assert stamped >= before

    def test_a_fresh_reversal_engine_claim_is_NOT_swept(self, fresh_db):
        """The interaction, end to end."""
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db

        _insert(status="pending", age_s=7200.0, created_age_s=7200.0)
        re_db.claim_vantage_signal_activation("sig-1")

        assert ssr.release_stranded_activations() == 0
        assert _status() == "activating"

    def test_an_ABANDONED_reversal_engine_claim_is_still_swept(self, fresh_db):
        """The other half: it must not become un-sweepable either."""
        from backend.src.db.database import db
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db

        _insert(status="pending", age_s=0.0)
        re_db.claim_vantage_signal_activation("sig-1")
        with db() as conn:
            conn.execute("UPDATE vantage_signals SET activated_at=? WHERE signal_id='sig-1'",
                         (time.time() - ssr.STRANDED_ACTIVATION_SECS - 60,))

        assert ssr.release_stranded_activations() == 1
        assert _status() == "pending"

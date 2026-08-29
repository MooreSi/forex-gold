"""Two signals must not both pass a cap of one (stage1 phase2/030).

`open_trade` reads `count_open_trades()`, compares it to `max_open_trades`, and
then does several awaits -- a tick fetch, the EA handoff, `place_order` --
before the row is inserted. Two signals arriving together both see "0 open,
under the cap", both pass, and both place real orders. The only always-on
protection the system has can be raced straight past (review data H5).

THE OBVIOUS FIX DOES NOT WORK, and symmetrically so. "Also count signals in
`activating`" means that with a cap of 1 and two simultaneous claims, each sees
the OTHER claim in flight and both refuse. Counting cannot break a tie between
equals.

What works is moving the cap into the claim's own WHERE clause. SQLite
serialises writers, so the second claim genuinely sees the first:

    first:  0 open + 0 activating < 1  -> claims
    second: 0 open + 1 activating < 1  -> refuses

There is no window between the test and the write because they are one
statement. The existing check in `open_trade` stays as the backstop for paths
that never claim a signal at all -- manual market orders and IME.

A claim that leaks would consume a slot permanently, so
`release_stranded_activations` (landed 2026-08-29) is a prerequisite, not a
nicety: without it a crash mid-open would eventually stop all trading.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.trading import signal_state_repo as ssr


def _set_cap(n: int):
    from backend.src.db.database import db
    with db() as conn:
        conn.execute("UPDATE vantage_risk_settings SET max_open_trades=? WHERE id=1", (n,))


def _signal(signal_id, status="pending"):
    from backend.src.db.database import db
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1, status, time.time()))


def _open_trade(trade_id="t-open", signal_id="sig-open"):
    from backend.src.db.database import db
    _signal(signal_id, status="active")
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,"
            "direction,entry_low,entry_high,entry_price,lot_size,remaining_lots,"
            "stop_loss,status,open_time,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, 111, "BUY", 4000.0, 4002.0, 4000.0, 0.1, 0.1,
             3990.0, "open", time.time(), "scalp"))


def _status(signal_id):
    from backend.src.db.database import db
    with db() as conn:
        return conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?",
                            (signal_id,)).fetchone()[0]


class TestTheClaimStillWorksNormally:
    """Controls. Without these the whole file would pass against a claim that
    refuses everything, which would stop all trading."""

    def test_a_first_claim_succeeds(self, fresh_db):
        _set_cap(1)
        _signal("sig-1")

        assert ssr.claim_signal_activation("sig-1") == 1
        assert _status("sig-1") == "activating"

    def test_an_active_signal_can_also_be_claimed(self, fresh_db):
        _set_cap(1)
        _signal("sig-1", status="active")

        assert ssr.claim_signal_activation("sig-1") == 1

    def test_a_closed_signal_cannot_be_claimed(self, fresh_db):
        _set_cap(5)
        _signal("sig-1", status="closed")

        assert ssr.claim_signal_activation("sig-1") == 0

    def test_the_same_signal_cannot_be_claimed_twice(self, fresh_db):
        """The original purpose of the claim, unchanged."""
        _set_cap(5)
        _signal("sig-1")

        assert ssr.claim_signal_activation("sig-1") == 1
        assert ssr.claim_signal_activation("sig-1") == 0


class TestTheRaceIsClosed:
    def test_TWO_SIGNALS_CANNOT_BOTH_CLAIM_A_CAP_OF_ONE(self, fresh_db):
        """The bug. Both used to pass the cap check and both placed orders."""
        _set_cap(1)
        _signal("sig-a")
        _signal("sig-b")

        first = ssr.claim_signal_activation("sig-a")
        second = ssr.claim_signal_activation("sig-b")

        assert first == 1
        assert second == 0, "two signals both claimed a single trade slot"
        assert _status("sig-b") == "pending", "the loser was left mid-claim"

    def test_a_cap_of_two_admits_exactly_two(self, fresh_db):
        _set_cap(2)
        for s in ("sig-a", "sig-b", "sig-c"):
            _signal(s)

        results = [ssr.claim_signal_activation(s) for s in ("sig-a", "sig-b", "sig-c")]

        assert results == [1, 1, 0]

    def test_AN_EXISTING_OPEN_TRADE_FILLS_THE_CAP(self, fresh_db):
        """The claim counts real open trades too, not just other claims --
        otherwise the cap only limits simultaneous opens, not open positions."""
        _set_cap(1)
        _open_trade()
        _signal("sig-a")

        assert ssr.claim_signal_activation("sig-a") == 0

    def test_open_trades_and_claims_are_counted_TOGETHER(self, fresh_db):
        """One of each against a cap of two leaves no room for a third."""
        _set_cap(2)
        _open_trade()
        _signal("sig-a")
        _signal("sig-b")

        assert ssr.claim_signal_activation("sig-a") == 1
        assert ssr.claim_signal_activation("sig-b") == 0

    def test_a_signal_does_not_count_ITSELF_out(self, fresh_db):
        """At the moment the WHERE is evaluated the signal is still pending,
        so it is not in the activating count. If it counted itself, a cap of
        one would admit nobody at all and trading would stop dead."""
        _set_cap(1)
        _signal("sig-a")

        assert ssr.claim_signal_activation("sig-a") == 1

    def test_releasing_a_claim_frees_the_slot(self, fresh_db):
        """The other half of the safety story: a slot must come back. This is
        what stops a leaked claim being permanent."""
        _set_cap(1)
        _signal("sig-a")
        _signal("sig-b")
        ssr.claim_signal_activation("sig-a")
        assert ssr.claim_signal_activation("sig-b") == 0

        ssr.restore_signal_after_failed_open("sig-a")

        assert ssr.claim_signal_activation("sig-b") == 1

    def test_a_closed_trade_does_not_hold_a_slot(self, fresh_db):
        from backend.src.db.database import db
        _set_cap(1)
        _open_trade()
        with db() as conn:
            conn.execute("UPDATE vantage_simulated_trades SET status='closed'")
        _signal("sig-a")

        assert ssr.claim_signal_activation("sig-a") == 1


class TestWhyAClaimFailed:
    """Two different refusals need two different messages: one is a duplicate
    caller, the other is the account being full."""

    def test_a_duplicate_claim_is_reported_as_such(self, fresh_db):
        _set_cap(5)
        _signal("sig-1")
        ssr.claim_signal_activation("sig-1")

        assert "already" in ssr.explain_failed_claim("sig-1").lower()

    def test_a_full_account_is_reported_as_such(self, fresh_db):
        _set_cap(1)
        _open_trade()
        _signal("sig-a")

        reason = ssr.explain_failed_claim("sig-a")

        assert "max open trades" in reason.lower()

    def test_a_missing_signal_says_so(self, fresh_db):
        assert "not found" in ssr.explain_failed_claim("no-such").lower()


class TestTheCapFallsBackSAFELY:
    """What the claim does when it cannot read a cap at all.

    Every other test here sets the cap explicitly, so the fallback was never
    exercised -- mutation showed that raising it to 9999 passed the whole
    file. A fallback that is looser than the schema default silently disables
    the only always-on protection the system has, and it applies exactly when
    the configuration is least trustworthy.
    """

    # There is deliberately no NULL-cap test: the column is NOT NULL, so that
    # state cannot exist. The `is not None` guard in _max_open_trades is belt
    # and braces, and a test for an unreachable state would only look like
    # coverage. The two cases below are both genuinely reachable.

    def test_NO_settings_row_falls_back_to_ONE(self, fresh_db):
        from backend.src.db.database import db
        with db() as conn:
            conn.execute("DELETE FROM vantage_risk_settings")
        _signal("sig-a")
        _signal("sig-b")

        assert ssr.claim_signal_activation("sig-a") == 1
        assert ssr.claim_signal_activation("sig-b") == 0

    def test_a_GARBAGE_cap_falls_back_to_ONE(self, fresh_db):
        """The column is INTEGER but SQLite does not enforce that."""
        from backend.src.db.database import db
        with db() as conn:
            conn.execute("UPDATE vantage_risk_settings SET max_open_trades='lots'")
        _signal("sig-a")
        _signal("sig-b")

        assert ssr.claim_signal_activation("sig-a") == 1
        assert ssr.claim_signal_activation("sig-b") == 0

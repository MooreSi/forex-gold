"""A signal's row says what happened to it, not merely that it stopped.

`vantage_signals.status` has four values the screens actually see -- active,
closed, expired, cancelled -- and "closed" is the one that carries no
information. A signal that took 300 dollars and one that gave back 300 both
read "closed", so the feed on the Dashboard was a list of things that had
finished with no way to tell which of them had worked.

The outcome is not on the signal. It is the net profit of the trades that
signal produced, which live in `vantage_simulated_trades` keyed by
`signal_id`. **The backend decides it**, here, once: a browser that summed
`net_pnl` itself would be a second answer to "did this win", and two screens
that compute it separately are two screens that can disagree about the same
signal.

Owner's request, 2026-09-22: won / lost / open / skipped on the Dashboard's
signal feed.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.signals import outcomes


def _signal(db, signal_id: str, status: str) -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals "
            "(signal_id,source_name,direction,entry_low,entry_high,stop_loss,"
            " status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (signal_id, "TestChannel", "BUY", 2400.0, 2401.0, 2390.0,
             status, time.time()),
        )


def _trade(db, signal_id: str, net_pnl, status: str = "closed") -> None:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades "
            "(trade_id,signal_id,direction,entry_low,entry_high,entry_price,"
            " lot_size,remaining_lots,stop_loss,status,net_pnl,open_time) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (f"t-{signal_id}-{net_pnl}", signal_id, "BUY", 2400.0, 2401.0,
             2400.5, 0.1, 0.1, 2390.0, status, net_pnl, time.time()),
        )


class TestWhatTheRuleSays:
    def test_a_closed_signal_whose_trade_made_money_won(self, fresh_db):
        _signal(fresh_db, "s-win", "closed")
        _trade(fresh_db, "s-win", 25.30)

        assert outcomes.outcomes_by_signal()["s-win"]["outcome"] == "won"

    def test_a_closed_signal_whose_trade_lost_money_lost(self, fresh_db):
        _signal(fresh_db, "s-loss", "closed")
        _trade(fresh_db, "s-loss", -59.40)

        assert outcomes.outcomes_by_signal()["s-loss"]["outcome"] == "lost"

    def test_break_even_is_neither_won_nor_lost(self, fresh_db):
        # A trade closed at break-even after the stop was moved is a real and
        # common ending. Calling it "won" would overstate the record by every
        # scratch trade the account has ever taken.
        _signal(fresh_db, "s-flat", "closed")
        _trade(fresh_db, "s-flat", 0.0)

        assert outcomes.outcomes_by_signal()["s-flat"]["outcome"] == "flat"

    def test_several_trades_from_one_signal_are_summed(self, fresh_db):
        # Six signals in the owner's own database have more than one trade
        # against them. Reading only the first would report the outcome of a
        # part of the position as the outcome of the signal.
        _signal(fresh_db, "s-multi", "closed")
        _trade(fresh_db, "s-multi", -20.0)
        _trade(fresh_db, "s-multi", 55.0)

        row = outcomes.outcomes_by_signal()["s-multi"]
        assert row["outcome"] == "won"
        assert row["net_pnl"] == pytest.approx(35.0)

    def test_an_open_trade_has_no_outcome_yet(self, fresh_db):
        # Running profit is not a result. A position 40 dollars up that is
        # still open has not won anything, and a feed that says it has is
        # counting money the account does not have.
        _signal(fresh_db, "s-running", "active")
        _trade(fresh_db, "s-running", 40.0, status="open")

        assert "s-running" not in outcomes.outcomes_by_signal()

    def test_a_signal_that_never_traded_has_no_outcome(self, fresh_db):
        # Expired and cancelled signals produced no trade. They are not
        # break-even; there is nothing to be even about.
        _signal(fresh_db, "s-expired", "expired")

        assert "s-expired" not in outcomes.outcomes_by_signal()

    def test_the_schema_will_not_let_a_trade_have_no_pnl(self, fresh_db):
        """`net_pnl` is `REAL NOT NULL DEFAULT 0`.

        This began as a test that a missing P&L produces no outcome, and the
        insert would not go in: the column forbids it, so there is no such
        row to classify and the guard against one was dead code. It is worth a
        test anyway, because the rule that replaces it is "a zero is a real
        break-even". The day somebody relaxes this column, a gap in the record
        starts arriving as 0.0 and `classify` will call it flat -- and this is
        the test that says so first.
        """
        _signal(fresh_db, "s-null", "closed")

        with pytest.raises(Exception, match="NOT NULL"):
            _trade(fresh_db, "s-null", None)

    def test_classify_still_refuses_a_missing_number(self):
        # Not reachable from the table today (see above), but `classify` is a
        # public rule and a caller with a number from somewhere else must not
        # get "flat" for "unknown".
        assert outcomes.classify(None) is None
        assert outcomes.classify("not a number") is None


class TestWhatTheSignalsReadCarries:
    def test_get_signals_attaches_the_outcome(self, fresh_db):
        # The read the API serves, not a separate endpoint: the feed and the
        # Signals table must not be able to disagree.
        from backend.src.services.signals import repo

        _signal(fresh_db, "s-a", "closed")
        _trade(fresh_db, "s-a", 12.0)
        _signal(fresh_db, "s-b", "cancelled")

        rows = {r["signal_id"]: r for r in repo.get_signals()}

        assert rows["s-a"]["outcome"] == "won"
        assert rows["s-a"]["net_pnl"] == pytest.approx(12.0)
        # Present as a key and empty, rather than absent: a renderer that has
        # to tell "no outcome" from "field missing" will get it wrong once.
        assert rows["s-b"]["outcome"] is None
        assert rows["s-b"]["net_pnl"] is None

    def test_the_outcome_query_runs_once_for_the_whole_read(self, fresh_db):
        # Not a query per signal. The owner's database has 608 of them and
        # this read is polled every ten seconds by an open Dashboard; a
        # per-row lookup is 608 round trips into SQLite on every tick.
        from backend.src.services.signals import repo

        for i in range(5):
            _signal(fresh_db, f"s-{i}", "closed")
            _trade(fresh_db, f"s-{i}", float(i))

        seen = []
        real = outcomes.outcomes_by_signal

        def counted(*a, **kw):
            seen.append(1)
            return real(*a, **kw)

        # Patched where `attach` LOOKS IT UP -- its own module namespace --
        # not where repo imported it. Patching the wrong one counts zero calls
        # and the test passes for a reason that has nothing to do with the
        # query.
        original = outcomes.outcomes_by_signal
        outcomes.outcomes_by_signal = counted
        try:
            rows = repo.get_signals()
        finally:
            outcomes.outcomes_by_signal = original

        assert len(rows) == 5
        assert len(seen) == 1

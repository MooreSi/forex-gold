"""A standing disagreement must be reported, not repeated 7,200 times a day.

Found live on the owner's demo account, 2026-09-01. Two EA-template
placeholders whose legs never filled sat open for their designed 24-hour
expiry, and the reconciliation pass logged the same two-line WARNING block
about them **every twelve seconds** -- the monitor loop fast-polls at 1s with
open trades, and the pass runs every 12 cycles. Over the row's lifetime that is
roughly 7,200 identical warnings. The log file was already 35MB.

The cost is not disk. It is that a warning which appears 7,200 times stops
being read, and the next genuinely new difference scrolls past inside the
noise -- which is the exact failure this pass exists to prevent.

What must NOT happen instead is silence. A problem that persists for a day is
worse than one that appears once, so it still gets a periodic reminder, and any
CHANGE to the set is reported immediately at full volume.
"""
from __future__ import annotations

import logging

import pytest

from backend.src.services.positions import reconciliation as rec


@pytest.fixture(autouse=True)
def _fresh_throttle():
    rec.reset_report_throttle()
    yield
    rec.reset_report_throttle()


def _diff(*trade_ids):
    entries = [rec.DiffEntry(kind=rec.DB_ONLY_NO_EVIDENCE, trade_id=t,
                                  signal_id=None, ticket=None,
                                  detail="open in the database, and the broker "
                                         "has no record of it")
               for t in trade_ids]
    return rec.ReconcileDiff(entries=entries)


class TestTheFirstTimeIsLoud:
    def test_a_new_difference_warns(self, caplog):
        with caplog.at_level(logging.WARNING):
            rec.report_periodic(_diff("a"))

        assert any("Reconciliation found differences" in r.message
                   for r in caplog.records)

    def test_the_full_detail_is_in_it(self, caplog):
        with caplog.at_level(logging.WARNING):
            rec.report_periodic(_diff("a"))

        assert "trade=a" in caplog.text
        assert "no record of it" in caplog.text


class TestARepeatIsQuiet:
    def test_the_same_difference_does_not_warn_again(self, caplog):
        rec.report_periodic(_diff("a"))
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            for _ in range(20):
                rec.report_periodic(_diff("a"))

        assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []

    def test_it_is_still_recorded_at_debug(self, caplog):
        """Quiet, not absent. Someone reading the log at DEBUG must still be
        able to see the pass ran and what it found."""
        rec.report_periodic(_diff("a"))
        caplog.clear()

        with caplog.at_level(logging.DEBUG):
            rec.report_periodic(_diff("a"))

        assert any(r.levelno == logging.DEBUG and "trade=a" in r.message
                   for r in caplog.records)


class TestAChangeIsLoudAgain:
    def test_an_added_trade_warns(self, caplog):
        rec.report_periodic(_diff("a"))
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            rec.report_periodic(_diff("a", "b"))

        assert "trade=b" in caplog.text

    def test_a_removed_trade_warns(self, caplog):
        """One of two phantoms being written off is news. Staying silent until
        the last one clears would hide the pass doing its job."""
        rec.report_periodic(_diff("a", "b"))
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            rec.report_periodic(_diff("a"))

        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_a_different_detail_on_the_same_trade_warns(self):
        """The signature is the whole entry, not just the id: the same trade
        moving from "no evidence" to "closed at the broker" is a different
        fact and must not be swallowed as a repeat."""
        first = rec._diff_signature(_diff("a"))
        changed = rec.ReconcileDiff(entries=[
            rec.DiffEntry(kind=rec.DB_ONLY_CLOSED, trade_id="a",
                               signal_id=None, ticket=None,
                               detail="closed at the broker")])

        assert first != rec._diff_signature(changed)

    def test_order_does_not_count_as_a_change(self):
        """The broker's position list has no guaranteed order. If it did count,
        nothing would ever be throttled and the fix would do nothing."""
        assert rec._diff_signature(_diff("a", "b")) == rec._diff_signature(_diff("b", "a"))


class TestItDoesNotGoSilentForever:
    def test_a_standing_problem_is_repeated_periodically(self, caplog, monkeypatch):
        """The point of the throttle is fewer lines, not none. A disagreement
        that persists all day must still be visible to someone reading
        warnings, or the throttle has just hidden the problem.

        The interval moved to backend.src.utils.log_throttle on 2026-09-01,
        when this became the third site with the same problem. The property
        still has to hold HERE, which is why this test stayed rather than
        being deleted as covered upstream.
        """
        from backend.src.utils import log_throttle

        rec.report_periodic(_diff("a"))
        caplog.clear()
        now = [log_throttle.time.time()]
        monkeypatch.setattr(log_throttle.time, "time", lambda: now[0])

        now[0] += log_throttle.DEFAULT_INTERVAL_S + 1
        with caplog.at_level(logging.WARNING):
            rec.report_periodic(_diff("a"))

        assert any(r.levelno >= logging.WARNING for r in caplog.records)

    def test_the_reminder_interval_is_long_enough_to_matter(self):
        """At 12s a cycle, anything under a few minutes barely helps. An hour
        is 24 lines a day instead of 7,200."""
        from backend.src.utils import log_throttle

        assert log_throttle.DEFAULT_INTERVAL_S >= 900


class TestCleanRuns:
    def test_no_differences_logs_nothing_and_clears_the_memory(self, caplog):
        """After everything is resolved, the next new problem must be loud
        again rather than matching a stale signature."""
        rec.report_periodic(_diff("a"))
        rec.report_periodic(rec.ReconcileDiff(entries=[]))
        caplog.clear()

        with caplog.at_level(logging.WARNING):
            rec.report_periodic(_diff("a"))

        assert any(r.levelno >= logging.WARNING for r in caplog.records)


class TestReportItselfIsUnchanged:
    def test_report_still_logs_every_time_it_is_called(self, caplog):
        """`report()` returns text and is the one-shot surface. The throttle
        belongs to the periodic caller; putting it inside report() would make
        a deliberate one-off call silently do nothing."""
        with caplog.at_level(logging.WARNING):
            rec.report(_diff("a"))
            rec.report(_diff("a"))

        assert len([r for r in caplog.records
                    if "Reconciliation found differences" in r.message]) == 2

"""The Signal Generator's virtual trade history.

The owner, 2026-09-19: "signal generator is missing all of the ml details and
virtual trade history so we can monitor how they are performing".

The shadow machinery has always recorded this — every variant's take/skip
decision on every reference signal, in `re_shadow_decisions` — but the only
thing ever read back was `shadow.report()`, which aggregates it to one row per
variant. Aggregates answer "which variant is ahead"; they cannot answer "what
did it do last Tuesday, and was it right", which is the question an operator
watching a challenger actually has.

The distinction that matters most here is between a variant that SKIPPED a
losing trade and one that never saw it. Both contribute nothing to the P&L,
and only one of them is evidence.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as repo
from backend.src.services.reversal_engine import shadow_repo
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_repo():
    """The engine's own database, created and torn down per test.

    Closed before the file is removed: Windows will not unlink a file that
    still has an open handle, which is the 2026-08-27 class of teardown
    failure this repo has already paid for once.
    """
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def _signal(ref, created_at=1000.0, status="closed", pnl=10.0,
            sl_dist=5.0, net=50.0, direction="BUY"):
    repo.get_db().run(
        "INSERT INTO re_signals (created_at, signal_ref, direction, sl_dist, "
        " status, outcome, pnl_pts, net_pnl_dollars) "
        "VALUES (?,?,?,?,?,?,?,?)",
        created_at, ref, direction, sl_dist, status, "win", pnl, net)


def _decision(ref, variant, would_take=1, ts=1000.0, reason="adx ok"):
    repo.get_db().run(
        "INSERT INTO re_shadow_decisions (ts, signal_ref, variant, "
        " would_take, reason) VALUES (?,?,?,?,?)",
        ts, ref, variant, would_take, reason)


@pytest.fixture
def seeded(fresh_repo):
    _signal("s1", created_at=1000.0, pnl=10.0, net=50.0)
    _signal("s2", created_at=2000.0, pnl=-5.0, net=-25.0)
    _signal("s3", created_at=3000.0, status="pending", pnl=0.0, net=0.0)
    _decision("s1", "champion", would_take=1, ts=1001.0)
    _decision("s2", "champion", would_take=1, ts=2001.0)
    _decision("s2", "challenger", would_take=0, ts=2002.0, reason="adx below 22")
    _decision("s3", "champion", would_take=1, ts=3001.0)
    return fresh_repo


class TestWhatComesBack:

    def test_it_returns_one_row_per_decision(self, seeded):
        rows = shadow_repo.recent_decisions(50)

        assert len(rows) == 4

    def test_the_newest_decision_is_first(self, seeded):
        # A history an operator scans from the top wants today at the top.
        rows = shadow_repo.recent_decisions(50)

        assert [r["ts"] for r in rows] == sorted(
            [r["ts"] for r in rows], reverse=True)

    def test_a_row_says_which_variant_decided(self, seeded):
        rows = shadow_repo.recent_decisions(50)

        assert {r["variant"] for r in rows} == {"champion", "challenger"}

    def test_a_row_carries_the_reason_the_variant_gave(self, seeded):
        # "It skipped" is not useful. "It skipped because ADX was below 22" is.
        skipped = next(r for r in shadow_repo.recent_decisions(50)
                       if not r["would_take"])

        assert skipped["reason"] == "adx below 22"

    def test_a_taken_decision_carries_the_outcome_it_would_have_had(self, seeded):
        taken = next(r for r in shadow_repo.recent_decisions(50)
                     if r["signal_ref"] == "s1")

        assert taken["net"] == pytest.approx(50.0)
        assert taken["pnl_pts"] == pytest.approx(10.0)

    def test_it_reports_the_R_a_decision_earned(self, seeded):
        # Dollars depend on the lot; R is the comparable number across
        # variants, and it is what report() scores on.
        taken = next(r for r in shadow_repo.recent_decisions(50)
                     if r["signal_ref"] == "s1")

        assert taken["r"] == pytest.approx(2.0)

    def test_a_decision_on_a_signal_that_has_not_closed_has_no_R(self, seeded):
        # Not 0.0. Zero R and "not settled yet" are different statements, and
        # a table rendering both as 0.00 invites the wrong one to be acted on
        # -- the same rule report() states about mean_r.
        pending = next(r for r in shadow_repo.recent_decisions(50)
                       if r["signal_ref"] == "s3")

        assert pending["r"] is None
        assert pending["status"] == "pending"


class TestSkippedIsNotAbsent:

    def test_a_skip_is_in_the_history(self, seeded):
        # A variant that skipped a losing trade and one that never saw it both
        # contribute nothing to the P&L, and only one of them is evidence.
        refs = {(r["signal_ref"], r["variant"]) for r in shadow_repo.recent_decisions(50)}

        assert ("s2", "challenger") in refs

    def test_a_skip_reports_what_it_avoided(self, seeded):
        # s2 lost. The challenger skipping it is the challenger being right,
        # and the table has to be able to show that.
        skip = next(r for r in shadow_repo.recent_decisions(50)
                    if r["signal_ref"] == "s2" and r["variant"] == "challenger")

        assert skip["would_take"] == 0
        assert skip["net"] == pytest.approx(-25.0)


class TestTheLimit:

    def test_it_returns_no_more_than_asked_for(self, seeded):
        assert len(shadow_repo.recent_decisions(2)) == 2

    def test_the_limit_keeps_the_newest(self, seeded):
        # Truncating from the wrong end shows a history that stops before
        # anything recent happened.
        rows = shadow_repo.recent_decisions(1)

        assert rows[0]["ts"] == 3001.0

    def test_an_empty_table_is_an_empty_list(self, fresh_repo):
        assert shadow_repo.recent_decisions(50) == []

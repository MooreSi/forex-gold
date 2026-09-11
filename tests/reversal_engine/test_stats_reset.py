"""Resetting the panel's numbers without destroying the engine's memory.

The owner asked for realised P&L, virtual balance, total P&L, max drawdown,
win rate and the performance analytics to start fresh, because the ML engine
has been rebuilt and the old numbers describe a system that no longer
exists.

**The rows stay.** Deleting them would take the ML training set, the 571
reconstructed excursion measurements and the entire attribution table with
them -- the only record of how this engine has behaved, and rebuilt from
broker tick history that only reaches back 30 days. So the reset records a
TIMESTAMP and the reporting queries ignore anything closed before it.

The distinction that matters: the epoch changes what the user SEES. It does
not change what the model LEARNS.
"""
from __future__ import annotations

import os
import tempfile
import time

import pytest

from backend.src.services.reversal_engine import reversal_engine_repo as repo
from backend.src.services.reversal_engine import stats_repo
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def closed(fresh_repo, ref, pnl, when, outcome="win", session="london",
           level_type="round_5", bias="bullish"):
    sid = fresh_repo.create_signal({"signal_ref": ref, "direction": "BUY",
                                    "created_at": when})
    repo.get_db().run(
        "UPDATE re_signals SET status='closed', outcome=?, close_time=?, "
        "net_pnl_dollars=?, pnl_pts=?, sl_dist=5.0, session=?, "
        "level_type=?, htf_bias=?, live_exec_status='executed' WHERE id=?",
        outcome, when, pnl, pnl / 10.0, session, level_type, bias, sid)
    return sid


OLD = 1_788_000_000.0
NEW = 1_789_000_000.0


class TestBeforeAnyReset:
    def test_everything_counts(self, fresh_repo):
        closed(fresh_repo, "a", 10.0, OLD)
        closed(fresh_repo, "b", -5.0, NEW)
        assert stats_repo.stats_epoch() == 0.0
        assert stats_repo.get_stats()["total"] == 2


class TestAfterAReset:
    def test_older_closed_signals_stop_counting(self, fresh_repo):
        closed(fresh_repo, "a", 10.0, OLD, outcome="win")
        closed(fresh_repo, "b", -5.0, OLD, outcome="loss")
        stats_repo.reset_stats(now=NEW - 1)
        closed(fresh_repo, "c", 20.0, NEW, outcome="win")

        st = stats_repo.get_stats()
        assert st["wins"] == 1
        assert st["losses"] == 0
        assert st["total_pnl"] == pytest.approx(20.0)
        assert st["win_rate"] == pytest.approx(100.0)

    def test_the_rows_are_still_there(self, fresh_repo):
        """The whole point. The ML training set, the excursion data and the
        attribution history all live in these rows."""
        closed(fresh_repo, "a", 10.0, OLD)
        stats_repo.reset_stats(now=NEW)
        assert len(repo.get_all_signals(limit=100)) == 1

    def test_the_virtual_balance_goes_back_to_the_starting_figure(self, fresh_repo):
        closed(fresh_repo, "a", -500.0, OLD)
        repo.reconcile_balance_with_trades()
        assert repo.get_virtual_balance() < repo._STARTING_BALANCE
        stats_repo.reset_stats(now=NEW)
        assert repo.get_virtual_balance() == pytest.approx(repo._STARTING_BALANCE)

    def test_reconciling_afterwards_does_not_undo_the_reset(self, fresh_repo):
        """`reconcile_balance_with_trades` runs on every `init()` and
        recomputes the balance from every closed trade ever. Without the
        epoch it would silently restore the old number on the next
        restart, and the reset would look like it had not worked."""
        closed(fresh_repo, "a", -500.0, OLD)
        stats_repo.reset_stats(now=NEW)
        repo.reconcile_balance_with_trades()
        assert repo.get_virtual_balance() == pytest.approx(repo._STARTING_BALANCE)

    def test_the_drawdown_ignores_the_pre_reset_balance_log(self, fresh_repo):
        closed(fresh_repo, "a", -800.0, OLD)
        repo.get_db().run(
            "INSERT INTO re_balance_log (ts, balance, change_amt, reason) "
            "VALUES (?,?,?,?)", OLD, 200.0, -800.0, "loss")
        assert stats_repo.get_max_drawdown() > 0
        stats_repo.reset_stats(now=NEW)
        assert stats_repo.get_max_drawdown() == 0.0

    def test_every_performance_table_honours_it(self, fresh_repo):
        closed(fresh_repo, "a", -50.0, OLD, outcome="loss",
               session="asian", level_type="round_5", bias="bearish")
        stats_repo.reset_stats(now=NEW)
        closed(fresh_repo, "b", 30.0, NEW, outcome="win",
               session="london", level_type="unicorn", bias="bullish")

        assert [r["session"] for r in stats_repo.get_perf_by_session()] == ["london"]
        assert [r["level_type"] for r in stats_repo.get_perf_by_level_type()] == ["unicorn"]
        assert [r["htf_bias"] for r in stats_repo.get_perf_by_bias()] == ["bullish"]


class TestWhatItMustNotTouch:
    def test_the_models_own_win_rate_feature_still_sees_everything(self, fresh_repo):
        """`get_recent_win_rate` feeds the feature vector. Filtering it would
        mean a reporting reset quietly changed what the model is told about
        the market, which is a different thing entirely and not what was
        asked for."""
        closed(fresh_repo, "a", 10.0, OLD, outcome="win")
        closed(fresh_repo, "b", 10.0, OLD, outcome="win")
        stats_repo.reset_stats(now=NEW)
        assert repo.get_recent_win_rate(20) == pytest.approx(1.0)

    def test_the_stored_feature_vectors_are_untouched(self, fresh_repo):
        sid = closed(fresh_repo, "a", 10.0, OLD)
        repo.get_db().run("UPDATE re_signals SET ml_features_json=? WHERE id=?",
                          "[1.0, 2.0]", sid)
        stats_repo.reset_stats(now=NEW)
        row = repo.get_signal_by_id(sid)
        assert row["ml_features_json"] == "[1.0, 2.0]"


class TestTheResetIsRecorded:
    def test_it_reports_when_it_happened(self, fresh_repo):
        stats_repo.reset_stats(now=NEW)
        assert stats_repo.stats_epoch() == pytest.approx(NEW)

    def test_resetting_twice_moves_the_line_forward(self, fresh_repo):
        stats_repo.reset_stats(now=NEW)
        stats_repo.reset_stats(now=NEW + 5000)
        assert stats_repo.stats_epoch() == pytest.approx(NEW + 5000)

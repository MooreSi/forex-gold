"""The half of the study that has no outcome: what got blocked, and by what.

The variant report can only score decisions with a resolved outcome, so it
says nothing at all about the signals the live path declined -- and those are
the free half, available from day one instead of after four weeks. This is
that readout.
"""
import os
import tempfile
import time

import pytest

from backend.src.services.signals import decision_log as dlog
from backend.src.services.signals import decision_log_repo as repo
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file


@pytest.fixture
def log_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    repo.create_schema()
    yield repo
    re_repo.close_db()
    remove_db_file(path)


def _d(msg, path="auto", executed=1, reason="", **over):
    row = {"tg_message_id": msg, "path": path, "decided_at": time.time(),
           "executed": executed, "skip_reason": reason}
    row.update(over)
    return repo.insert_decision(row)


class TestTheCounts:
    def test_an_empty_log_reports_zeroes_and_does_not_fail(self, log_db):
        s = dlog.summary()
        assert s["total"] == 0
        assert s["executed"] == 0
        assert s["blocked"] == 0
        assert s["by_path"] == []
        assert s["top_reasons"] == []

    def test_executed_and_blocked_are_counted_separately(self, log_db):
        _d("a", executed=1)
        _d("b", executed=0, reason="news blackout")
        _d("c", executed=0, reason="news blackout")
        s = dlog.summary()
        assert s["total"] == 3
        assert s["executed"] == 1
        assert s["blocked"] == 2

    def test_the_two_paths_are_reported_separately(self, log_db):
        """IME has no R:R filter and the full-signal path does. Pooling them
        would average away the only difference worth looking at."""
        _d("a", path="auto", executed=1)
        _d("a", path="ime", executed=0, reason="spread too wide")
        _d("b", path="ime", executed=0, reason="spread too wide")
        by_path = {r["path"]: r for r in dlog.summary()["by_path"]}
        assert by_path["auto"]["executed"] == 1
        assert by_path["auto"]["blocked"] == 0
        assert by_path["ime"]["blocked"] == 2

    def test_reasons_are_ranked_with_the_commonest_first(self, log_db):
        for i in range(3):
            _d(f"n{i}", executed=0, reason="news blackout")
        _d("s1", executed=0, reason="spread too wide")
        reasons = dlog.summary()["top_reasons"]
        assert reasons[0]["reason"] == "news blackout"
        assert reasons[0]["n"] == 3
        assert reasons[1]["n"] == 1

    def test_an_executed_decision_contributes_no_reason(self, log_db):
        """Its skip_reason is empty, and an empty bar at the top of the
        chart would be the single most misleading thing here."""
        _d("a", executed=1, reason="")
        assert dlog.summary()["top_reasons"] == []


class TestItSaysWhatIsStillUnknown:
    def test_unresolved_executions_are_reported(self, log_db):
        """A reader comparing this against the variant report needs to know
        the gap between them is trades still open, not trades lost."""
        did = _d("a", executed=1, trade_id="t-1")
        _d("b", executed=1, trade_id="t-2")
        repo.set_outcome(did, outcome="win", net_usd=10.0, realised_r=1.0,
                         max_tp_hit="TP1", exit_reason="TP")
        s = dlog.summary()
        assert s["resolved"] == 1
        assert s["awaiting_outcome"] == 1

    def test_reconstructed_rows_are_counted_apart_from_observed_ones(self, log_db):
        _d("live-1", executed=1)
        repo.insert_decision({"tg_message_id": "old-1", "path": "auto",
                              "decided_at": time.time(), "executed": 1,
                              "source": "backfill"})
        s = dlog.summary()
        assert s["observed"] == 1
        assert s["reconstructed"] == 1


class TestItNeverRaises:
    def test_a_broken_repo_returns_an_empty_summary(self, log_db, monkeypatch):
        """It is rendered by a page. A card that throws takes the page with
        it, and this one is decoration next to a live trading screen."""
        def _boom(*a, **k):
            raise RuntimeError("db gone")

        monkeypatch.setattr(repo, "decision_counts", _boom)
        s = dlog.summary()
        assert s["total"] == 0

"""Filling in what the trade actually did (stage 0, second half).

Expected behaviour, from docs/todo/signal-validation/010:

  * The outcome comes from the core trade ledger, read AFTER the fact. This
    does not hook the close path. That path is frozen and a research log has
    no business on it -- pro_outcome walks candles from a cursor for the
    same reason rather than being told when something closed.
  * An open trade is not an outcome. It stays in the queue.
  * R is realised R of OUR execution -- net_pnl over the risk actually taken
    -- not the signal's stated TP1. 000 section 4b is the whole reason this
    table exists: over 1,075 rows labelled on their stated levels the split
    is 738 win / 84 loss, while half of our own trades never reach any
    target at all.
"""
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.services.signals import decision_outcomes as outcomes
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


def _decision(trade_id="trade-1", **over):
    row = {"tg_message_id": "21429", "path": "auto", "decided_at": time.time(),
           "executed": 1, "trade_id": trade_id}
    row.update(over)
    return repo.insert_decision(row)


def _trade(**over):
    t = {"trade_id": "trade-1", "status": "closed", "net_pnl": -50.4,
         "initial_risk": 50.4, "max_tp_hit": "none", "exit_reason": "SL"}
    t.update(over)
    return t


class TestItReadsTheLedgerAfterTheFact:
    def test_a_closed_loss_is_recorded_with_its_realised_R(self, log_db):
        did = _decision()
        with mock.patch.object(outcomes, "_get_trade", return_value=_trade()):
            assert outcomes.resolve_pending() == 1
        row = repo.rows()[0]
        assert row["outcome"] == "loss"
        assert row["net_usd"] == -50.4
        assert row["realised_r"] == -1.0
        assert row["exit_reason"] == "SL"
        assert row["resolved_at"] is not None

    def test_a_closed_win_is_recorded(self, log_db):
        _decision()
        with mock.patch.object(outcomes, "_get_trade",
                               return_value=_trade(net_pnl=105.0, initial_risk=52.5,
                                                   max_tp_hit="TP2", exit_reason="TP")):
            outcomes.resolve_pending()
        row = repo.rows()[0]
        assert row["outcome"] == "win"
        assert row["realised_r"] == 2.0
        assert row["max_tp_hit"] == "TP2"

    def test_R_IS_OUR_RISK_not_the_signals_stated_levels(self, log_db):
        """The point of the whole table. 000 section 4b: a label built from
        their stated TP1 says 90% win and disagrees with the money."""
        _decision()
        with mock.patch.object(outcomes, "_get_trade",
                               return_value=_trade(net_pnl=-12.0, initial_risk=60.0,
                                                   max_tp_hit="TP1")):
            outcomes.resolve_pending()
        row = repo.rows()[0]
        assert row["realised_r"] == pytest.approx(-0.2)
        assert row["outcome"] == "loss", "TP1 was hit and it still lost money"


class TestWhatItRefusesToResolve:
    def test_an_open_trade_stays_in_the_queue(self, log_db):
        _decision()
        with mock.patch.object(outcomes, "_get_trade",
                               return_value=_trade(status="open", net_pnl=None)):
            assert outcomes.resolve_pending() == 0
        assert repo.rows()[0]["outcome"] is None
        assert len(repo.unresolved_outcomes()) == 1

    def test_a_missing_trade_is_left_alone_rather_than_scored_zero(self, log_db):
        """A trade the ledger has not got is not a flat trade."""
        _decision()
        with mock.patch.object(outcomes, "_get_trade", return_value=None):
            assert outcomes.resolve_pending() == 0
        assert repo.rows()[0]["outcome"] is None

    def test_a_blocked_decision_is_never_queued(self, log_db):
        _decision(trade_id=None, executed=0)
        with mock.patch.object(outcomes, "_get_trade",
                               side_effect=AssertionError("must not be asked")):
            assert outcomes.resolve_pending() == 0

    def test_a_zero_risk_trade_records_the_money_and_abstains_on_R(self, log_db):
        """Dividing by it would raise; calling it 0R would be a number we
        do not have."""
        _decision()
        with mock.patch.object(outcomes, "_get_trade",
                               return_value=_trade(initial_risk=0.0, net_pnl=25.0)):
            outcomes.resolve_pending()
        row = repo.rows()[0]
        assert row["net_usd"] == 25.0
        assert row["realised_r"] is None
        assert row["outcome"] == "win"


class TestItNeverBreaksTheSweep:
    def test_a_failing_ledger_read_does_not_raise(self, log_db):
        _decision()
        with mock.patch.object(outcomes, "_get_trade",
                               side_effect=RuntimeError("db gone")):
            assert outcomes.resolve_pending() == 0

    def test_one_bad_row_does_not_stop_the_others(self, log_db):
        _decision(trade_id="bad")
        _decision(tg_message_id="other", trade_id="good")

        def _lookup(trade_id):
            if trade_id == "bad":
                raise RuntimeError("corrupt")
            return _trade(trade_id="good", net_pnl=10.0, initial_risk=10.0)

        with mock.patch.object(outcomes, "_get_trade", side_effect=_lookup):
            assert outcomes.resolve_pending() == 1

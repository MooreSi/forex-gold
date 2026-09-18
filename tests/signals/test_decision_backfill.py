"""Reconstructing decisions the app made before the log existed.

Expected behaviour, from docs/todo/signal-validation/010:

  * Only what is EXACTLY reconstructible is filled in. The clock facts are
    (session liquidity is pure arithmetic on a timestamp) and so is the
    entry trigger (it replays candles as of the decision). The event
    calendar and the HTF bias are gone; they abstain, and abstaining is
    already how a variant says "no opinion".
  * A backfilled row is MARKED. It is a reconstruction, and a table where
    reconstructions are indistinguishable from observations is a table
    nobody can trust a second time.
  * Only executed trades can be reconstructed. Nothing recorded what was
    blocked before the log existed, and inventing those rows would make
    every challenger look better than the champion for free.
  * It is idempotent. Run it twice and the corpus is the same size.
"""
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.services.signals import decision_backfill as backfill
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


def _past(**over):
    row = {"tg_message_id": "21423", "trade_id": "t-1",
           "channel_name": "Gold Diggers VIP", "direction": "BUY",
           "opened_at": time.time() - 86400 * 4,
           "entry_low": 4358.0, "entry_high": 4362.0, "stop_loss": 4353.0,
           "tp1": 4364.0, "spread_points": 24.0, "strategy": "scale_out",
           "status": "closed", "net_pnl": -50.0, "initial_risk": 50.0,
           "max_tp_hit": "none", "exit_reason": "SL"}
    row.update(over)
    return row


class TestWhatItReconstructs:
    def test_a_past_trade_becomes_a_decision(self, log_db):
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            assert backfill.run() == 1
        row = repo.rows()[0]
        assert row["tg_message_id"] == "21423"
        assert row["executed"] == 1
        assert row["entry_low"] == 4358.0
        assert row["trade_id"] == "t-1"

    def test_the_row_is_MARKED_as_a_reconstruction(self, log_db):
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            backfill.run()
        assert repo.rows()[0]["source"] == "backfill"

    def test_a_live_row_is_marked_live(self, log_db):
        repo.insert_decision({"tg_message_id": "live-1", "path": "auto",
                              "decided_at": time.time(), "executed": 1})
        assert repo.rows()[0]["source"] == "live"

    def test_the_liquidity_fact_is_reconstructed_EXACTLY(self, log_db):
        """Pure arithmetic on the decision timestamp, so it is not an
        approximation of anything -- it is the same answer the live gate
        would have given at that instant."""
        sunday_reopen = 1789335000.0   # Sun 2026-09-13 21:30 UTC
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past(opened_at=sunday_reopen)]):
            backfill.run()
        assert repo.rows()[0]["liquidity_blocked"] == 1

    def test_the_outcome_is_carried_over_so_the_row_is_scoreable(self, log_db):
        """Without it the row is in the corpus and absent from every report
        -- the join is on a resolved outcome."""
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            backfill.run()
        row = repo.rows()[0]
        assert row["outcome"] == "loss"
        assert row["realised_r"] == -1.0
        assert row["resolved_at"] is not None


class TestWhatItRefusesToInvent:
    def test_the_event_calendar_abstains(self, log_db):
        """Past calendar state is not recoverable, and a guess would be
        indistinguishable from a measurement."""
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            backfill.run()
        assert repo.rows()[0]["event_blocked"] is None

    def test_an_OPEN_trade_is_not_reconstructed(self, log_db):
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past(status="open", net_pnl=None)]):
            assert backfill.run() == 0
        assert repo.rows() == []

    def test_nothing_that_was_BLOCKED_is_invented(self, log_db):
        """Nothing recorded the blocks. A reconstruction of them would make
        every challenger look better than the champion for free."""
        src = backfill.__doc__ or ""
        assert "executed" in src.lower()
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            backfill.run()
        assert all(r["executed"] == 1 for r in repo.rows())


class TestItCanBeRunTwice:
    def test_running_it_again_adds_nothing(self, log_db):
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            first = backfill.run()
            second = backfill.run()
        assert first == 1
        assert second == 0
        assert len(repo.rows()) == 1

    def test_it_does_not_overwrite_a_LIVE_row_for_the_same_message(self, log_db):
        """A live observation is better evidence than a reconstruction of
        it, and the live row is the one already in the table."""
        repo.insert_decision({"tg_message_id": "21423", "path": "auto",
                              "decided_at": time.time(), "executed": 1,
                              "spread_points": 11.0})
        with mock.patch.object(backfill, "_past_telegram_trades",
                               return_value=[_past()]):
            backfill.run()
        rows = repo.rows()
        assert len(rows) == 1
        assert rows[0]["source"] == "live"
        assert rows[0]["spread_points"] == 11.0

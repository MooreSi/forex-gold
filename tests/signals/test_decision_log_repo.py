"""Storage contract for the Telegram decision log (stage 0).

Expected behaviour, from docs/todo/signal-validation/010, not from the code:

  * It lives in the Reversal Engine's shared database, for the reason
    pro_corpus_repo documents -- the core DB is per-environment, so a corpus
    kept there splits in half the day the account switches.
  * One row per (message, path). The full-signal path and the IME path are
    different decisions about the same message and both are worth having; a
    rescan of the same one is not.
  * A decision with no trade is still a row. "Blocked" is the half of the
    study that is free.
  * Outcome columns stay NULL until the sweep fills them, and NULL means
    "not known yet", never "no result".
"""
import os
import tempfile
import time

import pytest

from backend.src.services.signals import decision_log_repo as repo
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file


@pytest.fixture
def log():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    repo.create_schema()
    yield repo
    re_repo.close_db()
    remove_db_file(path)


def decision(msg_id="21429", path="auto", **over):
    row = {
        "tg_message_id": msg_id, "path": path,
        "channel_name": "Gold Diggers VIP", "direction": "BUY",
        "decided_at": time.time(), "executed": 1, "skip_reason": "",
        "strategy": "scale_out", "entry_low": 4100.0, "entry_high": 4102.0,
        "stop_loss": 4090.0, "tp1": 4120.0,
        "bid": 4101.0, "ask": 4101.3, "spread_points": 30.0, "price": 4101.3,
        "session": "london", "trade_id": "trade-1", "account_env": "demo",
        "liquidity_blocked": 0, "event_blocked": 0,
    }
    row.update(over)
    return row


class TestOneRowPerMessagePerPath:
    def test_a_decision_is_stored_and_returned_by_id(self, log):
        did = log.insert_decision(decision())
        assert did is not None
        rows = log.rows()
        assert len(rows) == 1
        assert rows[0]["tg_message_id"] == "21429"
        assert rows[0]["path"] == "auto"

    def test_the_same_message_on_the_same_path_is_not_duplicated(self, log):
        first = log.insert_decision(decision())
        second = log.insert_decision(decision(skip_reason="different"))
        assert len(log.rows()) == 1
        assert second == first, "the existing row's id must come back"

    def test_the_SAME_message_on_the_OTHER_path_is_a_separate_row(self, log):
        """IME and the full-signal path decide the same message differently
        and both decisions are evidence."""
        log.insert_decision(decision(path="auto"))
        log.insert_decision(decision(path="ime"))
        assert len(log.rows()) == 2

    def test_a_BLOCKED_decision_is_recorded_too(self, log):
        """The blocked half is the free half of the study. A log that only
        kept executions could never say what a gate would have saved."""
        log.insert_decision(decision(executed=0, trade_id=None,
                                     skip_reason="blocked by news"))
        row = log.rows()[0]
        assert row["executed"] == 0
        assert row["trade_id"] is None
        assert "news" in row["skip_reason"]


class TestOutcomesAreFilledInLater:
    def test_outcome_columns_start_NULL(self, log):
        log.insert_decision(decision())
        row = log.rows()[0]
        assert row["outcome"] is None
        assert row["realised_r"] is None
        assert row["resolved_at"] is None

    def test_only_executed_rows_with_a_trade_are_offered_for_resolution(self, log):
        log.insert_decision(decision(msg_id="a", executed=1, trade_id="t-1"))
        log.insert_decision(decision(msg_id="b", executed=0, trade_id=None))
        log.insert_decision(decision(msg_id="c", executed=1, trade_id=None))
        pending = log.unresolved_outcomes()
        assert [p["tg_message_id"] for p in pending] == ["a"]

    def test_a_resolved_row_leaves_the_queue(self, log):
        did = log.insert_decision(decision())
        log.set_outcome(did, outcome="loss", net_usd=-50.4, realised_r=-1.0,
                        max_tp_hit="none", exit_reason="SL")
        assert log.unresolved_outcomes() == []
        row = log.rows()[0]
        assert row["outcome"] == "loss"
        assert row["realised_r"] == -1.0
        assert row["resolved_at"] is not None


class TestShadowRows:
    def test_one_row_per_variant_and_no_duplicates(self, log):
        did = log.insert_decision(decision())
        log.insert_shadow(did, "session liquidity", True, "")
        log.insert_shadow(did, "session liquidity", False, "changed my mind")
        rows = log.shadow_rows_for(did)
        assert len(rows) == 1
        assert rows[0]["would_take"] == 1

    def test_an_abstention_is_stored_as_NULL_not_false(self, log):
        """A variant that had no fact must not be counted as a refusal."""
        did = log.insert_decision(decision())
        log.insert_shadow(did, "trend (HTF bias)", None, "bias unavailable")
        assert log.shadow_rows_for(did)[0]["would_take"] is None

    def test_a_decision_leaves_the_evaluation_queue_once_marked(self, log):
        did = log.insert_decision(decision())
        assert [r["id"] for r in log.unevaluated_shadow()] == [did]
        log.mark_shadow_evaluated(did)
        assert log.unevaluated_shadow() == []


class TestTheOutcomeQueueDoesNotFillUpForever:
    """A trade that never closes -- held open indefinitely, or a trade_id the
    ledger never received -- would otherwise sit at the head of a query that
    is ORDER BY decided_at LIMIT n, and enough of them would stall the sweep
    behind rows it can never resolve."""

    def test_a_decision_older_than_the_cap_leaves_the_queue(self, log):
        old = time.time() - (repo.MAX_RESOLVE_AGE_S + 3600)
        log.insert_decision(decision(msg_id="ancient", decided_at=old))
        assert log.unresolved_outcomes() == []

    def test_it_stays_UNRESOLVED_rather_than_being_scored(self, log):
        """Giving up is not the same as a flat trade. The row keeps a NULL
        outcome so the report still excludes it."""
        old = time.time() - (repo.MAX_RESOLVE_AGE_S + 3600)
        log.insert_decision(decision(msg_id="ancient", decided_at=old))
        assert log.rows()[0]["outcome"] is None

    def test_a_recent_decision_is_still_queued(self, log):
        log.insert_decision(decision(msg_id="recent"))
        assert [r["tg_message_id"] for r in log.unresolved_outcomes()] == ["recent"]

    def test_the_cap_is_wide_enough_for_a_trade_held_over_a_weekend(self, log):
        assert repo.MAX_RESOLVE_AGE_S >= 7 * 86400

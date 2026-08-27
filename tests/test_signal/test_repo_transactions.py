"""Repo-specific tests for test_signal_repo.py: atomicity proof for the
consolidated close_signal_with_balance_update, plus the two new named
functions replacing raw-SQL bypasses. See
docs/todo/refactor/test-signal-migration/020-*.md.
"""
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from backend.src.services.test_signal import test_signal_repo as repo
from backend.src.services.test_signal.test_signal_repo import get_db
from tests.conftest import remove_db_file


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    remove_db_file(path)


def _sig(**overrides) -> dict:
    base = {
        "direction": "BUY",
        "entry_low": 2399.0, "entry_high": 2401.0, "entry_mid": 2400.0,
        "stop_loss": 2390.0,
    }
    base.update(overrides)
    return base


# ── close_signal_with_balance_update ──────────────────────────────────────────

def test_close_signal_with_balance_update_matches_hand_calculation(fresh_repo):
    sig_id, ref = fresh_repo.insert_signal(_sig())
    fresh_repo.update_signal_triggered(sig_id, 2400.0)

    new_bal = fresh_repo.close_signal_with_balance_update(
        sig_id, "win", 2410.5, pnl_pts=10.0, pnl_dollars=96.50,
        signal_ref=ref, direction="BUY",
    )
    assert new_bal == 1096.50
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["status"] == "closed"
    assert row["pnl_dollars"] == 96.50
    assert row["balance_after"] == 1096.50
    assert fresh_repo.get_virtual_balance() == 1096.50

    log = fresh_repo.get_balance_log()
    assert len(log) == 1
    assert log[0]["change_amount"] == 96.50


def test_close_signal_with_balance_update_rolls_back_all_four_writes_on_mid_failure(fresh_repo):
    """The killer test for this task: database.py's equivalent is 4 fully
    independent connections with zero atomicity -- a crash between any two
    of them leaves the balance, the log, and the signal row disagreeing.
    Forces a failure mid-sequence and proves everything rolls back together."""
    sig_id, ref = fresh_repo.insert_signal(_sig())
    balance_before = fresh_repo.get_virtual_balance()

    real_run = get_db().run
    call_count = {"n": 0}

    def _fail_on_third_call(sql, *params):
        call_count["n"] += 1
        if call_count["n"] == 3:  # after set_virtual_balance, before log_balance
            raise RuntimeError("simulated crash mid-close")
        return real_run(sql, *params)

    with patch.object(get_db(), "run", side_effect=_fail_on_third_call):
        with pytest.raises(RuntimeError):
            fresh_repo.close_signal_with_balance_update(
                sig_id, "win", 2410.0, pnl_pts=10.0, pnl_dollars=100.0,
                signal_ref=ref, direction="BUY",
            )

    # Nothing should have survived: balance unchanged, no log entry, signal
    # still open -- not a partial write.
    assert fresh_repo.get_virtual_balance() == balance_before
    assert fresh_repo.get_balance_log() == []
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["status"] == "pending"


def test_close_signal_with_balance_update_accepts_precomputed_learning_note(fresh_repo):
    sig_id, ref = fresh_repo.insert_signal(_sig())
    fresh_repo.close_signal_with_balance_update(
        sig_id, "win", 2410.0, pnl_pts=10.0, pnl_dollars=100.0,
        signal_ref=ref, direction="BUY", learning_note="entered on a clean retest",
    )
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["learning_note"] == "entered on a clean retest"


# ── New named functions ───────────────────────────────────────────────────────

def test_get_recent_outcomes_by_direction_filters_and_orders(fresh_repo):
    s1, r1 = fresh_repo.insert_signal(_sig(direction="BUY"))
    fresh_repo.close_signal_with_balance_update(s1, "loss", 2390.0, -10.0, -100.0, r1, "BUY")
    s2, r2 = fresh_repo.insert_signal(_sig(direction="SELL", entry_mid=2400.0, stop_loss=2410.0))
    fresh_repo.close_signal_with_balance_update(s2, "win", 2390.0, 10.0, 100.0, r2, "SELL")
    s3, r3 = fresh_repo.insert_signal(_sig(direction="BUY"))
    fresh_repo.close_signal_with_balance_update(s3, "loss", 2390.0, -10.0, -100.0, r3, "BUY")

    outcomes = fresh_repo.get_recent_outcomes_by_direction("BUY", since_ts=0, limit=10)
    assert outcomes == ["loss", "loss"]


def test_get_closed_signals_with_mt5_ticket(fresh_repo):
    no_ticket, r1 = fresh_repo.insert_signal(_sig())
    fresh_repo.close_signal_with_balance_update(no_ticket, "win", 2410.0, 10.0, 100.0, r1, "BUY")

    with_ticket, r2 = fresh_repo.insert_signal(_sig())
    fresh_repo.update_live_exec_result(with_ticket, 999, "v1", "success")
    fresh_repo.close_signal_with_balance_update(with_ticket, "win", 2410.0, 10.0, 100.0, r2, "BUY")

    still_open, r3 = fresh_repo.insert_signal(_sig())
    fresh_repo.update_live_exec_result(still_open, 111, "v2", "success")  # not closed -- excluded

    ids = {r["id"] for r in fresh_repo.get_closed_signals_with_mt5_ticket()}
    assert ids == {with_ticket}

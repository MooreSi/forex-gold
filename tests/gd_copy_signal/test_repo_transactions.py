"""Repo-specific tests: proves gd_copy_signal_repo.py's transaction()
wrapping actually fixes the atomicity gap database.py has -- see
docs/todo/refactor/backend-foundation/030-migrate-gd-copy-repo-layer.md.

Not parametrized against database.py -- these scenarios (forcing a crash
mid-write to check both statements roll back together) are exactly what
the OLD module can't do correctly, so there's nothing meaningful to prove
equivalence against here. This file is the proof the fix landed.
"""
import os
import tempfile
from unittest.mock import patch

import pytest

from forex_trader.gd_copy_signal import gd_copy_signal_repo as repo
from forex_trader.src.db.connection import get_db


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    os.remove(path)


def test_close_signal_rolls_back_status_and_balance_together_on_mid_write_failure(fresh_repo):
    """Simulates a crash between the status update and the balance write
    inside close_signal(). database.py's equivalent (three independent
    connections) would leave the status update committed with the balance
    never touched. The repo's single transaction() must roll back both."""
    sig_id = fresh_repo.create_signal({"direction": "BUY", "signal_ref": "s"})
    balance_before = fresh_repo.get_virtual_balance()

    real_run = get_db().run
    call_count = {"n": 0}

    def _run_that_fails_on_third_call(sql, *params):
        call_count["n"] += 1
        if call_count["n"] == 3:  # after the status UPDATE and the balance_log INSERT
            raise RuntimeError("simulated crash mid-close_signal")
        return real_run(sql, *params)

    with patch.object(get_db(), "run", side_effect=_run_that_fails_on_third_call):
        with pytest.raises(RuntimeError):
            fresh_repo.close_signal(sig_id, 2450.0, "win", net_pnl_dollars=25.0)

    # Both the status change AND the balance change must have rolled back --
    # neither is allowed to survive alone.
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["status"] == "pending", "status update should have rolled back"
    assert fresh_repo.get_virtual_balance() == balance_before, "balance change should have rolled back"


def test_book_partial_close_rolls_back_signal_row_and_balance_together(fresh_repo):
    sig_id = fresh_repo.create_signal({"direction": "BUY", "signal_ref": "s"})
    balance_before = fresh_repo.get_virtual_balance()

    real_run = get_db().run
    call_count = {"n": 0}

    def _run_that_fails_on_second_call(sql, *params):
        call_count["n"] += 1
        if call_count["n"] == 2:  # after the signal-row UPDATE, before balance_log INSERT
            raise RuntimeError("simulated crash mid-book_partial_close")
        return real_run(sql, *params)

    with patch.object(get_db(), "run", side_effect=_run_that_fails_on_second_call):
        with pytest.raises(RuntimeError):
            fresh_repo.book_partial_close(sig_id, leg_net_dollars=10.0, frac_closed=0.33, tp_idx=1)

    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["remaining_frac"] in (None, 1.0), "partial-close row update should have rolled back"
    assert fresh_repo.get_virtual_balance() == balance_before, "balance change should have rolled back"


def test_close_signal_succeeds_normally_without_injected_failure(fresh_repo):
    """Sanity check the mock-patching above isn't masking a real bug --
    the unpatched path must still work end to end."""
    sig_id = fresh_repo.create_signal({"direction": "BUY", "signal_ref": "s"})
    fresh_repo.close_signal(sig_id, 2450.0, "win", net_pnl_dollars=25.0)
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["status"] == "closed"
    assert fresh_repo.get_virtual_balance() == 1025.0


def test_get_recent_outcomes_by_direction_filters_and_orders(fresh_repo):
    """New repo-only function (added in 040) replacing a raw sqlite3 query
    that used to live inline in engine.py, bypassing database.py's API."""
    s1 = fresh_repo.create_signal({"direction": "BUY", "signal_ref": "s1"})
    fresh_repo.close_signal(s1, 2400.0, "loss", net_pnl_dollars=-10.0)
    s2 = fresh_repo.create_signal({"direction": "SELL", "signal_ref": "s2"})
    fresh_repo.close_signal(s2, 2400.0, "win", net_pnl_dollars=10.0)  # different direction -- excluded
    s3 = fresh_repo.create_signal({"direction": "BUY", "signal_ref": "s3"})
    fresh_repo.close_signal(s3, 2400.0, "loss", net_pnl_dollars=-5.0)

    outcomes = fresh_repo.get_recent_outcomes_by_direction("BUY", since_ts=0, limit=10)
    assert outcomes == ["loss", "loss"]  # s3 newest first, s2 excluded (wrong direction)

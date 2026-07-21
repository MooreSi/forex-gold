"""Repo-specific tests for breakout_signal_repo.py: atomicity proof +
the three new named functions that replace engine.py's raw-SQL bypasses.
See docs/todo/refactor/breakout-signal-migration/020-*.md.
"""
import os
import tempfile
import time
from unittest.mock import patch

import pytest

from forex_trader.breakout_signal import breakout_signal_repo as repo
from forex_trader.breakout_signal.breakout_signal_repo import get_db


@pytest.fixture
def fresh_repo():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    os.remove(path)


def _sig(**overrides) -> dict:
    base = {
        "direction": "BUY", "breakout_type": "go",
        "entry_mid": 2400.0, "stop_loss": 2390.0, "signal_ref": "BO-TEST",
    }
    base.update(overrides)
    return base


# ── Atomicity ─────────────────────────────────────────────────────────────────

def test_close_signal_rolls_back_status_and_balance_together_on_mid_write_failure(fresh_repo):
    sig_id = fresh_repo.create_signal(_sig())
    balance_before = fresh_repo.get_virtual_balance()

    real_run = get_db().run
    call_count = {"n": 0}

    def _fail_on_third_call(sql, *params):
        call_count["n"] += 1
        if call_count["n"] == 3:
            raise RuntimeError("simulated crash mid-close_signal")
        return real_run(sql, *params)

    with patch.object(get_db(), "run", side_effect=_fail_on_third_call):
        with pytest.raises(RuntimeError):
            fresh_repo.close_signal(sig_id, 2410.0, "win")

    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["status"] == "pending"
    assert fresh_repo.get_virtual_balance() == balance_before


def test_book_partial_close_rolls_back_together_on_mid_write_failure(fresh_repo):
    sig_id = fresh_repo.create_signal(_sig())
    balance_before = fresh_repo.get_virtual_balance()

    real_run = get_db().run
    call_count = {"n": 0}

    def _fail_on_second_call(sql, *params):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated crash mid-book_partial_close")
        return real_run(sql, *params)

    with patch.object(get_db(), "run", side_effect=_fail_on_second_call):
        with pytest.raises(RuntimeError):
            fresh_repo.book_partial_close(sig_id, 10.0, 0.33, "TP1")

    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["remaining_frac"] in (None, 1.0)
    assert fresh_repo.get_virtual_balance() == balance_before


def test_close_signal_succeeds_normally_without_injected_failure(fresh_repo):
    sig_id = fresh_repo.create_signal(_sig())
    fresh_repo.close_signal(sig_id, 2410.0, "win")
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["status"] == "closed"
    assert fresh_repo.get_virtual_balance() == 1100.0


# ── New named functions (replacing raw-SQL bypasses in engine.py) ────────────

def test_get_last_signal_time_for_level(fresh_repo):
    assert fresh_repo.get_last_signal_time_for_level("BUY", 2450.0, 0) is None
    fresh_repo.create_signal(_sig(direction="BUY", broken_level=2450.0))
    result = fresh_repo.get_last_signal_time_for_level("BUY", 2450.0, 0)
    assert result is not None
    assert result > 0


def test_get_last_signal_time_for_level_respects_direction_and_cutoff(fresh_repo):
    fresh_repo.create_signal(_sig(direction="SELL", broken_level=2450.0, signal_ref="s1"))
    assert fresh_repo.get_last_signal_time_for_level("BUY", 2450.0, 0) is None
    future_cutoff = time.time() + 3600
    fresh_repo.create_signal(_sig(direction="BUY", broken_level=2450.0, signal_ref="s2"))
    assert fresh_repo.get_last_signal_time_for_level("BUY", 2450.0, future_cutoff) is None


def test_get_recent_outcomes_by_direction_filters_and_orders(fresh_repo):
    s1 = fresh_repo.create_signal(_sig(direction="BUY", signal_ref="s1"))
    fresh_repo.close_signal(s1, 2395.0, "loss")  # BUY entry 2400 -> loss
    s2 = fresh_repo.create_signal(_sig(direction="SELL", signal_ref="s2", entry_mid=2400.0, stop_loss=2410.0))
    fresh_repo.close_signal(s2, 2410.0, "win")  # different direction -- excluded
    s3 = fresh_repo.create_signal(_sig(direction="BUY", signal_ref="s3"))
    fresh_repo.close_signal(s3, 2395.0, "loss")

    outcomes = fresh_repo.get_recent_outcomes_by_direction("BUY", since_ts=0, limit=10)
    assert outcomes == ["loss", "loss"]


def test_set_stop_loss(fresh_repo):
    sig_id = fresh_repo.create_signal(_sig())
    fresh_repo.set_stop_loss(sig_id, 2405.123)
    row = fresh_repo.get_signal_by_id(sig_id)
    assert row["stop_loss"] == 2405.123


def test_get_closed_or_expired_signals_with_mt5_ticket(fresh_repo):
    pending = fresh_repo.create_signal(_sig(signal_ref="pending"))
    fresh_repo.update_live_exec_result(pending, 111, "v1", "success")  # has ticket but still pending -- excluded

    closed_no_ticket = fresh_repo.create_signal(_sig(signal_ref="closed_no_ticket"))
    fresh_repo.close_signal(closed_no_ticket, 2410.0, "win")

    closed_with_ticket = fresh_repo.create_signal(_sig(signal_ref="closed_with_ticket"))
    fresh_repo.update_live_exec_result(closed_with_ticket, 222, "v2", "success")
    fresh_repo.close_signal(closed_with_ticket, 2410.0, "win")

    expired_with_ticket = fresh_repo.create_signal(_sig(signal_ref="expired_with_ticket"))
    fresh_repo.update_live_exec_result(expired_with_ticket, 333, "v3", "success")
    fresh_repo.expire_signal(expired_with_ticket)

    ids = {r["id"] for r in fresh_repo.get_closed_or_expired_signals_with_mt5_ticket()}
    assert ids == {closed_with_ticket, expired_with_ticket}


def test_promote_expired_to_closed(fresh_repo):
    sig_id = fresh_repo.create_signal(_sig())
    fresh_repo.expire_signal(sig_id)
    assert fresh_repo.get_signal_by_id(sig_id)["status"] == "expired"
    fresh_repo.promote_expired_to_closed(sig_id)
    assert fresh_repo.get_signal_by_id(sig_id)["status"] == "closed"

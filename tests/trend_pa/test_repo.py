"""Trend PA's own database. A temp file per test, closed before it is removed
(Windows refuses to unlink a file with an open handle -- CLAUDE.md)."""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.trend_pa import repo


@pytest.fixture
def db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    repo.init(path)
    yield repo
    repo.close_db()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(path + suffix)
        except FileNotFoundError:
            pass


def _sig(**kw):
    base = {"created_at": 100.0, "direction": "BUY", "pattern": "pin",
            "session": "london", "level": 1999.0, "level_kind": "swing_low",
            "entry": 2000.0, "stop_loss": 1998.0, "take_profit": 2004.0,
            "risk": 2.0, "atr_m15": 2.0, "spread": 0.2,
            "features": {"wick_frac": 0.7}, "ml_prob": None, "origin": "live"}
    return {**base, **kw}


def test_a_new_signal_is_open_and_has_a_reference(db):
    sid = db.insert_signal(_sig())
    [row] = db.open_signals()
    assert row["id"] == sid and row["status"] == "open"
    assert row["signal_ref"] == f"TPA-{sid:04d}"
    assert row["features"] == {"wick_frac": 0.7}


def test_closing_records_the_outcome_and_leaves_the_open_list(db):
    sid = db.insert_signal(_sig())
    db.close_signal(sid, "win", 2004.0, 200.0, 1.85)
    assert db.open_signals() == []
    [row] = db.closed_signals()
    assert (row["outcome"], row["exit_price"], row["closed_at"], row["r_net"]) == \
        ("win", 2004.0, 200.0, pytest.approx(1.85))


def test_closing_twice_does_not_rewrite_the_first_result(db):
    """The outcome loop and a restart can both reach the same signal."""
    sid = db.insert_signal(_sig())
    assert db.close_signal(sid, "win", 2004.0, 200.0, 1.85) is True
    assert db.close_signal(sid, "loss", 1998.0, 300.0, -1.15) is False
    assert db.closed_signals()[0]["outcome"] == "win"


def test_live_and_backtest_are_kept_apart(db):
    db.insert_signal(_sig())
    db.replace_backtest([{**_sig(origin="backtest"), "outcome": "loss",
                          "exit_price": 1998.0, "closed_at": 150.0, "r_net": -1.15}])
    assert len(db.closed_signals(origin="backtest")) == 1
    assert db.closed_signals(origin="live") == []
    assert len(db.open_signals()) == 1


def test_a_new_backtest_replaces_the_old_one_only(db):
    live = db.insert_signal(_sig())
    db.close_signal(live, "win", 2004.0, 200.0, 1.85)
    row = {**_sig(origin="backtest"), "outcome": "win", "exit_price": 2004.0,
           "closed_at": 150.0, "r_net": 1.85}
    db.replace_backtest([row, row])
    db.replace_backtest([row])
    assert len(db.closed_signals(origin="backtest")) == 1
    assert len(db.closed_signals(origin="live")) == 1


def test_the_analysis_log_keeps_the_newest_first(db):
    db.log_analysis("no clear H4 trend", ts=1.0)
    db.log_analysis("outside London/New York", ts=2.0)
    assert [r["reason"] for r in db.analysis_log(10)] == [
        "outside London/New York", "no clear H4 trend"]


def test_live_execution_results_are_recorded(db):
    sid = db.insert_signal(_sig())
    db.update_live_exec(sid, "success", mt5_ticket=123, vantage_signal_id="v1")
    row = db.recent_signals(5)[0]
    assert (row["live_exec_status"], row["mt5_ticket"], row["vantage_signal_id"]) == \
        ("success", 123, "v1")


def test_config_round_trips(db):
    assert db.get_config("user_stopped", "0") == "0"
    db.set_config("user_stopped", "1")
    assert db.get_config("user_stopped") == "1"


def test_the_last_signal_time_per_direction(db):
    db.insert_signal(_sig(created_at=100.0))
    db.insert_signal(_sig(created_at=300.0, direction="SELL"))
    assert db.last_signal_time("BUY") == 100.0
    assert db.last_signal_time("SELL") == 300.0
    assert db.last_signal_time("SELL", origin="backtest") is None

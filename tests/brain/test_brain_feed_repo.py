"""The brain feed's SQL, against real (private, temporary) engine databases.

The service tests fake these reads; this is what proves the column names are
the ones the tables really have. Nothing here reaches a broker.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo as bo_db
from backend.src.services.brain import feed_repo
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from backend.src.services.signals import decision_log_repo
from tests.conftest import remove_db_file


@pytest.fixture
def dbs():
    paths = []
    for init in (re_db.init, bo_db.init):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        init(path)
        paths.append(path)
    decision_log_repo.create_schema()
    yield
    re_db.close_db()
    bo_db.close_db()
    for p in paths:
        remove_db_file(p)


def test_telegram_decisions_newest_first(dbs):
    for i, (ts, executed, reason) in enumerate([(100.0, 1, None), (200.0, 0, "max open trades (3) reached")]):
        decision_log_repo.insert_decision({
            "tg_message_id": f"m{i}", "path": "auto", "channel_name": "GOLD X",
            "direction": "BUY", "decided_at": ts, "executed": executed, "skip_reason": reason,
        })
    rows = feed_repo.recent_telegram(10)
    assert [r["ts"] for r in rows] == [200.0, 100.0]
    assert rows[0]["source"] == "GOLD X"
    assert rows[0]["reason"] == "max open trades (3) reached"
    assert rows[1]["executed"] == 1


def test_reversal_rows_are_only_the_decided_ones(dbs):
    decided = re_db.create_signal({"direction": "SELL", "entry_low": 1.0, "entry_high": 2.0,
                                   "stop_loss": 3.0, "created_at": 50.0,
                                   "live_exec_status": "ml_skipped"})
    re_db.create_signal({"direction": "BUY", "entry_low": 1.0, "entry_high": 2.0,
                         "stop_loss": 0.5, "created_at": 60.0})
    rows = feed_repo.recent_reversal(10)
    assert [r["id"] for r in rows] == [decided]
    assert rows[0]["status"] == "ml_skipped"
    assert rows[0]["ts"] == 50.0


def test_breakout_rows_are_only_the_decided_ones(dbs):
    sid = bo_db.create_signal({"direction": "BUY", "breakout_type": "go", "entry_mid": 2400.0,
                               "stop_loss": 2390.0, "signal_ref": "BO-1"})
    bo_db.create_signal({"direction": "BUY", "breakout_type": "go", "entry_mid": 2400.0,
                         "stop_loss": 2390.0, "signal_ref": "BO-2"})
    bo_db.get_db().run("UPDATE bo_signals SET live_exec_status='success' WHERE id=?", sid)
    rows = feed_repo.recent_breakout(10)
    assert [r["id"] for r in rows] == [sid]
    assert rows[0]["status"] == "success"

"""Characterizes create_signal/get_signals/activate_signal/cancel_signal on
SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-signal-crud-migration/010-*.md.

None of these four methods use `self` -- called via the class directly
(SimulationEngine.create_signal(None, ...)) rather than constructing a full
engine instance, which would need a live bridge/config.
"""
import json
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime


def _reset_thread_local_connection():
    """db_module caches a thread-local sqlite3 connection -- db.init(path)
    alone does NOT close/replace it. Must be reset for tests to be isolated."""
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


# ── create_signal ─────────────────────────────────────────────────────────────

def test_create_signal_happy_path(fresh_db):
    result = TradingRuntime.create_signal(
        None, source_name="Manual", direction="buy",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    assert result["status"] == "pending"
    assert result["signal_id"]

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?",
                        (result["signal_id"],)).fetchone()
        )
    assert row["direction"] == "BUY"  # uppercased
    assert row["status"] == "pending"
    assert row["entry_low"] == 2399.0
    assert row["tp1"] == 2410.0


def test_create_signal_raises_on_validation_error(fresh_db):
    with pytest.raises(ValueError):
        TradingRuntime.create_signal(
            None, source_name="Manual", direction="BUY",
            entry_low=2401.0, entry_high=2399.0,  # low > high -- invalid
            stop_loss=2390.0, tp1=2410.0,
        )


def test_create_signal_requires_tp1_when_setting_enabled(fresh_db):
    # require_at_least_tp1 defaults to 1 (enabled)
    with pytest.raises(ValueError, match="TP1"):
        TradingRuntime.create_signal(
            None, source_name="Manual", direction="BUY",
            entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=None,
        )


def test_create_signal_allows_missing_tp1_when_setting_disabled(fresh_db):
    db.update_risk_settings({"require_at_least_tp1": 0})
    result = TradingRuntime.create_signal(
        None, source_name="Manual", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=None,
    )
    assert result["status"] == "pending"


# ── get_signals ───────────────────────────────────────────────────────────────

def test_get_signals_returns_all_newest_first(fresh_db):
    r1 = TradingRuntime.create_signal(
        None, source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    r2 = TradingRuntime.create_signal(
        None, source_name="B", direction="SELL",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2410.0, tp1=2390.0,
    )
    signals = TradingRuntime.get_signals(None)
    ids = [s["signal_id"] for s in signals]
    assert ids[0] == r2["signal_id"]  # newest first
    assert ids[1] == r1["signal_id"]


def test_get_signals_parses_claude_commentary_json(fresh_db):
    r1 = TradingRuntime.create_signal(
        None, source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET claude_commentary=? WHERE signal_id=?",
            (json.dumps({"take": "bullish"}), r1["signal_id"]),
        )
    signals = TradingRuntime.get_signals(None)
    match = next(s for s in signals if s["signal_id"] == r1["signal_id"])
    assert match["claude_commentary"] == {"take": "bullish"}


def test_get_signals_leaves_bad_json_commentary_alone(fresh_db):
    r1 = TradingRuntime.create_signal(
        None, source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET claude_commentary=? WHERE signal_id=?",
            ("not valid json", r1["signal_id"]),
        )
    signals = TradingRuntime.get_signals(None)
    match = next(s for s in signals if s["signal_id"] == r1["signal_id"])
    assert match["claude_commentary"] == "not valid json"


# ── activate_signal ───────────────────────────────────────────────────────────

# ── cancel_signal ─────────────────────────────────────────────────────────────

def test_cancel_signal_sets_cancelled_status(fresh_db):
    r1 = TradingRuntime.create_signal(
        None, source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    TradingRuntime.cancel_signal(None, r1["signal_id"])
    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?",
                        (r1["signal_id"],)).fetchone()
        )
    assert row["status"] == "cancelled"
    assert row["cancelled_at"] is not None

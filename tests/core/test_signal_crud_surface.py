"""Proves backend.src.services.signals.repo's extracted functions behave
identically to the SimulationEngine methods characterized in
test_signal_crud_characterization.py -- see
docs/todo/refactor/core-signal-crud-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class.
"""
import json
import os
import tempfile

import pytest

from forex_trader.core import database as db
from backend.src.services.signals import repo as sig


def _reset_thread_local_connection():
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
    result = sig.create_signal(
        source_name="Manual", direction="buy",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    assert result["status"] == "pending"
    assert result["signal_id"]

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?",
                        (result["signal_id"],)).fetchone()
        )
    assert row["direction"] == "BUY"
    assert row["status"] == "pending"
    assert row["entry_low"] == 2399.0
    assert row["tp1"] == 2410.0


def test_create_signal_raises_on_validation_error(fresh_db):
    with pytest.raises(ValueError):
        sig.create_signal(
            source_name="Manual", direction="BUY",
            entry_low=2401.0, entry_high=2399.0,
            stop_loss=2390.0, tp1=2410.0,
        )


def test_create_signal_requires_tp1_when_setting_enabled(fresh_db):
    with pytest.raises(ValueError, match="TP1"):
        sig.create_signal(
            source_name="Manual", direction="BUY",
            entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=None,
        )


def test_create_signal_allows_missing_tp1_when_setting_disabled(fresh_db):
    db.update_risk_settings({"require_at_least_tp1": 0})
    result = sig.create_signal(
        source_name="Manual", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=None,
    )
    assert result["status"] == "pending"


# ── get_signals ───────────────────────────────────────────────────────────────

def test_get_signals_returns_all_newest_first(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    r2 = sig.create_signal(
        source_name="B", direction="SELL",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2410.0, tp1=2390.0,
    )
    signals = sig.get_signals()
    ids = [s["signal_id"] for s in signals]
    assert ids[0] == r2["signal_id"]
    assert ids[1] == r1["signal_id"]


def test_get_signals_filters_by_status(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    sig.create_signal(
        source_name="B", direction="SELL",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2410.0, tp1=2390.0,
    )
    sig.activate_signal(r1["signal_id"])

    active = sig.get_signals(status="active")
    assert len(active) == 1
    assert active[0]["signal_id"] == r1["signal_id"]


def test_get_signals_parses_claude_commentary_json(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET claude_commentary=? WHERE signal_id=?",
            (json.dumps({"take": "bullish"}), r1["signal_id"]),
        )
    signals = sig.get_signals()
    match = next(s for s in signals if s["signal_id"] == r1["signal_id"])
    assert match["claude_commentary"] == {"take": "bullish"}


def test_get_signals_leaves_bad_json_commentary_alone(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    with db.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET claude_commentary=? WHERE signal_id=?",
            ("not valid json", r1["signal_id"]),
        )
    signals = sig.get_signals()
    match = next(s for s in signals if s["signal_id"] == r1["signal_id"])
    assert match["claude_commentary"] == "not valid json"


# ── activate_signal ───────────────────────────────────────────────────────────

def test_activate_signal_transitions_pending_to_active(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    sig.activate_signal(r1["signal_id"])
    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?",
                        (r1["signal_id"],)).fetchone()
        )
    assert row["status"] == "active"
    assert row["activated_at"] is not None


def test_activate_signal_raises_on_unknown_id(fresh_db):
    with pytest.raises(ValueError, match="not found"):
        sig.activate_signal("does-not-exist")


def test_activate_signal_raises_when_not_pending(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    sig.activate_signal(r1["signal_id"])
    with pytest.raises(ValueError, match="cannot activate"):
        sig.activate_signal(r1["signal_id"])


# ── cancel_signal ─────────────────────────────────────────────────────────────

def test_cancel_signal_sets_cancelled_status(fresh_db):
    r1 = sig.create_signal(
        source_name="A", direction="BUY",
        entry_low=2399.0, entry_high=2401.0, stop_loss=2390.0, tp1=2410.0,
    )
    sig.cancel_signal(r1["signal_id"])
    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?",
                        (r1["signal_id"],)).fetchone()
        )
    assert row["status"] == "cancelled"
    assert row["cancelled_at"] is not None

"""Proves backend.src.services.dpm.bookkeeping's extracted functions behave
identically to the SimulationEngine methods characterized in
test_dpm_bookkeeping_characterization.py -- see
docs/todo/refactor/core-dpm-bookkeeping-migration/020-*.md.

Same assertions as 010, called through the new module instead of the class.
_FakeEngine (self._dpm_calibrated/_dpm_cal_loaded_at/_dpm_recorded) is
replaced by a real DPMCache instance.
"""
import os
import tempfile
import time

import pytest

from forex_trader.core import database as db
from backend.src.services.dpm import bookkeeping as dpm


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


def _insert_calibration(session="London", bucket="strong", calibrated_at=None,
                        be_mult=1.5, trail_mult=1.2, tp1_pct=0.4, sample_size=20):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO dpm_calibration (calibrated_at, session, momentum_bucket, "
            "be_multiplier, trail_multiplier, tp1_partial_pct, sample_size) "
            "VALUES (?,?,?,?,?,?,?)",
            (calibrated_at or time.time(), session, bucket, be_mult, trail_mult,
             tp1_pct, sample_size),
        )


def _dpm_params(**overrides):
    p = {
        "atr": 8.0, "session": "London", "momentum": 0.6, "momentum_label": "strong",
        "regime": "trending", "adx": 30.0, "be_multiplier": 1.5, "trail_multiplier": 1.2,
        "be_trigger_usd": 20.0, "trail_distance": 15.0, "tp1_partial_pct": 0.4,
        "used_calibrated": True,
    }
    p.update(overrides)
    return p


def _insert_dpm_row(trade_id, entry_price=2400.0, original_sl=2390.0, lot_size=0.10,
                    opened_at=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO dpm_trade_performance (trade_id, entry_price, original_sl, "
            "lot_size, opened_at) VALUES (?,?,?,?,?)",
            (trade_id, entry_price, original_sl, lot_size, opened_at or time.time()),
        )


# ── load_dpm_calibrated ───────────────────────────────────────────────────────

def test_load_dpm_calibrated_empty_when_no_rows(fresh_db):
    cache = dpm.DPMCache()
    cal = dpm.load_dpm_calibrated(cache)
    assert cal == {}


def test_load_dpm_calibrated_keys_by_session_and_bucket(fresh_db):
    _insert_calibration(session="London", bucket="strong", be_mult=1.5)
    cache = dpm.DPMCache()
    cal = dpm.load_dpm_calibrated(cache)
    assert "London_strong" in cal
    assert cal["London_strong"]["be_multiplier"] == 1.5


def test_load_dpm_calibrated_uses_latest_batch_only(fresh_db):
    _insert_calibration(session="London", bucket="strong", be_mult=1.0, calibrated_at=100.0)
    _insert_calibration(session="London", bucket="strong", be_mult=2.0, calibrated_at=200.0)
    cache = dpm.DPMCache()
    cal = dpm.load_dpm_calibrated(cache)
    assert cal["London_strong"]["be_multiplier"] == 2.0


def test_load_dpm_calibrated_ttl_cache_returns_stale_within_600s(fresh_db):
    _insert_calibration(session="London", bucket="strong", be_mult=1.0)
    cache = dpm.DPMCache()
    first = dpm.load_dpm_calibrated(cache)
    assert first["London_strong"]["be_multiplier"] == 1.0

    _insert_calibration(session="London", bucket="strong", be_mult=9.0, calibrated_at=time.time() + 1)
    second = dpm.load_dpm_calibrated(cache)
    assert second["London_strong"]["be_multiplier"] == 1.0


def test_load_dpm_calibrated_reloads_after_ttl_expiry(fresh_db):
    _insert_calibration(session="London", bucket="strong", be_mult=1.0)
    cache = dpm.DPMCache()
    dpm.load_dpm_calibrated(cache)

    _insert_calibration(session="London", bucket="strong", be_mult=9.0, calibrated_at=time.time() + 1)
    cache.loaded_at = time.time() - 601
    reloaded = dpm.load_dpm_calibrated(cache)
    assert reloaded["London_strong"]["be_multiplier"] == 9.0


# ── record_dpm_entry ──────────────────────────────────────────────────────────

def test_record_dpm_entry_inserts_snapshot(fresh_db):
    cache = dpm.DPMCache()
    trade = {"trade_id": "t-1", "direction": "BUY", "entry_price": 2400.0,
             "lot_size": 0.10, "stop_loss": 2390.0, "tg_source": "Manual"}
    dpm.record_dpm_entry(cache, trade, _dpm_params())

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()
        )
    assert row["direction"] == "BUY"
    assert row["entry_price"] == 2400.0
    assert row["original_sl"] == 2390.0
    assert row["be_multiplier_used"] == 1.5
    assert row["used_calibrated"] == 1


def test_record_dpm_entry_dedups_via_in_memory_guard(fresh_db):
    cache = dpm.DPMCache()
    trade = {"trade_id": "t-1", "direction": "BUY", "entry_price": 2400.0,
             "lot_size": 0.10, "stop_loss": 2390.0, "tg_source": "Manual"}
    dpm.record_dpm_entry(cache, trade, _dpm_params(be_multiplier=1.5))
    dpm.record_dpm_entry(cache, trade, _dpm_params(be_multiplier=9.9))

    with db.db() as conn:
        rows = conn.execute("SELECT * FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchall()
    assert len(rows) == 1
    assert db.row_to_dict(rows[0])["be_multiplier_used"] == 1.5


# ── update_dpm_peak ────────────────────────────────────────────────────────────

def test_update_dpm_peak_raises_high_water_mark(fresh_db):
    _insert_dpm_row("t-1")
    dpm.update_dpm_peak("t-1", 50.0)
    dpm.update_dpm_peak("t-1", 30.0)

    with db.db() as conn:
        peak = conn.execute("SELECT peak_pnl FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()[0]
    assert peak == 50.0


def test_update_dpm_peak_ignores_non_positive_pnl(fresh_db):
    _insert_dpm_row("t-1")
    dpm.update_dpm_peak("t-1", -10.0)

    with db.db() as conn:
        peak = conn.execute("SELECT peak_pnl FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()[0]
    assert peak == 0.0


# ── set_dpm_milestone ──────────────────────────────────────────────────────────

def test_set_dpm_milestone_sets_flag(fresh_db):
    _insert_dpm_row("t-1")
    dpm.set_dpm_milestone("t-1", "reached_be")

    with db.db() as conn:
        reached = conn.execute("SELECT reached_be FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()[0]
    assert reached == 1


def test_set_dpm_milestone_ignores_invalid_column(fresh_db):
    _insert_dpm_row("t-1")
    dpm.set_dpm_milestone("t-1", "not_a_real_column")

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()
        )
    assert row["reached_be"] == 0
    assert row["reached_tp1"] == 0
    assert row["reached_tp2"] == 0


# ── finalize_dpm_record ────────────────────────────────────────────────────────

def test_finalize_dpm_record_computes_r_multiple(fresh_db):
    _insert_dpm_row("t-1", entry_price=2400.0, original_sl=2390.0, lot_size=0.10)
    dpm.finalize_dpm_record("t-1", close_price=2420.0, exit_type="TP", final_pnl=200.0)

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()
        )
    assert row["close_price"] == 2420.0
    assert row["exit_type"] == "TP"
    assert row["final_pnl"] == 200.0
    assert row["r_multiple"] == 2.0
    assert row["hold_minutes"] is not None
    assert row["closed_at"] is not None


def test_finalize_dpm_record_guards_zero_initial_risk(fresh_db):
    _insert_dpm_row("t-1", entry_price=2400.0, original_sl=2400.0, lot_size=0.10)
    dpm.finalize_dpm_record("t-1", close_price=2420.0, exit_type="TP", final_pnl=200.0)

    with db.db() as conn:
        r_multiple = conn.execute("SELECT r_multiple FROM dpm_trade_performance WHERE trade_id=?", ("t-1",)).fetchone()[0]
    assert r_multiple == 0.0


def test_finalize_dpm_record_is_noop_for_unknown_trade(fresh_db):
    dpm.finalize_dpm_record("does-not-exist", close_price=2420.0, exit_type="TP", final_pnl=200.0)

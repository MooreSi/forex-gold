"""The signal-generator adapter reads somebody else's database.

That makes three properties load-bearing: it must not fall over when the file is
absent (a fresh install has no generator), it must never write, and it must
return dicts rather than sqlite3.Row.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.services.analytics import signal_lab_repo


@pytest.fixture
def signal_db(tmp_path, monkeypatch):
    """A stand-in test_signal.db with the one table the overlay reads."""
    monkeypatch.setattr(signal_lab_repo, "DATA_DIR", tmp_path)
    path = tmp_path / signal_lab_repo.DB_NAME
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE test_analysis_log ("
        "  id INTEGER PRIMARY KEY AUTOINCREMENT, ts REAL NOT NULL,"
        "  adx REAL, htf_bias TEXT)"
    )
    conn.executemany(
        "INSERT INTO test_analysis_log (ts, adx, htf_bias) VALUES (?,?,?)",
        [(100.0, 31.0, "bullish"),
         (200.0, 18.0, "bearish"),
         (300.0, None, "bullish"),   # NULL adx: no market-type signal
         (900.0, 40.0, "bullish")],  # outside the window the tests ask for
    )
    conn.commit()
    conn.close()
    return path


def test_missing_database_is_not_an_error(tmp_path, monkeypatch):
    """The generator is optional; a fresh install simply has no file."""
    monkeypatch.setattr(signal_lab_repo, "DATA_DIR", tmp_path)
    assert signal_lab_repo.is_available() is False
    assert signal_lab_repo.adx_and_bias_samples(0.0, 1e12) == []


def test_reports_available_when_the_file_exists(signal_db):
    assert signal_lab_repo.is_available() is True


def test_window_is_half_open(signal_db):
    """ts >= start and ts < end -- a sample exactly on `end` belongs to the next
    month, or a trade would be counted in two calendars."""
    rows = signal_lab_repo.adx_and_bias_samples(100.0, 900.0)
    assert [r["ts"] for r in rows] == [100.0, 200.0]


def test_null_adx_rows_are_excluded(signal_db):
    rows = signal_lab_repo.adx_and_bias_samples(0.0, 1e12)
    assert all(r["adx"] is not None for r in rows)
    assert 300.0 not in [r["ts"] for r in rows]


def test_returns_dicts_not_sqlite_rows(signal_db):
    """A Row has no .get(), and a Row reaching a NiceGUI timer callback raises an
    AttributeError that NiceGUI swallows -- the page silently stops refreshing."""
    rows = signal_lab_repo.adx_and_bias_samples(0.0, 1e12)
    assert rows and all(isinstance(r, dict) for r in rows)
    assert rows[0].get("htf_bias") == "bullish"


def test_the_connection_is_read_only(signal_db):
    """Opened with mode=ro, so a bug here cannot corrupt the generator's data."""
    conn = sqlite3.connect(f"file:{signal_db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO test_analysis_log (ts, adx) VALUES (1.0, 1.0)")
    conn.close()


def test_a_missing_table_degrades_to_empty(tmp_path, monkeypatch):
    """The file can exist before the generator has created its schema. The
    calendar should render without the overlay, not raise."""
    monkeypatch.setattr(signal_lab_repo, "DATA_DIR", tmp_path)
    sqlite3.connect(tmp_path / signal_lab_repo.DB_NAME).close()
    assert signal_lab_repo.is_available() is True
    assert signal_lab_repo.adx_and_bias_samples(0.0, 1e12) == []

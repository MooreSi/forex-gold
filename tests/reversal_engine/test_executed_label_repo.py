"""docs/todo/017 item 2: which core trade rows may label an executed signal.

A signal is labelled only from exactly one CLOSED trade row that recorded a
positive `initial_risk`. Joining by ticket double-matched on the owner's data
(744 rows for 519 signals), so the join is by signal id and a signal with two
closed rows is refused rather than guessed. Temp file; read-only open.
"""
from __future__ import annotations

import sqlite3

from backend.src.db import database as core_db
from backend.src.services.reversal_engine import executed_label_repo as repo


def _core(tmp_path, rows):
    path = tmp_path / "core.db"
    c = sqlite3.connect(path)
    c.execute("CREATE TABLE vantage_simulated_trades (signal_id TEXT, status TEXT,"
              " net_pnl REAL, initial_risk REAL)")
    c.executemany("INSERT INTO vantage_simulated_trades VALUES (?,?,?,?)", rows)
    c.commit()
    c.close()
    return path


def test_only_one_closed_row_with_recorded_risk_labels(tmp_path, monkeypatch):
    path = _core(tmp_path, [
        ("A", "closed", -150.0, 300.0),        # labels
        ("B", "closed", 10.0, 100.0),          # two closed rows: refused
        ("B", "closed", 20.0, 100.0),
        ("C", "closed", 10.0, 0.0),            # no recorded risk
        ("D", "closed", 10.0, None),
        ("E", "open", 10.0, 100.0),            # still open
    ])
    monkeypatch.setattr(core_db, "_DB_PATH", str(path))
    got = repo.closed_risk_by_signal(["A", "B", "C", "D", "E", "Z", None])
    assert got == {"A": (-150.0, 300.0)}


def test_no_ids_opens_nothing(monkeypatch):
    monkeypatch.setattr(core_db, "_DB_PATH", "/nonexistent/never-opened.db")
    assert repo.closed_risk_by_signal([None, ""]) == {}

"""Trend PA's own database, `trend_pa.db`, isolated from every other engine.

`origin` separates what the engine did live (virtually or for real) from what
the replay says it would have done. Both teach the model; the panel reports
them apart, because a backtest is evidence about the past and a live number
is evidence about now.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from backend.src.db import connection as _conn_mod

_NAMESPACE = "trend_pa"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tpa_signals (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at        REAL    NOT NULL,
    signal_ref        TEXT,
    origin            TEXT    NOT NULL DEFAULT 'live',
    direction         TEXT    NOT NULL,
    pattern           TEXT,
    session           TEXT,
    level             REAL,
    level_kind        TEXT,
    entry             REAL    NOT NULL,
    stop_loss         REAL    NOT NULL,
    take_profit       REAL    NOT NULL,
    risk              REAL    NOT NULL,
    atr_m15           REAL,
    spread            REAL,
    features_json     TEXT,
    ml_prob           REAL,
    status            TEXT    NOT NULL DEFAULT 'open',
    outcome           TEXT,
    exit_price        REAL,
    closed_at         REAL,
    r_net             REAL,
    live_exec_status  TEXT,
    mt5_ticket        INTEGER,
    vantage_signal_id TEXT
);
CREATE INDEX IF NOT EXISTS tpa_signals_origin ON tpa_signals (origin, status);

CREATE TABLE IF NOT EXISTS tpa_analysis_log (
    id     INTEGER PRIMARY KEY AUTOINCREMENT,
    ts     REAL NOT NULL,
    reason TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tpa_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

_LOG_KEEP = 2000


def get_db():
    return _conn_mod.get_db(_NAMESPACE)


def init(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _conn_mod.init_db(db_path, _NAMESPACE)
    get_db().exec(_SCHEMA)


def close_db() -> None:
    _conn_mod.close_db(_NAMESPACE)


def _row(r) -> dict:
    d = dict(r)
    d["features"] = json.loads(d.pop("features_json") or "{}")
    return d


_INSERT = """INSERT INTO tpa_signals (created_at, origin, direction, pattern,
    session, level, level_kind, entry, stop_loss, take_profit, risk, atr_m15,
    spread, features_json, ml_prob, status, outcome, exit_price, closed_at, r_net)
    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"""


def _params(s: dict, closed: bool) -> tuple:
    return (s["created_at"], s.get("origin", "live"), s["direction"], s.get("pattern"),
            s.get("session"), s.get("level"), s.get("level_kind"), s["entry"],
            s["stop_loss"], s["take_profit"], s["risk"], s.get("atr_m15"),
            s.get("spread"), json.dumps(s.get("features") or {}), s.get("ml_prob"),
            "closed" if closed else "open",
            s.get("outcome") if closed else None, s.get("exit_price") if closed else None,
            s.get("closed_at") if closed else None, s.get("r_net") if closed else None)


def insert_signal(sig: dict) -> int:
    db = get_db()
    with db.transaction():
        sid = db.run(_INSERT, *_params(sig, closed=False)).lastrowid
        db.run("UPDATE tpa_signals SET signal_ref=? WHERE id=?", f"TPA-{sid:04d}", sid)
    return int(sid)


def replace_backtest(trades: list) -> None:
    """Swap the stored replay for a new one, atomically."""
    db = get_db()
    with db.transaction():
        db.run("DELETE FROM tpa_signals WHERE origin='backtest'")
        for t in trades:
            db.run(_INSERT, *_params({**t, "origin": "backtest"}, closed=True))


def close_signal(sid: int, outcome: str, exit_price: float, closed_at: float,
                 r_net: float) -> bool:
    """Close an OPEN signal. False when it was already closed."""
    res = get_db().run(
        "UPDATE tpa_signals SET status='closed', outcome=?, exit_price=?, closed_at=?, "
        "r_net=? WHERE id=? AND status='open'",
        outcome, exit_price, closed_at, r_net, sid)
    return res.rowcount == 1


def update_live_exec(sid: int, status: str, mt5_ticket=None, vantage_signal_id=None) -> None:
    get_db().run("UPDATE tpa_signals SET live_exec_status=?, mt5_ticket=?, "
                 "vantage_signal_id=? WHERE id=?", status, mt5_ticket, vantage_signal_id, sid)


def open_signals() -> list:
    return [_row(r) for r in get_db().all(
        "SELECT * FROM tpa_signals WHERE status='open' AND origin='live' ORDER BY id")]


def closed_signals(origin: Optional[str] = None) -> list:
    if origin is None:
        rows = get_db().all("SELECT * FROM tpa_signals WHERE status='closed' ORDER BY closed_at")
    else:
        rows = get_db().all("SELECT * FROM tpa_signals WHERE status='closed' AND origin=? "
                            "ORDER BY closed_at", origin)
    return [_row(r) for r in rows]


def recent_signals(limit: int = 25) -> list:
    return [_row(r) for r in get_db().all(
        "SELECT * FROM tpa_signals WHERE origin='live' ORDER BY id DESC LIMIT ?", limit)]


def last_signal_time(direction: str, origin: str = "live") -> Optional[float]:
    r = get_db().get("SELECT MAX(created_at) AS t FROM tpa_signals WHERE direction=? "
                     "AND origin=?", direction, origin)
    return float(r["t"]) if r and r["t"] is not None else None


def log_analysis(reason: str, ts: Optional[float] = None) -> None:
    db = get_db()
    with db.transaction():
        db.run("INSERT INTO tpa_analysis_log (ts, reason) VALUES (?,?)",
               time.time() if ts is None else ts, reason)
        db.run("DELETE FROM tpa_analysis_log WHERE id <= "
               "(SELECT MAX(id) FROM tpa_analysis_log) - ?", _LOG_KEEP)


def analysis_log(limit: int = 25) -> list:
    return [dict(r) for r in get_db().all(
        "SELECT ts, reason FROM tpa_analysis_log ORDER BY ts DESC, id DESC LIMIT ?", limit)]


def get_config(key: str, default: Optional[str] = None) -> Optional[str]:
    r = get_db().get("SELECT value FROM tpa_config WHERE key=?", key)
    return r["value"] if r else default


def set_config(key: str, value: str) -> None:
    get_db().run("INSERT OR REPLACE INTO tpa_config (key, value) VALUES (?,?)", key, value)

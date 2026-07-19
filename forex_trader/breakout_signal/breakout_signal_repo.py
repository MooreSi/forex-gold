"""Isolated data store for the BREAKOUT signal module, rebuilt on the shared
DbAdapter (forex_trader.src.db) instead of raw sqlite3 -- see
docs/todo/refactor/breakout-signal-migration/020-*.md.

Structural port of breakout_signal/database.py: same tables (bo_ prefix),
same function signatures, same behavior -- proven by running
tests/breakout_signal/test_database_characterization.py against this module
unmodified, INCLUDING the known close_signal balance double-counting bug
(see that test file's docstring) -- deliberately preserved here, not fixed,
per this task's no-behavior-change scope. The one deliberate behavior
change: close_signal() and book_partial_close() wrap their multi-statement
writes in a single get_db().transaction(), instead of database.py's several
independent connections for what should be one atomic operation.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Optional

from forex_trader.src.db.connection import get_db, init_db

_STARTING_BALANCE = 1000.0

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bo_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          REAL    NOT NULL,
    signal_ref          TEXT    UNIQUE,
    direction           TEXT    NOT NULL,
    breakout_type       TEXT    NOT NULL,
    broken_level        REAL,
    broken_level_type   TEXT,
    entry_mid           REAL    NOT NULL,
    stop_loss           REAL    NOT NULL,
    tp1                 REAL,
    tp2                 REAL,
    tp3                 REAL,
    sl_dist             REAL,
    rr_tp1              REAL,
    session             TEXT,
    htf_bias            TEXT,
    h4_bias             TEXT,
    adx_at_signal       REAL,
    macd_hist           REAL,
    atr_m15             REAL,
    quality_score       REAL    DEFAULT 0.0,
    rationale           TEXT,
    status              TEXT    DEFAULT 'pending',
    outcome             TEXT    DEFAULT 'open',
    trigger_price       REAL,
    trigger_time        REAL,
    close_price         REAL,
    close_time          REAL,
    pnl_pts             REAL,
    pnl_dollars         REAL,
    balance_after       REAL,
    lot_size            REAL    DEFAULT 0.10,
    sl_moved_to_be      INTEGER DEFAULT 0,
    claude_fallback     INTEGER DEFAULT 0,
    learning_note       TEXT
);

CREATE TABLE IF NOT EXISTS bo_analysis_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL    NOT NULL,
    session             TEXT,
    htf_bias            TEXT,
    h4_bias             TEXT,
    price               REAL,
    atr_m15             REAL,
    adx                 REAL,
    key_levels_json     TEXT,
    candidate_json      TEXT,
    result              TEXT,
    claude_decision     TEXT,
    suppressed_reason   TEXT
);

CREATE TABLE IF NOT EXISTS bo_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS bo_balance_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    balance        REAL    NOT NULL,
    change_amount  REAL    NOT NULL DEFAULT 0.0,
    change_reason  TEXT,
    signal_id      INTEGER
);
"""


def init(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    get_db().exec(_SCHEMA)

    row = get_db().get("SELECT value FROM bo_config WHERE key='virtual_balance'")
    if not row:
        get_db().run(
            "INSERT OR IGNORE INTO bo_config (key, value) VALUES ('virtual_balance', ?)",
            str(_STARTING_BALANCE),
        )

    # ── Migrations: add columns if not present ─────────────────────────────
    cols = {r[1] for r in get_db().all("PRAGMA table_info(bo_signals)")}
    for col, defn in [
        ("ml_features_json",  "TEXT"),
        ("ml_prob",           "REAL"),
        ("net_pnl_pts",       "REAL"),
        ("net_pnl_dollars",   "REAL"),
        ("mt5_ticket",        "INTEGER"),
        ("vantage_signal_id", "TEXT"),
        ("live_exec_status",  "TEXT"),
        ("strategy",          "TEXT DEFAULT 'conservative'"),
        ("partial_pnl_dollars", "REAL DEFAULT 0"),
        ("remaining_frac",      "REAL DEFAULT 1.0"),
    ]:
        if col not in cols:
            get_db().run(f"ALTER TABLE bo_signals ADD COLUMN {col} {defn}")

    reconcile_balance_with_trades()


def _row(r) -> dict:
    return dict(r) if r else {}


# ── Config ────────────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    row = get_db().get("SELECT value FROM bo_config WHERE key=?", key)
    return row[0] if row else default


def set_config(key: str, value: str) -> None:
    get_db().run("INSERT OR REPLACE INTO bo_config (key, value) VALUES (?,?)", key, value)


# ── Balance ───────────────────────────────────────────────────────────────────

def get_virtual_balance() -> float:
    raw = get_config("virtual_balance", str(_STARTING_BALANCE))
    try:
        return float(raw)
    except (ValueError, TypeError):
        return _STARTING_BALANCE


def reconcile_balance_with_trades() -> Optional[float]:
    row = get_db().get(
        "SELECT SUM(COALESCE(net_pnl_dollars, pnl_dollars)) FROM bo_signals WHERE status='closed'"
    )
    total_pnl = float(row[0] or 0.0)
    expected  = round(_STARTING_BALANCE + total_pnl, 2)
    current   = get_virtual_balance()
    correction = round(expected - current, 2)
    if abs(correction) > 0.01:
        _update_balance(correction, "bo_balance_reconciliation", None)
        return correction
    return None


def _update_balance(delta: float, reason: str, signal_id: Optional[int] = None) -> float:
    bal = get_virtual_balance() + delta
    set_config("virtual_balance", str(round(bal, 2)))
    get_db().run(
        "INSERT INTO bo_balance_log (ts, balance, change_amount, change_reason, signal_id) "
        "VALUES (?,?,?,?,?)",
        time.time(), bal, delta, reason, signal_id,
    )
    return round(bal, 2)


# ── Signals ───────────────────────────────────────────────────────────────────

def create_signal(data: dict) -> int:
    result = get_db().run(
        """INSERT INTO bo_signals
           (created_at, signal_ref, direction, breakout_type,
            broken_level, broken_level_type, entry_mid, stop_loss,
            tp1, tp2, tp3, sl_dist, rr_tp1,
            session, htf_bias, h4_bias, adx_at_signal, macd_hist, atr_m15,
            quality_score, rationale, lot_size, claude_fallback, strategy)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        data.get("created_at", time.time()),
        data.get("signal_ref"),
        data["direction"],
        data["breakout_type"],
        data.get("broken_level"),
        data.get("broken_level_type"),
        data["entry_mid"],
        data["stop_loss"],
        data.get("tp1"),
        data.get("tp2"),
        data.get("tp3"),
        data.get("sl_dist"),
        data.get("rr_tp1"),
        data.get("session"),
        data.get("htf_bias"),
        data.get("h4_bias"),
        data.get("adx_at_signal"),
        data.get("macd_hist"),
        data.get("atr_m15"),
        data.get("quality_score", 0.0),
        data.get("rationale"),
        data.get("lot_size", 0.10),
        int(data.get("claude_fallback", False)),
        data.get("strategy", "conservative"),
    )
    return result.lastrowid or 0


def get_open_signals() -> list[dict]:
    rows = get_db().all(
        "SELECT * FROM bo_signals WHERE status IN ('pending','triggered') ORDER BY created_at DESC"
    )
    return [_row(r) for r in rows]


def get_all_signals(limit: int = 100) -> list[dict]:
    rows = get_db().all("SELECT * FROM bo_signals ORDER BY created_at DESC LIMIT ?", limit)
    return [_row(r) for r in rows]


def get_signal_by_id(signal_id: int) -> Optional[dict]:
    row = get_db().get("SELECT * FROM bo_signals WHERE id=?", signal_id)
    return _row(row) if row else None


def close_signal(
    signal_id: int,
    close_price: float,
    outcome: str,
    learning_note: str = "",
    net_pnl_pts: float | None = None,
    net_pnl_dollars: float | None = None,
) -> None:
    """Preserves database.py's exact behavior, INCLUDING the known
    double-counting bug when a prior partial was booked -- see the module
    docstring and tests/breakout_signal/test_database_characterization.py.
    Not fixed here; wrapped in a transaction for atomicity only."""
    with get_db().transaction():
        sig = get_signal_by_id(signal_id)
        if not sig:
            return

        entry = float(sig["entry_mid"])
        lot   = float(sig.get("lot_size") or 0.10)
        direction = sig["direction"]

        raw_pts  = (close_price - entry) if direction == "BUY" else (entry - close_price)
        pnl_pts  = round(raw_pts, 2)
        pnl_dol  = round(pnl_pts * lot * 100.0, 2)
        balance_delta = net_pnl_dollars if net_pnl_dollars is not None else pnl_dol
        if net_pnl_dollars is not None:
            pnl_dol = net_pnl_dollars
        if net_pnl_pts is not None:
            pnl_pts = net_pnl_pts
        bal = _update_balance(balance_delta, f"bo_{outcome}", signal_id)

        get_db().run(
            """UPDATE bo_signals
               SET status='closed', outcome=?, close_price=?, close_time=?,
                   pnl_pts=?, pnl_dollars=?, net_pnl_pts=?, net_pnl_dollars=?,
                   balance_after=?, learning_note=?
               WHERE id=?""",
            outcome, close_price, time.time(), pnl_pts, pnl_dol,
            net_pnl_pts, net_pnl_dollars, bal, learning_note or "", signal_id,
        )


def trigger_signal(signal_id: int, price: float) -> None:
    get_db().run(
        "UPDATE bo_signals SET status='triggered', trigger_price=?, trigger_time=? WHERE id=?",
        price, time.time(), signal_id,
    )


def move_sl_to_be(signal_id: int, be_price: float | None = None) -> None:
    sig = get_signal_by_id(signal_id)
    if not sig:
        return
    get_db().run(
        "UPDATE bo_signals SET stop_loss=?, sl_moved_to_be=1 WHERE id=?",
        be_price if be_price is not None else sig["entry_mid"], signal_id,
    )


def set_stop_loss(signal_id: int, price: float) -> None:
    """New in the repo (020) -- replaces engine.py's raw
    `with bdb._conn() as conn: conn.execute("UPDATE bo_signals SET
    stop_loss=? WHERE id=?", ...)` at the TP2 partial-close trail, which had
    no named function in database.py at all."""
    get_db().run("UPDATE bo_signals SET stop_loss=? WHERE id=?", price, signal_id)


def book_partial_close(
    signal_id: int,
    leg_net_dollars: float,
    frac_closed: float,
    note: str,
) -> float:
    with get_db().transaction():
        sig = get_signal_by_id(signal_id)
        if not sig:
            return 0.0
        remaining = float(sig.get("remaining_frac") if sig.get("remaining_frac") is not None else 1.0)
        booked    = float(sig.get("partial_pnl_dollars") or 0.0)
        new_remaining = round(max(0.0, remaining - frac_closed), 4)
        new_booked    = round(booked + leg_net_dollars, 2)
        get_db().run(
            "UPDATE bo_signals SET partial_pnl_dollars=?, remaining_frac=? WHERE id=?",
            new_booked, new_remaining, signal_id,
        )
        _update_balance(leg_net_dollars, "bo_partial_close", signal_id)
    return new_remaining


def expire_signal(signal_id: int, reason: str = "expired") -> None:
    get_db().run(
        "UPDATE bo_signals SET status='expired', outcome='expired', close_time=?, learning_note=? WHERE id=?",
        time.time(), reason, signal_id,
    )


# ── Level cooldown / consecutive-loss (new named functions, 020) ─────────────

def get_last_signal_time_for_level(direction: str, level: float, cutoff: float) -> Optional[float]:
    """New in the repo (020) -- replaces engine.py's raw
    `with bdb._conn() as _lc_conn: ...` level-cooldown query
    (_process_candidate, ~engine.py:796-816)."""
    row = get_db().get(
        "SELECT MAX(created_at) FROM bo_signals "
        "WHERE direction=? AND ROUND(broken_level,0)=ROUND(?,0) AND created_at>?",
        direction, level, cutoff,
    )
    return float(row[0]) if row and row[0] else None


def get_recent_outcomes_by_direction(direction: str, since_ts: float, limit: int) -> list[str]:
    """New in the repo (020) -- replaces engine.py's raw
    `with bdb._conn() as _cl_conn: ...` consecutive-loss query
    (_process_candidate, ~engine.py:818-845)."""
    rows = get_db().all(
        "SELECT outcome FROM bo_signals "
        "WHERE direction=? AND close_time>? AND outcome IS NOT NULL "
        "  AND outcome NOT IN ('open','pending') "
        "ORDER BY close_time DESC LIMIT ?",
        direction, since_ts, limit,
    )
    return [r[0] for r in rows]


# ── ML helpers ────────────────────────────────────────────────────────────────

def store_ml_features(signal_id: int, features: list) -> None:
    get_db().run("UPDATE bo_signals SET ml_features_json=? WHERE id=?", json.dumps(features), signal_id)


def store_ml_prob(signal_id: int, ml_prob: float) -> None:
    get_db().run("UPDATE bo_signals SET ml_prob=? WHERE id=?", round(ml_prob, 4), signal_id)


def update_live_exec_result(
    signal_id: int,
    mt5_ticket: Optional[int],
    vantage_signal_id: Optional[str],
    status: str,
) -> None:
    get_db().run(
        "UPDATE bo_signals SET mt5_ticket=?, vantage_signal_id=?, live_exec_status=? WHERE id=?",
        mt5_ticket, vantage_signal_id, status, signal_id,
    )


def update_signal_pnl_from_mt5(
    signal_id: int,
    mt5_profit: float,
    mt5_outcome: str,
) -> None:
    """Overwrite simulated P&L with the verified MT5 actual profit. Wrapped
    in one transaction -- database.py's equivalent used two independent
    connections (read, then write + balance update) for what should be one
    atomic operation."""
    with get_db().transaction():
        row = get_db().get(
            "SELECT lot_size, net_pnl_dollars, pnl_dollars, balance_after FROM bo_signals WHERE id=?",
            signal_id,
        )
        if not row:
            return
        lot       = float(row[0] or 0.10)
        net_pnl   = row[1]
        pnl_gross = row[2]
        bal_after = row[3]

        virtual_pnl = (
            float(net_pnl)   if net_pnl   is not None else
            float(pnl_gross) if pnl_gross is not None else
            None
        )

        pnl_pts = round(mt5_profit / (lot * 100.0), 2) if lot else 0.0
        get_db().run(
            """UPDATE bo_signals
               SET pnl_pts=?, pnl_dollars=?, net_pnl_pts=?, net_pnl_dollars=?, outcome=?
               WHERE id=?""",
            pnl_pts, mt5_profit, pnl_pts, mt5_profit, mt5_outcome, signal_id,
        )

        if bal_after is None:
            if abs(mt5_profit) > 0.01:
                _update_balance(mt5_profit, "bo_mt5_correction", signal_id)
        elif virtual_pnl is not None:
            correction = round(mt5_profit - virtual_pnl, 2)
            if abs(correction) > 0.01:
                _update_balance(correction, "bo_mt5_correction", signal_id)


def get_ml_features_for_signal(signal_id: int) -> Optional[str]:
    row = get_db().get("SELECT ml_features_json FROM bo_signals WHERE id=?", signal_id)
    return row[0] if row else None


def get_ml_training_data() -> list[dict]:
    rows = get_db().all(
        """SELECT id, ml_features_json, outcome, rr_tp1
           FROM bo_signals
           WHERE status='closed'
             AND outcome IN ('win','loss','be')
             AND ml_features_json IS NOT NULL
           ORDER BY close_time ASC"""
    )
    return [_row(r) for r in rows]


def get_ml_monitor_data(limit: int = 20) -> list[dict]:
    rows = get_db().all(
        """SELECT id, signal_ref, direction, breakout_type, session,
                  quality_score, ml_prob, outcome, status, created_at
           FROM bo_signals
           WHERE ml_prob IS NOT NULL
           ORDER BY created_at DESC LIMIT ?""",
        limit,
    )
    return [_row(r) for r in rows]


def get_recent_win_rate(n: int = 10) -> float:
    rows = get_db().all(
        """SELECT outcome FROM bo_signals
           WHERE status='closed' AND outcome IN ('win','loss','be')
           ORDER BY close_time DESC LIMIT ?""",
        n,
    )
    if not rows:
        return 0.5
    wins = sum(1 for r in rows if r[0] == "win")
    return round(wins / len(rows), 3)


def get_recent_closed_signals(limit: int = 20) -> list[dict]:
    rows = get_db().all(
        """SELECT * FROM bo_signals
           WHERE status='closed' AND outcome IN ('win','loss','be')
           ORDER BY close_time DESC LIMIT ?""",
        limit,
    )
    return [_row(r) for r in rows]


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    row = get_db().get("""
        SELECT
          COUNT(*) as total,
          SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
          SUM(CASE WHEN outcome='be'   THEN 1 ELSE 0 END) as be,
          SUM(CASE WHEN status IN ('pending','triggered') THEN 1 ELSE 0 END) as pending,
          AVG(CASE WHEN status='closed' AND outcome IN ('win','loss','be')
                   THEN pnl_pts END) as avg_pnl_pts,
          AVG(CASE WHEN status='closed' AND outcome IN ('win','loss','be')
                   THEN pnl_dollars END) as avg_pnl_dollars
        FROM bo_signals
    """)
    wins   = row["wins"]   or 0
    losses = row["losses"] or 0
    be     = row["be"]     or 0
    closed = wins + losses + be
    return {
        "total":          row["total"] or 0,
        "wins":           wins,
        "losses":         losses,
        "be":             be,
        "pending":        row["pending"] or 0,
        "win_rate":       round(wins / closed * 100) if closed else 0,
        "avg_pnl_pts":    round(row["avg_pnl_pts"] or 0, 1),
        "avg_pnl_dollars": round(row["avg_pnl_dollars"] or 0, 2),
    }


def get_max_drawdown() -> float:
    rows = get_db().all("SELECT balance FROM bo_balance_log ORDER BY ts")
    if not rows:
        return 0.0
    peak = _STARTING_BALANCE
    max_dd = 0.0
    for r in rows:
        bal = float(r[0])
        peak = max(peak, bal)
        dd = peak - bal
        max_dd = max(max_dd, dd)
    return round(max_dd, 2)


def get_consecutive_losses() -> int:
    rows = get_db().all(
        "SELECT outcome FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be') "
        "ORDER BY close_time DESC LIMIT 10"
    )
    count = 0
    for r in rows:
        if r[0] == "loss":
            count += 1
        else:
            break
    return count


def get_perf_by_session() -> list[dict]:
    rows = get_db().all("""
        SELECT session,
               SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(pnl_dollars) as avg_pnl,
               SUM(pnl_dollars) as total_pnl
        FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be')
        GROUP BY session ORDER BY total_pnl DESC
    """)
    return [_row(r) for r in rows]


def get_perf_by_breakout_type() -> list[dict]:
    rows = get_db().all("""
        SELECT breakout_type,
               SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(pnl_dollars) as avg_pnl,
               SUM(pnl_dollars) as total_pnl
        FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be')
        GROUP BY breakout_type
    """)
    return [_row(r) for r in rows]


def get_perf_by_adx_band() -> list[dict]:
    rows = get_db().all("""
        SELECT
          CASE
            WHEN adx_at_signal < 35  THEN '25-35 (mild)'
            WHEN adx_at_signal < 50  THEN '35-50 (trending)'
            ELSE '50+ (strong)'
          END as adx_band,
          SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
          SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
          AVG(pnl_dollars) as avg_pnl,
          SUM(pnl_dollars) as total_pnl
        FROM bo_signals
        WHERE status='closed' AND outcome IN ('win','loss','be')
          AND adx_at_signal IS NOT NULL
        GROUP BY adx_band ORDER BY adx_at_signal
    """)
    return [_row(r) for r in rows]


def get_perf_by_bias() -> list[dict]:
    rows = get_db().all("""
        SELECT htf_bias,
               SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(pnl_dollars) as avg_pnl,
               SUM(pnl_dollars) as total_pnl
        FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be')
        GROUP BY htf_bias ORDER BY total_pnl DESC
    """)
    return [_row(r) for r in rows]


# ── Analysis log ──────────────────────────────────────────────────────────────

def log_analysis(entry: dict) -> None:
    get_db().run(
        """INSERT INTO bo_analysis_log
           (ts, session, htf_bias, h4_bias, price, atr_m15, adx,
            key_levels_json, candidate_json, result, claude_decision, suppressed_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
        entry.get("ts", time.time()),
        entry.get("session"),
        entry.get("htf_bias"),
        entry.get("h4_bias"),
        entry.get("price"),
        entry.get("atr_m15"),
        entry.get("adx"),
        json.dumps(entry.get("key_levels", [])),
        json.dumps(entry.get("candidate")) if entry.get("candidate") else None,
        entry.get("result", ""),
        entry.get("claude_decision", ""),
        entry.get("suppressed_reason", ""),
    )


def get_analysis_log(limit: int = 40) -> list[dict]:
    rows = get_db().all("SELECT * FROM bo_analysis_log ORDER BY ts DESC LIMIT ?", limit)
    result = []
    for r in rows:
        d = _row(r)
        for field in ("key_levels_json", "candidate_json"):
            if d.get(field):
                try:
                    d[field] = json.loads(d[field])
                except Exception:
                    pass
        result.append(d)
    return result

"""Isolated data store for the TEST signal (Bounce) module, rebuilt on the
shared DbAdapter (forex_trader.src.db) instead of raw sqlite3 -- see
docs/todo/refactor/test-signal-migration/020-*.md.

Structural port of test_signal/database.py: same tables, same function
signatures, same behavior -- proven by running
tests/test_signal/test_database_characterization.py against this module
unmodified. The deliberate behavior change: close_signal_with_balance_update()
consolidates what database.py does as 4 fully independent connections (read
balance, write balance, log balance entry, update signal row) into one
transaction() -- database.py's version has no atomicity at all today.
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

CREATE TABLE IF NOT EXISTS test_signals (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       REAL    NOT NULL,
    direction        TEXT    NOT NULL,
    entry_low        REAL    NOT NULL,
    entry_high       REAL    NOT NULL,
    entry_mid        REAL    NOT NULL,
    stop_loss        REAL    NOT NULL,
    tp1              REAL,
    tp2              REAL,
    tp3              REAL,
    sl_dist          REAL,
    rr_tp1           REAL,
    rr_tp3           REAL,
    session          TEXT,
    htf_bias         TEXT,
    atr_m15          REAL,
    key_level        REAL,
    key_level_type   TEXT,
    rationale        TEXT,
    quality_score    REAL    DEFAULT 0.0,
    claude_approved  INTEGER DEFAULT 0,
    status           TEXT    DEFAULT 'pending',
    trigger_price    REAL,
    trigger_time     REAL,
    outcome          TEXT    DEFAULT 'open',
    close_price      REAL,
    close_time       REAL,
    pnl_pts          REAL,
    lot_size         REAL    DEFAULT 0.01,
    risk_amount      REAL    DEFAULT 0.0,
    pnl_dollars      REAL,
    balance_after    REAL,
    learning_note    TEXT,
    sl_moved_to_be   INTEGER DEFAULT 0,
    signal_ref       TEXT,
    claude_fallback  INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS test_analysis_log (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                  REAL    NOT NULL,
    session             TEXT,
    htf_bias            TEXT,
    price               REAL,
    atr_m15             REAL,
    adx                 REAL,
    key_levels_json     TEXT,
    candidate_json      TEXT,
    result              TEXT,
    claude_decision     TEXT,
    suppressed_reason   TEXT
);

CREATE TABLE IF NOT EXISTS test_auth (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    password_hash TEXT    NOT NULL,
    created_at    REAL    NOT NULL,
    last_access   REAL
);

CREATE TABLE IF NOT EXISTS test_config (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS test_balance_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    ts             REAL    NOT NULL,
    balance        REAL    NOT NULL,
    change_amount  REAL    NOT NULL DEFAULT 0.0,
    change_reason  TEXT,
    signal_id      INTEGER
);

CREATE TABLE IF NOT EXISTS ml_features (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    signal_id    INTEGER NOT NULL,
    ts           REAL    NOT NULL,
    features_json TEXT   NOT NULL
);
"""

_V03_COLUMNS = {
    "lot_size":       "REAL DEFAULT 0.01",
    "risk_amount":    "REAL DEFAULT 0.0",
    "pnl_dollars":    "REAL",
    "balance_after":  "REAL",
    "learning_note":  "TEXT",
    "sl_moved_to_be": "INTEGER DEFAULT 0",
}
_V04_COLUMNS = {"signal_ref": "TEXT", "claude_fallback": "INTEGER DEFAULT 0"}
_V05_COLUMNS = {"trigger_pattern": "TEXT"}
_V06_COLUMNS = {"strategy": "TEXT DEFAULT 'be_runner'"}
_V07_COLUMNS = {"ml_prob": "REAL"}
_V08_COLUMNS = {"mt5_ticket": "INTEGER", "live_exec_status": "TEXT", "vantage_signal_id": "TEXT"}
_V09_COLUMNS = {"regime": "TEXT", "adx": "REAL"}


def init(db_path: str) -> None:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    init_db(db_path)
    get_db().exec(_SCHEMA)
    _migrate_v03()
    row = get_db().get("SELECT value FROM test_config WHERE key='virtual_balance'")
    if not row:
        get_db().run(
            "INSERT OR IGNORE INTO test_config (key, value) VALUES ('virtual_balance', ?)",
            str(_STARTING_BALANCE),
        )


def _migrate_v03() -> None:
    existing = {r[1] for r in get_db().all("PRAGMA table_info(test_signals)")}
    for col, defn in {
        **_V03_COLUMNS, **_V04_COLUMNS, **_V05_COLUMNS,
        **_V06_COLUMNS, **_V07_COLUMNS, **_V08_COLUMNS, **_V09_COLUMNS,
    }.items():
        if col not in existing:
            get_db().run(f"ALTER TABLE test_signals ADD COLUMN {col} {defn}")


def _row(r) -> dict:
    return dict(r) if r else {}


# ── Auth ──────────────────────────────────────────────────────────────────────

def get_password_hash() -> Optional[str]:
    row = get_db().get("SELECT password_hash FROM test_auth WHERE id=1")
    return row["password_hash"] if row else None


def set_password_hash(h: str) -> None:
    get_db().run(
        "INSERT OR REPLACE INTO test_auth (id, password_hash, created_at) VALUES (1,?,?)",
        h, time.time(),
    )


def touch_last_access() -> None:
    get_db().run("UPDATE test_auth SET last_access=? WHERE id=1", time.time())


# ── Config ────────────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    row = get_db().get("SELECT value FROM test_config WHERE key=?", key)
    return row["value"] if row else default


def set_config(key: str, value: str) -> None:
    get_db().run("INSERT OR REPLACE INTO test_config (key, value) VALUES (?,?)", key, value)


# ── Virtual balance ───────────────────────────────────────────────────────────

def get_virtual_balance() -> float:
    val = get_config("virtual_balance", str(_STARTING_BALANCE))
    try:
        return float(val)
    except (ValueError, TypeError):
        return _STARTING_BALANCE


def set_virtual_balance(amount: float) -> None:
    set_config("virtual_balance", str(round(amount, 2)))


def log_balance(balance: float, change_amount: float, change_reason: str,
                signal_id: Optional[int] = None) -> None:
    get_db().run(
        "INSERT INTO test_balance_log (ts, balance, change_amount, change_reason, signal_id) "
        "VALUES (?,?,?,?,?)",
        time.time(), balance, change_amount, change_reason, signal_id,
    )


def get_balance_log(limit: int = 100) -> list[dict]:
    rows = get_db().all("SELECT * FROM test_balance_log ORDER BY ts DESC LIMIT ?", limit)
    return [_row(r) for r in rows]


def get_max_drawdown() -> float:
    rows = get_db().all("SELECT balance FROM test_balance_log ORDER BY ts")
    if not rows:
        return 0.0
    peak = _STARTING_BALANCE
    max_dd = 0.0
    for r in rows:
        b = float(r[0])
        if b > peak:
            peak = b
        dd = peak - b
        if dd > max_dd:
            max_dd = dd
    return round(max_dd, 2)


def get_consecutive_losses() -> int:
    rows = get_db().all(
        "SELECT outcome FROM test_signals WHERE status='closed' "
        "ORDER BY close_time DESC LIMIT 10"
    )
    count = 0
    for r in rows:
        if r[0] == "loss":
            count += 1
        else:
            break
    return count


# ── Signals ───────────────────────────────────────────────────────────────────

def get_signal_by_id(signal_id: int) -> dict:
    """Returns {} for a missing row -- matches database.py's behavior
    (via its own _row() helper), NOT None like gd_copy_signal/breakout_signal."""
    row = get_db().get("SELECT * FROM test_signals WHERE id=?", signal_id)
    return _row(row)


def insert_signal(sig: dict) -> tuple[int, str]:
    result = get_db().run(
        """INSERT INTO test_signals
           (created_at, direction, entry_low, entry_high, entry_mid, stop_loss,
            tp1, tp2, tp3, sl_dist, rr_tp1, rr_tp3,
            session, htf_bias, atr_m15, key_level, key_level_type,
            rationale, quality_score, claude_approved,
            lot_size, risk_amount, claude_fallback, trigger_pattern, strategy,
            ml_prob, regime, adx)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        sig.get("created_at", time.time()),
        sig["direction"], sig["entry_low"], sig["entry_high"], sig["entry_mid"],
        sig["stop_loss"], sig.get("tp1"), sig.get("tp2"), sig.get("tp3"),
        sig.get("sl_dist"), sig.get("rr_tp1"), sig.get("rr_tp3"),
        sig.get("session"), sig.get("htf_bias"), sig.get("atr_m15"),
        sig.get("key_level"), sig.get("key_level_type"),
        sig.get("rationale", ""), sig.get("quality_score", 0.0),
        1 if sig.get("claude_approved") else 0,
        sig.get("lot_size", 0.01), sig.get("risk_amount", 0.0),
        1 if sig.get("claude_fallback") else 0,
        sig.get("trigger_pattern", ""),
        sig.get("strategy", "be_runner"),
        sig.get("ml_prob"),
        sig.get("regime"),
        sig.get("adx"),
    )
    signal_id = result.lastrowid
    signal_ref = f"SIG-{signal_id:04d}"
    get_db().run("UPDATE test_signals SET signal_ref=? WHERE id=?", signal_ref, signal_id)
    return signal_id, signal_ref


def get_open_signals() -> list[dict]:
    rows = get_db().all(
        "SELECT * FROM test_signals WHERE status IN ('pending','triggered') ORDER BY created_at DESC"
    )
    return [_row(r) for r in rows]


def get_all_signals(limit: int = 100) -> list[dict]:
    rows = get_db().all("SELECT * FROM test_signals ORDER BY created_at DESC LIMIT ?", limit)
    return [_row(r) for r in rows]


def get_recent_closed_signals(limit: int = 10) -> list[dict]:
    rows = get_db().all(
        "SELECT * FROM test_signals WHERE status='closed' ORDER BY close_time DESC LIMIT ?",
        limit,
    )
    return [_row(r) for r in rows]


def update_signal_status(signal_id: int, status: str, outcome: str = "",
                          close_price: float = 0.0, pnl_pts: float = 0.0,
                          pnl_dollars: float = 0.0, balance_after: float = 0.0,
                          learning_note: str = "") -> None:
    get_db().run(
        """UPDATE test_signals SET status=?, outcome=?, close_price=?,
           close_time=?, pnl_pts=?, pnl_dollars=?, balance_after=?,
           learning_note=? WHERE id=?""",
        status, outcome, close_price, time.time(), pnl_pts,
        pnl_dollars, balance_after, learning_note, signal_id,
    )


def close_signal_with_balance_update(
    signal_id: int,
    outcome: str,
    close_price: float,
    pnl_pts: float,
    pnl_dollars: float,
    signal_ref: str,
    direction: str,
    learning_note: str = "",
) -> float:
    """New in the repo (020) -- consolidates what the caller (engine.py's
    _close_signal) previously did as 4 fully independent connections (read
    balance / write balance / log balance entry / update signal row) into
    ONE transaction. Returns the new balance.

    Note: `learning_note` must be computed by the CALLER before invoking
    this function (it involves an async AI call) -- holding a DB
    transaction open across network I/O would block every other DB
    operation in the app for the duration of that call. This changes the
    ORDER of operations relative to engine.py (note generated first, then
    the atomic update happens), not what ends up stored."""
    with get_db().transaction():
        balance = get_virtual_balance()
        new_balance = round(balance + pnl_dollars, 2)
        set_virtual_balance(new_balance)
        log_balance(new_balance, pnl_dollars, f"{outcome} {signal_ref} {direction}", signal_id)
        update_signal_status(
            signal_id, "closed", outcome, close_price, pnl_pts,
            pnl_dollars=pnl_dollars, balance_after=new_balance,
            learning_note=learning_note,
        )
    return new_balance


def update_signal_triggered(signal_id: int, trigger_price: float) -> None:
    get_db().run(
        "UPDATE test_signals SET status='triggered', trigger_price=?, trigger_time=? WHERE id=?",
        trigger_price, time.time(), signal_id,
    )


def set_signal_sl_moved(signal_id: int, new_sl: float) -> None:
    get_db().run(
        "UPDATE test_signals SET stop_loss=?, outcome='tp1_hit', sl_moved_to_be=1 WHERE id=?",
        new_sl, signal_id,
    )


def update_conservative_levels(signal_id: int, sl: float, tp1: float) -> None:
    get_db().run("UPDATE test_signals SET stop_loss=?, tp1=? WHERE id=?", sl, tp1, signal_id)


def expire_old_pending_signals(max_age_hours: float = 24.0) -> int:
    cutoff = time.time() - max_age_hours * 3600
    result = get_db().run(
        "UPDATE test_signals SET status='expired', outcome='expired' "
        "WHERE status='pending' AND created_at < ?",
        cutoff,
    )
    return result.rowcount


# ── Consecutive-loss check (new named function, 020) ─────────────────────────

def get_recent_outcomes_by_direction(direction: str, since_ts: float, limit: int) -> list[str]:
    """New in the repo (020) -- replaces engine.py's raw
    `with tdb._conn() as _cl_con: ...` consecutive-loss query in _run_cycle."""
    rows = get_db().all(
        "SELECT outcome FROM test_signals "
        "WHERE direction=? AND close_time>? AND outcome IS NOT NULL "
        "  AND outcome NOT IN ('open','pending') "
        "ORDER BY close_time DESC LIMIT ?",
        direction, since_ts, limit,
    )
    return [r[0] for r in rows]


def get_closed_signals_with_mt5_ticket() -> list[dict]:
    """New in the repo (020) -- replaces engine.py's raw
    `with tdb._conn() as conn: ...` in _reconcile_live_pnl."""
    rows = get_db().all(
        "SELECT id, mt5_ticket, pnl_dollars, outcome FROM test_signals"
        " WHERE status='closed' AND mt5_ticket IS NOT NULL"
    )
    return [_row(r) for r in rows]


# ── Analysis log ──────────────────────────────────────────────────────────────

def log_analysis(entry: dict) -> None:
    try:
        get_db().run("ALTER TABLE test_analysis_log ADD COLUMN adx REAL")
    except Exception:
        pass
    get_db().run(
        """INSERT INTO test_analysis_log
           (ts, session, htf_bias, price, atr_m15, adx, key_levels_json,
            candidate_json, result, claude_decision, suppressed_reason)
           VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
        entry.get("ts", time.time()),
        entry.get("session"), entry.get("htf_bias"), entry.get("price"),
        entry.get("atr_m15"), entry.get("adx"),
        json.dumps(entry.get("key_levels", [])),
        json.dumps(entry.get("candidate")) if entry.get("candidate") else None,
        entry.get("result", ""),
        entry.get("claude_decision", ""),
        entry.get("suppressed_reason", ""),
    )


def get_analysis_log(limit: int = 50) -> list[dict]:
    rows = get_db().all("SELECT * FROM test_analysis_log ORDER BY ts DESC LIMIT ?", limit)
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


# ── ML feature store ─────────────────────────────────────────────────────────

def store_ml_features(signal_id: int, features: dict) -> None:
    get_db().run(
        "INSERT INTO ml_features (signal_id, ts, features_json) VALUES (?,?,?)",
        signal_id, time.time(), json.dumps(features),
    )


def patch_ml_features(signal_id: int, updates: dict) -> None:
    with get_db().transaction():
        existing = get_ml_features_for_signal(signal_id)
        if existing is None:
            return
        existing.update(updates)
        get_db().run(
            "UPDATE ml_features SET features_json=? WHERE signal_id=?",
            json.dumps(existing), signal_id,
        )


def get_ml_training_data() -> list[dict]:
    rows = get_db().all(
        """SELECT f.signal_id, f.features_json, s.outcome, f.ts, s.rr_tp1
           FROM ml_features f
           JOIN test_signals s ON s.id = f.signal_id
           WHERE s.status = 'closed' AND s.outcome IN ('win', 'loss', 'be')
           ORDER BY s.close_time ASC"""
    )
    result = []
    for r in rows:
        try:
            feat = json.loads(r[1])
        except Exception:
            continue
        result.append({
            "signal_id": r[0], "features": feat, "outcome": r[2],
            "ts": float(r[3]), "rr_tp1": float(r[4]) if r[4] else 1.0,
        })
    return result


def update_live_exec_result(
    signal_id: int,
    mt5_ticket: Optional[int],
    vantage_signal_id: Optional[str],
    status: str,
) -> None:
    get_db().run(
        "UPDATE test_signals SET mt5_ticket=?, vantage_signal_id=?, live_exec_status=? WHERE id=?",
        mt5_ticket, vantage_signal_id, status, signal_id,
    )


def update_signal_pnl_from_mt5(
    signal_id: int,
    mt5_profit: float,
    mt5_outcome: str,
) -> None:
    """Overwrite simulated P&L with the verified MT5 actual profit. Wrapped
    in one transaction -- database.py's equivalent used two independent
    connections (read, then write + balance correction)."""
    with get_db().transaction():
        row = get_db().get("SELECT lot_size, pnl_dollars FROM test_signals WHERE id=?", signal_id)
        if not row:
            return
        lot         = float(row[0] or 0.10)
        virtual_pnl = float(row[1]) if row[1] is not None else None

        pnl_pts = round(mt5_profit / (lot * 100.0), 2) if lot else 0.0
        get_db().run(
            "UPDATE test_signals SET pnl_pts=?, pnl_dollars=?, outcome=? WHERE id=?",
            pnl_pts, mt5_profit, mt5_outcome, signal_id,
        )

        if virtual_pnl is not None:
            correction = round(mt5_profit - virtual_pnl, 2)
            if abs(correction) > 0.01:
                new_bal = round(get_virtual_balance() + correction, 2)
                set_virtual_balance(new_bal)
                log_balance(new_bal, correction, "mt5_correction", signal_id)


def get_ml_features_for_signal(signal_id: int) -> Optional[dict]:
    row = get_db().get("SELECT features_json FROM ml_features WHERE signal_id=? LIMIT 1", signal_id)
    if not row:
        return None
    try:
        return json.loads(row[0])
    except Exception:
        return None


def get_ml_monitor_data() -> list[dict]:
    rows = get_db().all(
        """SELECT s.id AS signal_id, s.ml_prob, s.outcome, s.htf_bias, s.session,
                  s.pnl_dollars, s.created_at, f.features_json
           FROM test_signals s
           LEFT JOIN ml_features f ON f.signal_id = s.id
           WHERE s.status='closed' AND s.ml_prob IS NOT NULL AND s.outcome IS NOT NULL
           ORDER BY s.id"""
    )
    result = []
    for r in rows:
        d = _row(r)
        feat: dict = {}
        if d.get("features_json"):
            try:
                feat = json.loads(d["features_json"])
            except Exception:
                pass
        d["adx_norm"] = float(feat.get("adx_norm", 0.5))
        d["regime"] = "trending" if d["adx_norm"] >= 0.5 else "ranging"
        d.pop("features_json", None)
        result.append(d)
    return result


def get_perf_by_regime() -> list[dict]:
    rows = get_db().all(
        """SELECT s.outcome, s.pnl_dollars, f.features_json
           FROM test_signals s
           LEFT JOIN ml_features f ON f.signal_id = s.id
           WHERE s.status='closed' AND s.outcome IS NOT NULL"""
    )
    result: dict = {}
    for r in rows:
        d = _row(r)
        feat: dict = {}
        if d.get("features_json"):
            try:
                feat = json.loads(d["features_json"])
            except Exception:
                pass
        adx_norm = float(feat.get("adx_norm", 0.5))
        regime = "trending" if adx_norm >= 0.5 else "ranging"
        if regime not in result:
            result[regime] = {"regime": regime, "wins": 0, "losses": 0, "be": 0,
                              "total": 0, "total_pnl": 0.0}
        outcome = d.get("outcome", "")
        result[regime]["total"] += 1
        result[regime]["total_pnl"] += float(d.get("pnl_dollars") or 0)
        if outcome == "win":
            result[regime]["wins"] += 1
        elif outcome == "loss":
            result[regime]["losses"] += 1
        elif outcome == "be":
            result[regime]["be"] += 1
    for v in result.values():
        closed = v["wins"] + v["losses"]
        v["win_rate"] = round(v["wins"] / closed * 100, 1) if closed else 0.0
        v["total_pnl"] = round(v["total_pnl"], 2)
    return list(result.values())


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    total   = get_db().get("SELECT COUNT(*) FROM test_signals")[0]
    wins    = get_db().get("SELECT COUNT(*) FROM test_signals WHERE outcome='win'")[0]
    losses  = get_db().get("SELECT COUNT(*) FROM test_signals WHERE outcome='loss'")[0]
    be      = get_db().get("SELECT COUNT(*) FROM test_signals WHERE outcome='be'")[0]
    pending = get_db().get(
        "SELECT COUNT(*) FROM test_signals WHERE status IN ('pending','triggered')"
    )[0]
    avg_pts_row = get_db().get(
        "SELECT AVG(pnl_pts) FROM test_signals WHERE pnl_pts IS NOT NULL AND status='closed'"
    )
    total_dollars_row = get_db().get(
        "SELECT SUM(pnl_dollars) FROM test_signals WHERE pnl_dollars IS NOT NULL AND status='closed'"
    )
    avg_dollars_row = get_db().get(
        "SELECT AVG(pnl_dollars) FROM test_signals WHERE pnl_dollars IS NOT NULL AND status='closed'"
    )
    closed = wins + losses + be
    win_rate = round(wins / closed * 100, 1) if closed else 0.0
    return {
        "total": total, "wins": wins, "losses": losses, "be": be,
        "pending": pending, "closed": closed, "win_rate": win_rate,
        "avg_pnl_pts":     round(float(avg_pts_row[0] or 0), 2),
        "total_pnl_dollars": round(float(total_dollars_row[0] or 0), 2),
        "avg_pnl_dollars":   round(float(avg_dollars_row[0] or 0), 2),
    }


def get_perf_by_session() -> list[dict]:
    rows = get_db().all(
        """SELECT session,
                  COUNT(*) as total,
                  SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                  ROUND(AVG(pnl_dollars), 2) as avg_pnl,
                  ROUND(SUM(pnl_dollars), 2) as total_pnl
           FROM test_signals WHERE status='closed' AND session IS NOT NULL
           GROUP BY session ORDER BY total_pnl DESC"""
    )
    return [_row(r) for r in rows]


def get_perf_by_level_type() -> list[dict]:
    rows = get_db().all(
        """SELECT key_level_type,
                  COUNT(*) as total,
                  SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                  ROUND(AVG(pnl_dollars), 2) as avg_pnl,
                  ROUND(SUM(pnl_dollars), 2) as total_pnl
           FROM test_signals WHERE status='closed' AND key_level_type IS NOT NULL
           GROUP BY key_level_type ORDER BY total_pnl DESC"""
    )
    return [_row(r) for r in rows]


def get_perf_by_bias() -> list[dict]:
    rows = get_db().all(
        """SELECT htf_bias,
                  COUNT(*) as total,
                  SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                  SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                  ROUND(AVG(pnl_dollars), 2) as avg_pnl,
                  ROUND(SUM(pnl_dollars), 2) as total_pnl
           FROM test_signals WHERE status='closed' AND htf_bias IS NOT NULL
           GROUP BY htf_bias ORDER BY total_pnl DESC"""
    )
    return [_row(r) for r in rows]

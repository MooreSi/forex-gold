"""
Isolated SQLite database for the BREAKOUT signal module.
Completely separate from both the main vantage.db and test_signal.db.
No shared tables, no shared connection, no cross-contamination of ML labels.
"""

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

_DB_PATH: str = ""

_STARTING_BALANCE = 1000.0

_SCHEMA = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS bo_signals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at          REAL    NOT NULL,
    signal_ref          TEXT    UNIQUE,
    direction           TEXT    NOT NULL,
    breakout_type       TEXT    NOT NULL,   -- 'go' or 'retest'
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
    global _DB_PATH
    _DB_PATH = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    with _conn() as conn:
        conn.executescript(_SCHEMA)
        row = conn.execute(
            "SELECT value FROM bo_config WHERE key='virtual_balance'"
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT OR IGNORE INTO bo_config (key, value) VALUES ('virtual_balance', ?)",
                (str(_STARTING_BALANCE),),
            )
        # ── Migrations: add columns if not present ─────────────────────────────
        cols = {r[1] for r in conn.execute("PRAGMA table_info(bo_signals)").fetchall()}
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
                conn.execute(f"ALTER TABLE bo_signals ADD COLUMN {col} {defn}")

    # Reconcile stored balance against sum of pnl_dollars on every startup.
    # This corrects any historical drift caused by MT5 correction entries that
    # were never written (pre-fix schema migration, or signal 22-style gaps).
    reconcile_balance_with_trades()


@contextmanager
def _conn():
    conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _row(r) -> dict:
    return dict(r) if r else {}


# ── Config ────────────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    with _conn() as conn:
        row = conn.execute(
            "SELECT value FROM bo_config WHERE key=?", (key,)
        ).fetchone()
    return row[0] if row else default


def set_config(key: str, value: str) -> None:
    with _conn() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO bo_config (key, value) VALUES (?,?)",
            (key, value),
        )


# ── Balance ───────────────────────────────────────────────────────────────────

def get_virtual_balance() -> float:
    raw = get_config("virtual_balance", str(_STARTING_BALANCE))
    try:
        return float(raw)
    except (ValueError, TypeError):
        return _STARTING_BALANCE


def reconcile_balance_with_trades() -> Optional[float]:
    """Recompute virtual_balance from sum of pnl_dollars across all closed signals.

    Detects and corrects the gap that arises when MT5 reconciliation updates
    pnl_dollars but fails to write the matching balance correction (e.g. because
    net_pnl_dollars was NULL at reconciliation time, or close_signal was never called).

    Returns the correction amount applied, or None if balance was already correct.
    """
    with _conn() as conn:
        row = conn.execute(
            # Reconcile against the cost-adjusted NET figure (falling back to gross
            # pnl_dollars only for legacy rows where net_pnl_dollars is NULL) —
            # summing the gross column here silently overwrote the cost-adjusted
            # balance back to pre-cost figures on every engine restart.
            "SELECT SUM(COALESCE(net_pnl_dollars, pnl_dollars)) FROM bo_signals WHERE status='closed'"
        ).fetchone()
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
    with _conn() as conn:
        conn.execute(
            "INSERT INTO bo_balance_log (ts, balance, change_amount, change_reason, signal_id) "
            "VALUES (?,?,?,?,?)",
            (time.time(), bal, delta, reason, signal_id),
        )
    return round(bal, 2)


# ── Signals ───────────────────────────────────────────────────────────────────

def create_signal(data: dict) -> int:
    with _conn() as conn:
        cur = conn.execute(
            """INSERT INTO bo_signals
               (created_at, signal_ref, direction, breakout_type,
                broken_level, broken_level_type, entry_mid, stop_loss,
                tp1, tp2, tp3, sl_dist, rr_tp1,
                session, htf_bias, h4_bias, adx_at_signal, macd_hist, atr_m15,
                quality_score, rationale, lot_size, claude_fallback, strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
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
            ),
        )
        return cur.lastrowid


def get_open_signals() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bo_signals WHERE status IN ('pending','triggered') ORDER BY created_at DESC"
        ).fetchall()
    return [_row(r) for r in rows]


def get_all_signals(limit: int = 100) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bo_signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [_row(r) for r in rows]


def get_signal_by_id(signal_id: int) -> Optional[dict]:
    with _conn() as conn:
        row = conn.execute("SELECT * FROM bo_signals WHERE id=?", (signal_id,)).fetchone()
    return _row(row) if row else None


def close_signal(
    signal_id: int,
    close_price: float,
    outcome: str,
    learning_note: str = "",
    net_pnl_pts: float | None = None,
    net_pnl_dollars: float | None = None,
) -> None:
    sig = get_signal_by_id(signal_id)
    if not sig:
        return

    entry = float(sig["entry_mid"])
    lot   = float(sig.get("lot_size") or 0.10)
    direction = sig["direction"]

    raw_pts  = (close_price - entry) if direction == "BUY" else (entry - close_price)
    pnl_pts  = round(raw_pts, 2)
    pnl_dol  = round(pnl_pts * lot * 100.0, 2)
    # Use cost-adjusted (and, for partial-close trades, partial-profit-inclusive)
    # P&L for both the balance update AND the reported gross figure — keeping a
    # separate uncorrected "gross" column was what caused the reconciliation bug
    # above (drift silently reverted cost adjustments on restart).
    balance_delta = net_pnl_dollars if net_pnl_dollars is not None else pnl_dol
    if net_pnl_dollars is not None:
        pnl_dol = net_pnl_dollars
    if net_pnl_pts is not None:
        pnl_pts = net_pnl_pts
    bal = _update_balance(balance_delta, f"bo_{outcome}", signal_id)

    with _conn() as conn:
        conn.execute(
            """UPDATE bo_signals
               SET status='closed', outcome=?, close_price=?, close_time=?,
                   pnl_pts=?, pnl_dollars=?, net_pnl_pts=?, net_pnl_dollars=?,
                   balance_after=?, learning_note=?
               WHERE id=?""",
            (outcome, close_price, time.time(), pnl_pts, pnl_dol,
             net_pnl_pts, net_pnl_dollars, bal, learning_note or "", signal_id),
        )


def trigger_signal(signal_id: int, price: float) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET status='triggered', trigger_price=?, trigger_time=? WHERE id=?",
            (price, time.time(), signal_id),
        )


def move_sl_to_be(signal_id: int, be_price: float | None = None) -> None:
    """Move the stop to break-even. `be_price` should be entry plus the
    round-trip cost (spread+commission+slippage) so a BE exit on the remaining
    fraction nets $0 instead of quietly losing the cost on every 'be' close;
    falls back to raw entry_mid when not supplied."""
    sig = get_signal_by_id(signal_id)
    if not sig:
        return
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET stop_loss=?, sl_moved_to_be=1 WHERE id=?",
            (be_price if be_price is not None else sig["entry_mid"], signal_id),
        )


def book_partial_close(
    signal_id: int,
    leg_net_dollars: float,
    frac_closed: float,
    note: str,
) -> float:
    """
    Bank realized profit for a fraction of an open position (TP1/TP2 partials)
    without closing the signal. Updates the running balance immediately so the
    gain is locked rather than fully exposed to a trail-stop giveback if price
    reverses before the next target.

    Returns the new remaining_frac.
    """
    sig = get_signal_by_id(signal_id)
    if not sig:
        return 0.0
    remaining = float(sig.get("remaining_frac") if sig.get("remaining_frac") is not None else 1.0)
    booked    = float(sig.get("partial_pnl_dollars") or 0.0)
    new_remaining = round(max(0.0, remaining - frac_closed), 4)
    new_booked    = round(booked + leg_net_dollars, 2)
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET partial_pnl_dollars=?, remaining_frac=? WHERE id=?",
            (new_booked, new_remaining, signal_id),
        )
    _update_balance(leg_net_dollars, "bo_partial_close", signal_id)
    return new_remaining


def expire_signal(signal_id: int, reason: str = "expired") -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET status='expired', outcome='expired', close_time=?, learning_note=? WHERE id=?",
            (time.time(), reason, signal_id),
        )


# ── ML helpers ────────────────────────────────────────────────────────────────

def store_ml_features(signal_id: int, features: list) -> None:
    import json as _json
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET ml_features_json=? WHERE id=?",
            (_json.dumps(features), signal_id),
        )


def store_ml_prob(signal_id: int, ml_prob: float) -> None:
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET ml_prob=? WHERE id=?",
            (round(ml_prob, 4), signal_id),
        )


def update_live_exec_result(
    signal_id: int,
    mt5_ticket: Optional[int],
    vantage_signal_id: Optional[str],
    status: str,
) -> None:
    """Record the outcome of a live MT5 execution attempt on a bo_signal row."""
    with _conn() as conn:
        conn.execute(
            "UPDATE bo_signals SET mt5_ticket=?, vantage_signal_id=?, live_exec_status=? WHERE id=?",
            (mt5_ticket, vantage_signal_id, status, signal_id),
        )


def update_signal_pnl_from_mt5(
    signal_id: int,
    mt5_profit: float,
    mt5_outcome: str,
) -> None:
    """Overwrite simulated P&L with the verified MT5 actual profit for a live signal.

    Also corrects the virtual balance log so that drawdown, total P&L, and the
    running balance all reflect the real trade result rather than the virtual
    simulation figure that was written at close time.
    """
    with _conn() as conn:
        row = conn.execute(
            "SELECT lot_size, net_pnl_dollars, pnl_dollars, balance_after FROM bo_signals WHERE id=?",
            (signal_id,),
        ).fetchone()
    if not row:
        return
    lot       = float(row[0] or 0.10)
    net_pnl   = row[1]   # what was applied to balance; may be NULL (pre-migration rows)
    pnl_gross = row[2]   # gross P&L set by close_signal
    bal_after = row[3]   # NULL means close_signal was never called for this signal

    # Use net_pnl_dollars as the reference for what was applied to the balance.
    # Fall back to pnl_dollars (gross) when net is NULL — covers rows created
    # before the net_pnl_dollars column existed (schema migration adds NULL).
    virtual_pnl = (
        float(net_pnl)   if net_pnl   is not None else
        float(pnl_gross) if pnl_gross is not None else
        None
    )

    pnl_pts = round(mt5_profit / (lot * 100.0), 2) if lot else 0.0
    with _conn() as conn:
        conn.execute(
            """UPDATE bo_signals
               SET pnl_pts=?, pnl_dollars=?, net_pnl_pts=?, net_pnl_dollars=?, outcome=?
               WHERE id=?""",
            (pnl_pts, mt5_profit, pnl_pts, mt5_profit, mt5_outcome, signal_id),
        )

    # Correct the virtual balance so the running total matches the real MT5 result.
    if bal_after is None:
        # close_signal was never called (engine restarted before detecting the virtual
        # close): apply the full MT5 profit as a fresh balance entry.
        if abs(mt5_profit) > 0.01:
            _update_balance(mt5_profit, "bo_mt5_correction", signal_id)
    elif virtual_pnl is not None:
        correction = round(mt5_profit - virtual_pnl, 2)
        if abs(correction) > 0.01:
            _update_balance(correction, "bo_mt5_correction", signal_id)


def get_ml_features_for_signal(signal_id: int) -> Optional[str]:
    with _conn() as conn:
        row = conn.execute(
            "SELECT ml_features_json FROM bo_signals WHERE id=?", (signal_id,)
        ).fetchone()
    return row[0] if row else None


def get_ml_training_data() -> list[dict]:
    """Return all closed signals that have ML features stored, including rr_tp1 for R-multiple label."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, ml_features_json, outcome, rr_tp1
               FROM bo_signals
               WHERE status='closed'
                 AND outcome IN ('win','loss','be')
                 AND ml_features_json IS NOT NULL
               ORDER BY close_time ASC"""
        ).fetchall()
    return [_row(r) for r in rows]


def get_ml_monitor_data(limit: int = 20) -> list[dict]:
    """Recent signals with ML predictions, for UI display."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT id, signal_ref, direction, breakout_type, session,
                      quality_score, ml_prob, outcome, status, created_at
               FROM bo_signals
               WHERE ml_prob IS NOT NULL
               ORDER BY created_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


def get_recent_win_rate(n: int = 10) -> float:
    """Win rate (0-1) over the last n closed bo_signals."""
    with _conn() as conn:
        rows = conn.execute(
            """SELECT outcome FROM bo_signals
               WHERE status='closed' AND outcome IN ('win','loss','be')
               ORDER BY close_time DESC LIMIT ?""",
            (n,),
        ).fetchall()
    if not rows:
        return 0.5
    wins = sum(1 for r in rows if r[0] == "win")
    return round(wins / len(rows), 3)


def get_recent_closed_signals(limit: int = 20) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            """SELECT * FROM bo_signals
               WHERE status='closed' AND outcome IN ('win','loss','be')
               ORDER BY close_time DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [_row(r) for r in rows]


# ── Stats ─────────────────────────────────────────────────────────────────────

def get_stats() -> dict:
    with _conn() as conn:
        row = conn.execute("""
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
        """).fetchone()
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
    with _conn() as conn:
        rows = conn.execute(
            "SELECT balance FROM bo_balance_log ORDER BY ts"
        ).fetchall()
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
    with _conn() as conn:
        rows = conn.execute(
            "SELECT outcome FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be') "
            "ORDER BY close_time DESC LIMIT 10"
        ).fetchall()
    count = 0
    for r in rows:
        if r[0] == "loss":
            count += 1
        else:
            break
    return count


def get_perf_by_session() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT session,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   AVG(pnl_dollars) as avg_pnl,
                   SUM(pnl_dollars) as total_pnl
            FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be')
            GROUP BY session ORDER BY total_pnl DESC
        """).fetchall()
    return [_row(r) for r in rows]


def get_perf_by_breakout_type() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT breakout_type,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   AVG(pnl_dollars) as avg_pnl,
                   SUM(pnl_dollars) as total_pnl
            FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be')
            GROUP BY breakout_type
        """).fetchall()
    return [_row(r) for r in rows]


def get_perf_by_adx_band() -> list[dict]:
    """Performance grouped into ADX bands: 25-35, 35-50, 50+"""
    with _conn() as conn:
        rows = conn.execute("""
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
        """).fetchall()
    return [_row(r) for r in rows]


def get_perf_by_bias() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT htf_bias,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   AVG(pnl_dollars) as avg_pnl,
                   SUM(pnl_dollars) as total_pnl
            FROM bo_signals WHERE status='closed' AND outcome IN ('win','loss','be')
            GROUP BY htf_bias ORDER BY total_pnl DESC
        """).fetchall()
    return [_row(r) for r in rows]


# ── Analysis log ──────────────────────────────────────────────────────────────

def log_analysis(entry: dict) -> None:
    with _conn() as conn:
        conn.execute(
            """INSERT INTO bo_analysis_log
               (ts, session, htf_bias, h4_bias, price, atr_m15, adx,
                key_levels_json, candidate_json, result, claude_decision, suppressed_reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
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
            ),
        )


def get_analysis_log(limit: int = 40) -> list[dict]:
    with _conn() as conn:
        rows = conn.execute(
            "SELECT * FROM bo_analysis_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
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

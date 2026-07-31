"""
Isolated SQLite store for the Reversal Engine signal engine.

All tables are prefixed re_ and live in reversal_engine.db, completely
separate from the main forex_trader.db, bounce engine, and breakout engine.
"""
from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from typing import Optional

import sqlite3

_log = logging.getLogger(__name__)

_DB_PATH: str = ""
_STARTING_BALANCE = 1000.0


def init(db_path: str) -> None:
    global _DB_PATH
    _DB_PATH = db_path
    _create_schema()
    _run_migrations()
    reconcile_balance_with_trades()


@contextmanager
def _conn():
    con = sqlite3.connect(_DB_PATH, timeout=10)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


# ── Schema ────────────────────────────────────────────────────────────────────

def _create_schema() -> None:
    with _conn() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS re_signals (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at      REAL    NOT NULL,
            signal_ref      TEXT    UNIQUE,
            direction       TEXT,
            entry_low       REAL,
            entry_high      REAL,
            stop_loss       REAL,
            sl_dist         REAL,
            tp1             REAL,
            tp2             REAL,
            tp3             REAL,
            tp4             REAL,
            tp5             REAL,
            tp6             REAL,
            tp7             REAL,
            tp8             REAL,
            rr_tp1          REAL,
            level_price     REAL,
            level_type      TEXT,
            level_score     REAL,
            session         TEXT,
            htf_bias        TEXT,
            h1_bias         TEXT,
            atr             REAL,
            adx             REAL,
            price_at_signal REAL,
            status          TEXT    NOT NULL DEFAULT 'pending',
            outcome         TEXT    NOT NULL DEFAULT 'open',
            trigger_price   REAL,
            trigger_time    REAL,
            close_price     REAL,
            close_time      REAL,
            pnl_pts         REAL,
            pnl_dollars     REAL,
            net_pnl_dollars REAL,
            balance_after   REAL,
            sl_moved_to_be  INTEGER NOT NULL DEFAULT 0,
            ml_features_json TEXT,
            ml_prob         REAL,
            mt5_ticket      INTEGER,
            vantage_signal_id TEXT,
            live_exec_status TEXT,
            strategy        TEXT    DEFAULT 'signal_climber',
            correlated_ref_signal_id TEXT,
            correlation_time_delta_s REAL,
            correlation_distance_pts REAL,
            correlation_confirmed INTEGER NOT NULL DEFAULT 0
        );

        CREATE TABLE IF NOT EXISTS re_levels (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            updated_at  REAL,
            level_type  TEXT,
            price       REAL,
            direction   TEXT,
            strength    INTEGER DEFAULT 1,
            active      INTEGER DEFAULT 1,
            source      TEXT DEFAULT 'engine',
            notes       TEXT
        );

        CREATE TABLE IF NOT EXISTS re_correlation (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            date            TEXT UNIQUE,
            re_signals_sent INTEGER DEFAULT 0,
            re_correlated  INTEGER DEFAULT 0,
            ref_signals_sent INTEGER DEFAULT 0,
            ref_predicted   INTEGER DEFAULT 0,
            avg_lead_time_s REAL,
            correlation_rate REAL
        );

        CREATE TABLE IF NOT EXISTS re_analysis_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL,
            session     TEXT,
            htf_bias    TEXT,
            price       REAL,
            atr         REAL,
            adx         REAL,
            levels_json TEXT,
            result      TEXT,
            reason      TEXT
        );

        CREATE TABLE IF NOT EXISTS re_config (
            key     TEXT PRIMARY KEY,
            value   TEXT NOT NULL DEFAULT ''
        );

        CREATE TABLE IF NOT EXISTS re_balance_log (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            ts          REAL,
            balance     REAL,
            change_amt  REAL,
            reason      TEXT,
            signal_id   INTEGER
        );

        CREATE TABLE IF NOT EXISTS re_near_miss (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            ts              REAL,
            re_signal_id   INTEGER,
            ref_signal_id   TEXT,
            direction       TEXT,
            time_delta_s    REAL,
            distance_pts    REAL,
            reason          TEXT
        );

        CREATE TABLE IF NOT EXISTS re_daily_research (
            date                TEXT PRIMARY KEY,
            discipline_score    REAL,
            aggression_score    REAL,
            summary             TEXT,
            notable_trades      TEXT,
            entry_logic_notes   TEXT,
            risk_mgmt_notes     TEXT,
            n_messages          INTEGER,
            n_images_analyzed   INTEGER,
            raw_json            TEXT,
            created_at          REAL
        );
        """)


def _run_migrations() -> None:
    # Ladder scale-out support: partials banked at TP1/TP4 (REF-style) with the
    # remainder riding to TP7. max_tp_hit records ladder progress for analytics
    # and honest win-rate accounting (TP1 touch = win, like the REF channel).
    _migrations = [
        "ALTER TABLE re_signals ADD COLUMN partial_pnl_dollars REAL DEFAULT 0",
        "ALTER TABLE re_signals ADD COLUMN remaining_frac REAL DEFAULT 1.0",
        "ALTER TABLE re_signals ADD COLUMN max_tp_hit INTEGER DEFAULT 0",
        # Which real channel this signal is modelled on -- added 2026-07 when
        # Reversal Engine grew a second profile (Gold Diggers 2.0 / Institutional,
        # via the ICT Unicorn detector in ict_patterns.py) alongside the
        # original Gold Diggers VIP one. correlated_ref_signal_id is reused
        # generically for whichever channel this row's source_channel says,
        # to avoid a second parallel correlation-column set.
        "ALTER TABLE re_signals ADD COLUMN source_channel TEXT DEFAULT 'Gold Diggers VIP'",
        # Fresh fill-time re-evaluation (2026-07-17) — see
        # store_ml_prob_at_fill and engine.py's _try_live_execute. Separate
        # from ml_prob/htf_bias (creation-time) so both remain visible.
        "ALTER TABLE re_signals ADD COLUMN ml_prob_at_fill REAL",
        "ALTER TABLE re_signals ADD COLUMN htf_bias_at_fill TEXT",
        # The REF level type this signal correlated against (2026-07-31).
        # Classified at correlation time by reversal_engine_correlate's
        # _classify_ref_level but previously discarded immediately after being
        # counted, so when the signal later closed there was no way to tell
        # ml_engine which level type had just won or lost -- which is why
        # record_ref_signal was only ever called with was_win=None and the
        # `wins` counter behind ref_level_win_rate sat at 0 forever.
        "ALTER TABLE re_signals ADD COLUMN correlated_ref_level_type TEXT",
    ]
    with _conn() as con:
        for stmt in _migrations:
            try:
                con.execute(stmt)
            except sqlite3.OperationalError:
                pass  # column already exists


# ── Config helpers ────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    try:
        with _conn() as con:
            row = con.execute("SELECT value FROM re_config WHERE key=?", (key,)).fetchone()
        return row[0] if row else default
    except Exception:
        return default


def set_config(key: str, value: str) -> None:
    with _conn() as con:
        con.execute(
            "INSERT INTO re_config (key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, str(value)),
        )


# ── Balance ───────────────────────────────────────────────────────────────────

def get_virtual_balance() -> float:
    try:
        val = get_config("virtual_balance", str(_STARTING_BALANCE))
        return float(val)
    except Exception:
        return _STARTING_BALANCE


def _update_balance(delta: float, reason: str, signal_id: Optional[int] = None) -> float:
    bal = get_virtual_balance() + delta
    set_config("virtual_balance", str(round(bal, 4)))
    with _conn() as con:
        con.execute(
            "INSERT INTO re_balance_log (ts,balance,change_amt,reason,signal_id) VALUES (?,?,?,?,?)",
            (time.time(), bal, delta, reason, signal_id),
        )
    return bal


def reconcile_balance_with_trades() -> Optional[float]:
    try:
        with _conn() as con:
            row = con.execute(
                "SELECT COALESCE(SUM(net_pnl_dollars),0) FROM re_signals "
                "WHERE status='closed' AND net_pnl_dollars IS NOT NULL"
            ).fetchone()
        total_pnl = float(row[0]) if row and row[0] else 0.0
        correct = round(_STARTING_BALANCE + total_pnl, 4)
        current = get_virtual_balance()
        if abs(correct - current) > 0.01:
            set_config("virtual_balance", str(correct))
            _log.info("[RE-DB] Balance reconciled: %.2f → %.2f", current, correct)
        return correct
    except Exception as exc:
        _log.debug("[RE-DB] reconcile failed: %s", exc)
        return None


def get_max_drawdown() -> float:
    try:
        with _conn() as con:
            rows = con.execute(
                "SELECT balance FROM re_balance_log ORDER BY ts"
            ).fetchall()
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
        return max_dd
    except Exception:
        return 0.0


# ── Signal CRUD ───────────────────────────────────────────────────────────────

def create_signal(data: dict) -> int:
    data.setdefault("created_at", time.time())
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    with _conn() as con:
        cur = con.execute(
            f"INSERT OR IGNORE INTO re_signals ({cols}) VALUES ({placeholders})",
            list(data.values()),
        )
    return cur.lastrowid or 0


def get_open_signals() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_signals WHERE status IN ('pending','triggered') ORDER BY created_at DESC"
        ).fetchall()
    return [dict(r) for r in rows]


def get_all_signals(limit: int = 100) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_signals ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def get_signal_by_id(sig_id: int) -> Optional[dict]:
    with _conn() as con:
        row = con.execute("SELECT * FROM re_signals WHERE id=?", (sig_id,)).fetchone()
    return dict(row) if row else None


def trigger_signal(sig_id: int, price: float) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET status='triggered', trigger_price=?, trigger_time=? WHERE id=?",
            (price, time.time(), sig_id),
        )


def close_signal(sig_id: int, close_price: float, outcome: str,
                 pnl_pts: float = 0.0, net_pnl_dollars: float = 0.0,
                 pnl_dollars: float = None, balance_delta: float = None) -> None:
    """Close a signal. `net_pnl_dollars` is stored as the TOTAL realized result
    (ladder partials + final leg). `balance_delta` is what still needs adding
    to the balance — the final leg only, since book_partial_close already
    banked the partials. Defaults to net_pnl_dollars for non-ladder closes."""
    now = time.time()
    if pnl_dollars is None:
        pnl_dollars = net_pnl_dollars
    if balance_delta is None:
        balance_delta = net_pnl_dollars
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET status='closed', outcome=?, close_price=?, close_time=?, "
            "pnl_pts=?, pnl_dollars=?, net_pnl_dollars=? WHERE id=?",
            (outcome, close_price, now, pnl_pts, pnl_dollars, net_pnl_dollars, sig_id),
        )
    bal = _update_balance(balance_delta, f"signal_close_{outcome}", sig_id)
    with _conn() as con:
        con.execute("UPDATE re_signals SET balance_after=? WHERE id=?", (bal, sig_id))


def move_sl_to_be(sig_id: int, be_price: float | None = None) -> None:
    """Move SL to break-even. Pass entry ± round-trip cost as `be_price` so a
    BE exit on the remaining fraction nets $0 instead of losing the spread."""
    with _conn() as con:
        row = con.execute(
            "SELECT entry_low, entry_high FROM re_signals WHERE id=?", (sig_id,)
        ).fetchone()
        if not row:
            return
        px = be_price if be_price is not None else (row[0] + row[1]) / 2
        con.execute(
            "UPDATE re_signals SET stop_loss=?, sl_moved_to_be=1 WHERE id=?",
            (round(px, 2), sig_id),
        )


def set_stop_loss(sig_id: int, price: float) -> None:
    """Trail the stop (e.g. to TP1 after the TP4 partial books)."""
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET stop_loss=? WHERE id=?", (round(price, 2), sig_id)
        )


def book_partial_close(sig_id: int, leg_net_dollars: float, frac_closed: float,
                       tp_idx: int) -> float:
    """Bank realized profit for a fraction of the position at a ladder TP
    without closing the signal. Updates the running balance immediately.
    Returns the new remaining_frac."""
    with _conn() as con:
        row = con.execute(
            "SELECT remaining_frac, partial_pnl_dollars, max_tp_hit "
            "FROM re_signals WHERE id=?", (sig_id,)
        ).fetchone()
        if not row:
            return 0.0
        remaining = float(row[0] if row[0] is not None else 1.0)
        booked    = float(row[1] or 0.0)
        max_tp    = int(row[2] or 0)
        new_remaining = round(max(0.0, remaining - frac_closed), 4)
        con.execute(
            "UPDATE re_signals SET partial_pnl_dollars=?, remaining_frac=?, max_tp_hit=? "
            "WHERE id=?",
            (round(booked + leg_net_dollars, 2), new_remaining,
             max(max_tp, tp_idx), sig_id),
        )
    _update_balance(leg_net_dollars, f"re_partial_tp{tp_idx}", sig_id)
    return new_remaining


def expire_signal(sig_id: int, reason: str) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET status='expired', outcome='expired', close_time=? WHERE id=?",
            (time.time(), sig_id),
        )


def store_ml_features(sig_id: int, features: list) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET ml_features_json=? WHERE id=?",
            (json.dumps(features), sig_id),
        )


def store_ml_prob(sig_id: int, prob: float) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET ml_prob=? WHERE id=?", (prob, sig_id)
        )


def store_ml_prob_at_fill(sig_id: int, prob: float, htf_bias: str) -> None:
    """Fresh ML re-score + bias, recomputed at fill time (see engine.py's
    _try_live_execute) rather than trusting the creation-time ml_prob/
    htf_bias, which can be anywhere from minutes to 4h stale by the time a
    pending zone signal actually fills. Kept separate from ml_prob/htf_bias
    (the creation-time values) so both are visible for comparison."""
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET ml_prob_at_fill=?, htf_bias_at_fill=? WHERE id=?",
            (prob, htf_bias, sig_id),
        )


def update_correlation(sig_id: int, ref_signal_id: str,
                       time_delta_s: float, distance_pts: float) -> None:
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET correlated_ref_signal_id=?, correlation_time_delta_s=?, "
            "correlation_distance_pts=?, correlation_confirmed=1 WHERE id=?",
            (ref_signal_id, time_delta_s, distance_pts, sig_id),
        )


def log_near_miss(re_signal_id: int, ref_signal_id: str, direction: str,
                  time_delta_s: float, distance_pts: float, reason: str) -> None:
    """Record a correlation attempt that matched direction but missed the
    price/time match window — used to empirically tune the match thresholds.
    Idempotent: each RE/REF pair is only recorded once."""
    with _conn() as con:
        already = con.execute(
            "SELECT id FROM re_near_miss WHERE re_signal_id=? AND ref_signal_id=?",
            (re_signal_id, ref_signal_id),
        ).fetchone()
        if already:
            return
        con.execute(
            "INSERT INTO re_near_miss (ts, re_signal_id, ref_signal_id, direction, "
            "time_delta_s, distance_pts, reason) VALUES (?,?,?,?,?,?,?)",
            (time.time(), re_signal_id, ref_signal_id, direction,
             time_delta_s, distance_pts, reason),
        )


def get_near_misses(limit: int = 200) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_near_miss ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def update_live_exec(sig_id: int, mt5_ticket: Optional[int] = None,
                     vantage_sig_id: Optional[str] = None, status: str = "") -> None:
    with _conn() as con:
        con.execute(
            "UPDATE re_signals SET mt5_ticket=?, vantage_signal_id=?, live_exec_status=? WHERE id=?",
            (mt5_ticket, vantage_sig_id, status, sig_id),
        )


def get_ml_training_data() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_signals WHERE status='closed' AND ml_features_json IS NOT NULL"
        ).fetchall()
    return [dict(r) for r in rows]


def store_daily_research(date: str, discipline_score: float, aggression_score: float,
                          summary: str, notable_trades: str, entry_logic_notes: str,
                          risk_mgmt_notes: str, n_messages: int, n_images_analyzed: int,
                          raw_json: str) -> None:
    """One row per calendar date — re-running the same night's job overwrites
    (INSERT OR REPLACE) rather than duplicating."""
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO re_daily_research "
            "(date, discipline_score, aggression_score, summary, notable_trades, "
            "entry_logic_notes, risk_mgmt_notes, n_messages, n_images_analyzed, raw_json, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (date, discipline_score, aggression_score, summary, notable_trades,
             entry_logic_notes, risk_mgmt_notes, n_messages, n_images_analyzed,
             raw_json, time.time()),
        )


def get_latest_daily_research() -> Optional[dict]:
    with _conn() as con:
        row = con.execute(
            "SELECT * FROM re_daily_research ORDER BY date DESC LIMIT 1"
        ).fetchone()
    return dict(row) if row else None


def get_recent_win_rate(n: int = 20) -> float:
    with _conn() as con:
        rows = con.execute(
            "SELECT outcome FROM re_signals WHERE status='closed' ORDER BY close_time DESC LIMIT ?",
            (n,),
        ).fetchall()
    if not rows:
        return 0.5
    wins = sum(1 for r in rows if r[0] == "win")
    return wins / len(rows)


def get_stats() -> dict:
    try:
        with _conn() as con:
            r = con.execute("""
                SELECT
                    COUNT(*) as total,
                    SUM(CASE WHEN outcome='win' THEN 1 ELSE 0 END) as wins,
                    SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                    SUM(CASE WHEN outcome='be' THEN 1 ELSE 0 END) as bes,
                    SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) as pending,
                    SUM(CASE WHEN status='triggered' THEN 1 ELSE 0 END) as triggered,
                    AVG(CASE WHEN status='closed' THEN net_pnl_dollars END) as avg_pnl,
                    SUM(CASE WHEN status='closed' THEN net_pnl_dollars ELSE 0 END) as total_pnl,
                    SUM(CASE WHEN correlation_confirmed=1 THEN 1 ELSE 0 END) as correlated
                FROM re_signals
            """).fetchone()
        closed = (r["wins"] or 0) + (r["losses"] or 0) + (r["bes"] or 0)
        win_rate = (r["wins"] / closed * 100) if closed > 0 else 0.0
        corr_rate = (r["correlated"] / r["total"] * 100) if r["total"] else 0.0
        return {
            "total": r["total"] or 0,
            "wins": r["wins"] or 0,
            "losses": r["losses"] or 0,
            "bes": r["bes"] or 0,
            "pending": r["pending"] or 0,
            "triggered": r["triggered"] or 0,
            "win_rate": win_rate,
            "avg_pnl": r["avg_pnl"] or 0.0,
            "total_pnl": r["total_pnl"] or 0.0,
            "correlated": r["correlated"] or 0,
            "correlation_rate": corr_rate,
        }
    except Exception:
        return {"total": 0, "wins": 0, "losses": 0, "bes": 0, "pending": 0,
                "triggered": 0, "win_rate": 0, "avg_pnl": 0, "total_pnl": 0,
                "correlated": 0, "correlation_rate": 0}


# ── Performance breakdowns ─────────────────────────────────────────────────────
# Same shape as breakout_signal/database.py's get_perf_by_*() — feeds the
# Performance Analytics section in the Reversal Engine UI panel (parity with Bounce/
# Breakout, which already had this; Reversal Engine didn't).

def get_perf_by_session() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT session,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   AVG(net_pnl_dollars) as avg_pnl,
                   SUM(net_pnl_dollars) as total_pnl
            FROM re_signals WHERE status='closed' AND outcome IN ('win','loss','be')
            GROUP BY session ORDER BY total_pnl DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_perf_by_bias() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT htf_bias,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   AVG(net_pnl_dollars) as avg_pnl,
                   SUM(net_pnl_dollars) as total_pnl
            FROM re_signals WHERE status='closed' AND outcome IN ('win','loss','be')
            GROUP BY htf_bias ORDER BY total_pnl DESC
        """).fetchall()
    return [dict(r) for r in rows]


def get_perf_by_level_type() -> list[dict]:
    with _conn() as conn:
        rows = conn.execute("""
            SELECT level_type,
                   SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
                   SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
                   AVG(net_pnl_dollars) as avg_pnl,
                   SUM(net_pnl_dollars) as total_pnl
            FROM re_signals WHERE status='closed' AND outcome IN ('win','loss','be')
            GROUP BY level_type ORDER BY total_pnl DESC
        """).fetchall()
    return [dict(r) for r in rows]


# ── Correlation daily stats ───────────────────────────────────────────────────

def upsert_daily_correlation(date: str, **kwargs) -> None:
    with _conn() as con:
        existing = con.execute(
            "SELECT * FROM re_correlation WHERE date=?", (date,)
        ).fetchone()
        if existing:
            sets = ", ".join(f"{k}=?" for k in kwargs)
            con.execute(
                f"UPDATE re_correlation SET {sets} WHERE date=?",
                list(kwargs.values()) + [date],
            )
        else:
            kwargs["date"] = date
            cols = ", ".join(kwargs.keys())
            ph = ", ".join("?" for _ in kwargs)
            con.execute(f"INSERT INTO re_correlation ({cols}) VALUES ({ph})", list(kwargs.values()))


def count_today_signals() -> int:
    """SQL COUNT of signals created today (UTC). More reliable than Python filter over get_all_signals()."""
    try:
        from datetime import datetime, timezone
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        with _conn() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM re_signals WHERE created_at >= ?", (today_start,)
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def count_today_correlated() -> int:
    """SQL COUNT of confirmed correlations among signals created today (UTC)."""
    try:
        from datetime import datetime, timezone
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        with _conn() as con:
            row = con.execute(
                "SELECT COUNT(*) FROM re_signals WHERE created_at >= ? AND correlation_confirmed=1",
                (today_start,),
            ).fetchone()
        return int(row[0]) if row else 0
    except Exception:
        return 0


def get_correlation_history(days: int = 30) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_correlation ORDER BY date DESC LIMIT ?", (days,)
        ).fetchall()
    return [dict(r) for r in rows]


# ── Level tracking ────────────────────────────────────────────────────────────

def upsert_level(level_type: str, price: float, direction: str,
                 source: str = "engine", notes: str = "") -> None:
    price = round(price, 2)
    with _conn() as con:
        existing = con.execute(
            "SELECT id, strength FROM re_levels WHERE level_type=? AND ABS(price-?)<=1 AND direction=?",
            (level_type, price, direction),
        ).fetchone()
        if existing:
            con.execute(
                "UPDATE re_levels SET strength=strength+1, updated_at=?, price=?, active=1 WHERE id=?",
                (time.time(), price, existing[0]),
            )
        else:
            con.execute(
                "INSERT INTO re_levels (updated_at,level_type,price,direction,source,notes) VALUES (?,?,?,?,?,?)",
                (time.time(), level_type, price, direction, source, notes),
            )


def get_active_levels() -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_levels WHERE active=1 ORDER BY updated_at DESC LIMIT 50"
        ).fetchall()
    return [dict(r) for r in rows]


def deactivate_old_levels(older_than_hours: int = 48) -> None:
    cutoff = time.time() - older_than_hours * 3600
    with _conn() as con:
        con.execute(
            "UPDATE re_levels SET active=0 WHERE updated_at < ? AND source != 'ref'",
            (cutoff,),
        )


# ── Analysis log ─────────────────────────────────────────────────────────────

def log_analysis(entry: dict) -> None:
    try:
        with _conn() as con:
            con.execute(
                "INSERT INTO re_analysis_log (ts,session,htf_bias,price,atr,adx,levels_json,result,reason) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    entry.get("ts", time.time()),
                    entry.get("session"), entry.get("htf_bias"),
                    entry.get("price"), entry.get("atr"), entry.get("adx"),
                    json.dumps(entry.get("levels", [])),
                    entry.get("result"), entry.get("reason"),
                ),
            )
    except Exception as exc:
        _log.debug("[RE-DB] log_analysis error: %s", exc)


def get_analysis_log(limit: int = 50) -> list[dict]:
    with _conn() as con:
        rows = con.execute(
            "SELECT * FROM re_analysis_log ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["levels"] = json.loads(d.get("levels_json") or "[]")
        except Exception:
            d["levels"] = []
        result.append(d)
    return result

"""Isolated data store for the Reversal Engine signal engine, rebuilt on the shared
DbAdapter (backend.src.db) instead of raw sqlite3 -- see
docs/todo/refactor/backend-foundation/030-migrate-reversal-engine-repo-layer.md.

Structural port of reversal_engine/database.py: same tables (re_ prefix),
same function signatures, same behavior -- proven by running
tests/reversal_engine/test_database_characterization.py against this module
unmodified. The one deliberate behavior change: close_signal() and
book_partial_close() now wrap their multi-statement writes in a single
get_db().transaction(), instead of database.py's several independent
connections for what should be one atomic operation.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

import sqlite3
from datetime import datetime, timezone

from backend.src.config import get as cfg_get
from backend.src.db import connection as _conn_mod
from backend.src.services.reversal_engine import _repo_schema
from backend.src.utils.sql_identifiers import set_clause_for

_NAMESPACE = "reversal_engine"


def get_db():
    return _conn_mod.get_db(_NAMESPACE)


def init_db(db_path: str):
    return _conn_mod.init_db(db_path, _NAMESPACE)


def close_db():
    """Release this namespace's connection.

    Tests that build a temp database must call this before unlinking it:
    the adapter holds the file open (and its WAL sidecars), and Windows
    refuses to remove a file that still has an open handle.
    """
    return _conn_mod.close_db(_NAMESPACE)


_log = logging.getLogger(__name__)

_STARTING_BALANCE = 1000.0


def init(db_path: str) -> None:
    init_db(db_path)
    _repo_schema.create_schema(get_db)
    _run_migrations()
    reconcile_balance_with_trades()


# ── Schema ────────────────────────────────────────────────────────────────────

def _run_migrations() -> None:
    migrations = [
        "ALTER TABLE re_signals ADD COLUMN partial_pnl_dollars REAL DEFAULT 0",
        "ALTER TABLE re_signals ADD COLUMN remaining_frac REAL DEFAULT 1.0",
        "ALTER TABLE re_signals ADD COLUMN max_tp_hit INTEGER DEFAULT 0",
        "ALTER TABLE re_signals ADD COLUMN source_channel TEXT DEFAULT 'Gold Diggers VIP'",
        "ALTER TABLE re_signals ADD COLUMN ml_prob_at_fill REAL",
        "ALTER TABLE re_signals ADD COLUMN htf_bias_at_fill TEXT",
        # The REF level type this signal correlated against (2026-07-31).
        # Classified by _classify_ref_level then discarded, so on close nothing
        # could tell ml_engine which level type won -- record_ref_signal was
        # always called with was_win=None and ref_level_win_rate's `wins` sat
        # at 0 forever. Mirrored in reversal_engine/database.py's twin list.
        "ALTER TABLE re_signals ADD COLUMN correlated_ref_level_type TEXT",
        # Excursion watermarks (2026-09-10): record_excursion() wrote these
        # and nothing created them -- a fresh install failed silently on "no
        # such column". See reversal-engine/020.
        "ALTER TABLE re_signals ADD COLUMN mfe_pts REAL",
        "ALTER TABLE re_signals ADD COLUMN mae_pts REAL",
        # The stop actually in force (2026-09-10). sl_dist is the stop at
        # OPEN and the EA trails silently. See reversal-engine/020.
        "ALTER TABLE re_signals ADD COLUMN last_seen_sl REAL",
        # How the excursion was obtained (2026-09-11): the five-second live
        # sampler, or a reconstruction from broker tick history. NULL means
        # the sampler, which is the only thing that wrote one before this
        # column existed. See measure_repo and reversal-engine/200.
        "ALTER TABLE re_signals ADD COLUMN excursion_source TEXT",
    ]
    for stmt in migrations:
        try:
            get_db().run(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists


# ── Config helpers ────────────────────────────────────────────────────────────

def get_config(key: str, default: str = "") -> str:
    try:
        row = get_db().get("SELECT value FROM re_config WHERE key=?", key)
        return row[0] if row else default
    except Exception:
        return default


def set_config(key: str, value: str) -> None:
    get_db().run(
        "INSERT INTO re_config (key,value) VALUES (?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        key, str(value),
    )


# ── Balance ───────────────────────────────────────────────────────────────────

def get_virtual_balance() -> float:
    try:
        return float(get_config("virtual_balance", str(_STARTING_BALANCE)))
    except Exception:
        return _STARTING_BALANCE


def _update_balance(delta: float, reason: str, signal_id: Optional[int] = None) -> float:
    """Participates transparently in an ambient transaction() if one is
    active on the shared adapter -- see module docstring."""
    bal = get_virtual_balance() + delta
    set_config("virtual_balance", str(round(bal, 4)))
    get_db().run(
        "INSERT INTO re_balance_log (ts,balance,change_amt,reason,signal_id) VALUES (?,?,?,?,?)",
        time.time(), bal, delta, reason, signal_id,
    )
    return bal


def reconcile_balance_with_trades() -> Optional[float]:
    try:
        row = get_db().get(
            "SELECT COALESCE(SUM(net_pnl_dollars),0) FROM re_signals "
            "WHERE status='closed' AND net_pnl_dollars IS NOT NULL"
        )
        total_pnl = float(row[0]) if row and row[0] else 0.0
        correct = round(_STARTING_BALANCE + total_pnl, 4)
        current = get_virtual_balance()
        if abs(correct - current) > 0.01:
            set_config("virtual_balance", str(correct))
            _log.info("[RE-Repo] Balance reconciled: %.2f -> %.2f", current, correct)
        return correct
    except Exception as exc:
        _log.debug("[RE-Repo] reconcile failed: %s", exc)
        return None


# ── Signal CRUD ───────────────────────────────────────────────────────────────

def create_signal(data: dict) -> int:
    data = dict(data)
    data.setdefault("created_at", time.time())
    cols = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    result = get_db().run(
        f"INSERT OR IGNORE INTO re_signals ({cols}) VALUES ({placeholders})",
        *data.values(),
    )
    return result.lastrowid or 0


def get_open_signals() -> list[dict]:
    rows = get_db().all(
        "SELECT * FROM re_signals WHERE status IN ('pending','triggered') ORDER BY created_at DESC"
    )
    return [dict(r) for r in rows]


def get_all_signals(limit: int = 100) -> list[dict]:
    rows = get_db().all("SELECT * FROM re_signals ORDER BY created_at DESC, id DESC LIMIT ?", limit)
    return [dict(r) for r in rows]


def get_recent_outcomes_by_direction(direction: str, since_ts: float, limit: int) -> list[str]:
    """Recent closed-signal outcomes for a direction, newest first -- feeds
    the consecutive-loss cooldown check in reversal_engine_service. New in
    the repo (030/040): database.py's equivalent was a raw sqlite3 query
    inline in engine.py (bypassing database.py's own API entirely) rather
    than a named function here."""
    rows = get_db().all(
        "SELECT outcome FROM re_signals "
        "WHERE direction=? AND close_time>? AND outcome IN ('win','loss','be') "
        "ORDER BY close_time DESC LIMIT ?",
        direction, since_ts, limit,
    )
    return [r[0] for r in rows]


def get_signal_by_id(sig_id: int) -> Optional[dict]:
    row = get_db().get("SELECT * FROM re_signals WHERE id=?", sig_id)
    return dict(row) if row else None


def trigger_signal(sig_id: int, price: float) -> None:
    get_db().run(
        "UPDATE re_signals SET status='triggered', trigger_price=?, trigger_time=? WHERE id=?",
        price, time.time(), sig_id,
    )


def close_signal(sig_id: int, close_price: float, outcome: str,
                 pnl_pts: float = 0.0, net_pnl_dollars: float = 0.0,
                 pnl_dollars: float = None, balance_delta: float = None) -> None:
    """Close a signal. `net_pnl_dollars` is stored as the TOTAL realized
    result (ladder partials + final leg). `balance_delta` is what still
    needs adding to the balance -- the final leg only, since
    book_partial_close already banked the partials. Defaults to
    net_pnl_dollars for non-ladder closes.

    Everything below runs in ONE transaction -- database.py's equivalent
    does the status update, the balance read/write, and the balance_after
    write as three independent connections. See 030's task file."""
    if pnl_dollars is None:
        pnl_dollars = net_pnl_dollars
    if balance_delta is None:
        balance_delta = net_pnl_dollars
    now = time.time()
    with get_db().transaction():
        get_db().run(
            "UPDATE re_signals SET status='closed', outcome=?, close_price=?, close_time=?, "
            "pnl_pts=?, pnl_dollars=?, net_pnl_dollars=? WHERE id=?",
            outcome, close_price, now, pnl_pts, pnl_dollars, net_pnl_dollars, sig_id,
        )
        bal = _update_balance(balance_delta, f"signal_close_{outcome}", sig_id)
        get_db().run("UPDATE re_signals SET balance_after=? WHERE id=?", bal, sig_id)


def move_sl_to_be(sig_id: int, be_price: float | None = None) -> None:
    """Move SL to break-even. Pass entry ± round-trip cost as `be_price` so
    a BE exit on the remaining fraction nets $0 instead of losing the spread."""
    row = get_db().get("SELECT entry_low, entry_high FROM re_signals WHERE id=?", sig_id)
    if not row:
        return
    px = be_price if be_price is not None else (row[0] + row[1]) / 2
    get_db().run(
        "UPDATE re_signals SET stop_loss=?, sl_moved_to_be=1 WHERE id=?",
        round(px, 2), sig_id,
    )


def set_stop_loss(sig_id: int, price: float) -> None:
    """Trail the stop (e.g. to TP1 after the TP4 partial books)."""
    get_db().run("UPDATE re_signals SET stop_loss=? WHERE id=?", round(price, 2), sig_id)


def book_partial_close(sig_id: int, leg_net_dollars: float, frac_closed: float,
                       tp_idx: int) -> float:
    """Bank realized profit for a fraction of the position at a ladder TP
    without closing the signal. Updates the running balance immediately.
    Returns the new remaining_frac.

    Wrapped in ONE transaction -- database.py's equivalent updates the
    signal row and the balance as two independent connections. See 030's
    task file."""
    with get_db().transaction():
        row = get_db().get(
            "SELECT remaining_frac, partial_pnl_dollars, max_tp_hit FROM re_signals WHERE id=?",
            sig_id,
        )
        if not row:
            return 0.0
        remaining = float(row[0] if row[0] is not None else 1.0)
        booked    = float(row[1] or 0.0)
        max_tp    = int(row[2] or 0)
        new_remaining = round(max(0.0, remaining - frac_closed), 4)
        get_db().run(
            "UPDATE re_signals SET partial_pnl_dollars=?, remaining_frac=?, max_tp_hit=? WHERE id=?",
            round(booked + leg_net_dollars, 2), new_remaining, max(max_tp, tp_idx), sig_id,
        )
        _update_balance(leg_net_dollars, f"re_partial_tp{tp_idx}", sig_id)
    return new_remaining


def expire_signal(sig_id: int, reason: str) -> None:
    get_db().run(
        "UPDATE re_signals SET status='expired', outcome='expired', close_time=? WHERE id=?",
        time.time(), sig_id,
    )


def store_ml_features(sig_id: int, features: list) -> None:
    get_db().run("UPDATE re_signals SET ml_features_json=? WHERE id=?", json.dumps(features), sig_id)


def store_ml_prob(sig_id: int, prob: float) -> None:
    get_db().run("UPDATE re_signals SET ml_prob=? WHERE id=?", prob, sig_id)


def store_ml_prob_at_fill(sig_id: int, prob: float, htf_bias: str) -> None:
    """Fresh ML re-score + bias, recomputed at fill time -- kept separate
    from ml_prob/htf_bias (creation-time) so both remain visible."""
    get_db().run(
        "UPDATE re_signals SET ml_prob_at_fill=?, htf_bias_at_fill=? WHERE id=?",
        prob, htf_bias, sig_id,
    )


def update_correlation(sig_id: int, ref_signal_id: str,
                       time_delta_s: float, distance_pts: float,
                       ref_level_type: Optional[str] = None) -> None:
    """`ref_level_type` is persisted so the eventual win/loss can be credited
    back to that level type when this signal closes (ml_engine.record_outcome
    -> record_ref_signal(was_win=...)). Without it the correlation's outcome
    was unrecoverable and ref_level_win_rate could only ever read 0."""
    get_db().run(
        "UPDATE re_signals SET correlated_ref_signal_id=?, correlation_time_delta_s=?, "
        "correlation_distance_pts=?, correlation_confirmed=1, correlated_ref_level_type=? "
        "WHERE id=?",
        ref_signal_id, time_delta_s, distance_pts, ref_level_type, sig_id,
    )


def log_near_miss(re_signal_id: int, ref_signal_id: str, direction: str,
                  time_delta_s: float, distance_pts: float, reason: str) -> None:
    """Idempotent: each RE/REF pair is only recorded once."""
    already = get_db().get(
        "SELECT id FROM re_near_miss WHERE re_signal_id=? AND ref_signal_id=?",
        re_signal_id, ref_signal_id,
    )
    if already:
        return
    get_db().run(
        "INSERT INTO re_near_miss (ts, re_signal_id, ref_signal_id, direction, "
        "time_delta_s, distance_pts, reason) VALUES (?,?,?,?,?,?,?)",
        time.time(), re_signal_id, ref_signal_id, direction, time_delta_s, distance_pts, reason,
    )


def get_near_misses(limit: int = 200) -> list[dict]:
    rows = get_db().all("SELECT * FROM re_near_miss ORDER BY ts DESC LIMIT ?", limit)
    return [dict(r) for r in rows]


def update_live_exec(sig_id: int, mt5_ticket: Optional[int] = None,
                     vantage_sig_id: Optional[str] = None, status: str = "") -> None:
    get_db().run(
        "UPDATE re_signals SET mt5_ticket=?, vantage_signal_id=?, live_exec_status=? WHERE id=?",
        mt5_ticket, vantage_sig_id, status, sig_id,
    )


def get_ml_training_data() -> list[dict]:
    rows = get_db().all(
        "SELECT * FROM re_signals WHERE status='closed' AND ml_features_json IS NOT NULL"
    )
    return [dict(r) for r in rows]


def store_daily_research(date: str, discipline_score: float, aggression_score: float,
                          summary: str, notable_trades: str, entry_logic_notes: str,
                          risk_mgmt_notes: str, n_messages: int, n_images_analyzed: int,
                          raw_json: str) -> None:
    """One row per calendar date -- re-running the same night's job
    overwrites (INSERT OR REPLACE) rather than duplicating."""
    get_db().run(
        "INSERT OR REPLACE INTO re_daily_research "
        "(date, discipline_score, aggression_score, summary, notable_trades, "
        "entry_logic_notes, risk_mgmt_notes, n_messages, n_images_analyzed, raw_json, created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        date, discipline_score, aggression_score, summary, notable_trades,
        entry_logic_notes, risk_mgmt_notes, n_messages, n_images_analyzed,
        raw_json, time.time(),
    )


def get_latest_daily_research() -> Optional[dict]:
    row = get_db().get("SELECT * FROM re_daily_research ORDER BY date DESC LIMIT 1")
    return dict(row) if row else None


def get_recent_win_rate(n: int = 20) -> float:
    rows = get_db().all(
        "SELECT outcome FROM re_signals WHERE status='closed' ORDER BY close_time DESC LIMIT ?",
        n,
    )
    if not rows:
        return 0.5
    wins = sum(1 for r in rows if r[0] == "win")
    return wins / len(rows)


# ── Correlation daily stats ───────────────────────────────────────────────────

def upsert_daily_correlation(date: str, **kwargs) -> None:
    with get_db().transaction():
        existing = get_db().get("SELECT * FROM re_correlation WHERE date=?", date)
        if existing:
            sets = set_clause_for(kwargs)
            get_db().run(f"UPDATE re_correlation SET {sets} WHERE date=?", *kwargs.values(), date)
        else:
            kwargs = dict(kwargs)
            kwargs["date"] = date
            cols = ", ".join(kwargs.keys())
            ph = ", ".join("?" for _ in kwargs)
            get_db().run(f"INSERT INTO re_correlation ({cols}) VALUES ({ph})", *kwargs.values())


def count_today_signals() -> int:
    """COUNT of signals created today (UTC)."""
    try:
        from datetime import datetime, timezone
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        row = get_db().get("SELECT COUNT(*) FROM re_signals WHERE created_at >= ?", today_start)
        return int(row[0]) if row else 0
    except Exception:
        return 0


def count_today_correlated() -> int:
    """COUNT of confirmed correlations among signals created today (UTC)."""
    try:
        from datetime import datetime, timezone
        today_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        row = get_db().get(
            "SELECT COUNT(*) FROM re_signals WHERE created_at >= ? AND correlation_confirmed=1",
            today_start,
        )
        return int(row[0]) if row else 0
    except Exception:
        return 0


def get_correlation_history(days: int = 30) -> list[dict]:
    rows = get_db().all("SELECT * FROM re_correlation ORDER BY date DESC LIMIT ?", days)
    return [dict(r) for r in rows]


# ── Level tracking ────────────────────────────────────────────────────────────

def upsert_level(level_type: str, price: float, direction: str,
                 source: str = "engine", notes: str = "") -> None:
    price = round(price, 2)
    with get_db().transaction():
        existing = get_db().get(
            "SELECT id, strength FROM re_levels WHERE level_type=? AND ABS(price-?)<=1 AND direction=?",
            level_type, price, direction,
        )
        if existing:
            get_db().run(
                "UPDATE re_levels SET strength=strength+1, updated_at=?, price=?, active=1 WHERE id=?",
                time.time(), price, existing[0],
            )
        else:
            get_db().run(
                "INSERT INTO re_levels (updated_at,level_type,price,direction,source,notes) VALUES (?,?,?,?,?,?)",
                time.time(), level_type, price, direction, source, notes,
            )


def get_active_levels() -> list[dict]:
    rows = get_db().all("SELECT * FROM re_levels WHERE active=1 ORDER BY updated_at DESC LIMIT 50")
    return [dict(r) for r in rows]


def deactivate_old_levels(older_than_hours: int = 48) -> None:
    cutoff = time.time() - older_than_hours * 3600
    get_db().run(
        "UPDATE re_levels SET active=0 WHERE updated_at < ? AND source != 'ref'",
        cutoff,
    )


# ── Analysis log ─────────────────────────────────────────────────────────────

def log_analysis(entry: dict) -> None:
    try:
        get_db().run(
            "INSERT INTO re_analysis_log (ts,session,htf_bias,price,atr,adx,levels_json,result,reason) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            entry.get("ts", time.time()),
            entry.get("session"), entry.get("htf_bias"),
            entry.get("price"), entry.get("atr"), entry.get("adx"),
            json.dumps(entry.get("levels", [])),
            entry.get("result"), entry.get("reason"),
        )
    except Exception as exc:
        _log.debug("[RE-Repo] log_analysis error: %s", exc)


def get_analysis_log(limit: int = 50) -> list[dict]:
    rows = get_db().all("SELECT * FROM re_analysis_log ORDER BY ts DESC LIMIT ?", limit)
    result = []
    for r in rows:
        d = dict(r)
        try:
            d["levels"] = json.loads(d.get("levels_json") or "[]")
        except Exception:
            d["levels"] = []
        result.append(d)
    return result


# ── Main-app-DB statements, collected from reversal_engine_live_execute.py
#    and ml_engine.py (M1 SQL sweep). These hit the MAIN database through
#    db_module -- not this engine's own file -- exactly as the callers did.

def claim_vantage_signal_activation(signal_id) -> int:
    """Claim a vantage_signals row for this engine's pending-order path.

    Stamps activated_at with the claim time, and that is not cosmetic:
    signal_state_repo.release_stranded_activations releases any 'activating'
    row whose activated_at is NULL, on the reasoning that a claim with no
    recorded time cannot be one a live process is running. Leaving it NULL
    here meant a claim still in flight was released on the next reconciliation
    pass and the signal opened twice -- the sweep causing the exact failure the
    claim exists to prevent.

    ANSWERED 2026-09-04. This used NOT to enforce max_open_trades, on the
    reasoning that it places a RESTING order rather than opening a position and
    that whether a resting order should consume a trade slot was a money
    decision for the owner. The owner's answer: "whether it is a resting order
    or a market order the EA should manage the max number of allowable trades
    as set within the gui". So the cap applies here too, using the one
    definition in signal_state_repo -- a resting order holds a slot from
    placement until the position it becomes is closed.

    The cap is inside the UPDATE's own WHERE for the same reason the canonical
    claim keeps it there: SQLite serialises writers, so two engines claiming at
    once cannot both read "under the cap" and both place.
    """
    import time as _t
    from backend.src.db import database as db_module
    from backend.src.services.trading.signal_state_repo import (
        _SLOTS_IN_USE_SQL, _NO_EXCLUSION, _max_open_trades,
    )
    with db_module.db() as conn:
        return conn.execute(
            "UPDATE vantage_signals SET status='activating', activated_at=? "
            f"WHERE signal_id=? AND status IN ('pending','active') "
            f"AND ({_SLOTS_IN_USE_SQL}) < ?",
            (_t.time(), signal_id, *_NO_EXCLUSION, _max_open_trades(conn)),
        ).rowcount


def restore_vantage_signal_pending(signal_id) -> None:
    from backend.src.db import database as db_module
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='pending' WHERE signal_id=? AND status='activating'",
            (signal_id,),
        )


def insert_vantage_pending_order(row: tuple) -> None:
    from backend.src.db import database as db_module
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            row,
        )


def fetch_ml_outcome_rows() -> list:
    """Completed, ML-scored RE signals -- the calibration-report corpus."""
    return get_db().all(
        "SELECT id, signal_ref, ml_prob, outcome, rr_tp1, sl_dist, net_pnl_dollars "
        "FROM re_signals "
        "WHERE ml_prob IS NOT NULL AND outcome IS NOT NULL AND outcome != 'open' "
        "ORDER BY id"
    )


def fetch_ref_signal_window(covered_group_ids, cutoff: float):
    """The VIP-reference cross-DB read (M1 SQL sweep): a raw connection to
    the configured db_path, exactly as inline -- see the named-adapter note
    in the refactor plan. Returns (rows_in_window, today_count) or None."""
    db_path = cfg_get("db_path", "")
    if not db_path:
        return None
    con = sqlite3.connect(db_path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        _ph = ",".join("?" for _ in covered_group_ids)
        ref_rows = con.execute(f"""
            SELECT id, group_name, direction, entry_low, entry_high, parsed_at, status
            FROM vantage_tg_signals
            WHERE group_id IN ({_ph})
            AND direction IN ('BUY','SELL')
            AND parsed_at > ?
            ORDER BY parsed_at DESC
            LIMIT 100
        """, (*covered_group_ids, cutoff)).fetchall()
        # True count of real signals received so far *today* (UTC), both
        # channels combined -- distinct from ref_rows above, which is only
        # a 4h rolling window used for matching.
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        ref_today_count = con.execute(f"""
            SELECT COUNT(*) FROM vantage_tg_signals
            WHERE group_id IN ({_ph})
            AND direction IN ('BUY','SELL')
            AND parsed_at >= ?
        """, (*covered_group_ids, day_start)).fetchone()[0]
        return ref_rows, ref_today_count
    finally:
        con.close()


def fetch_ref_cadence(ref_group_id):
    """Last-signal timestamp and today's count for the cadence model.
    Same raw cross-DB connection as fetch_ref_signal_window."""
    db_path = cfg_get("db_path", "")
    if not db_path:
        return None
    con = sqlite3.connect(db_path, timeout=5)
    con.row_factory = sqlite3.Row
    try:
        last_row = con.execute("""
            SELECT parsed_at FROM vantage_tg_signals
            WHERE group_id=? AND direction IN ('BUY','SELL')
            ORDER BY parsed_at DESC LIMIT 1
        """, (ref_group_id,)).fetchone()
        day_start = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).timestamp()
        today_count = con.execute("""
            SELECT COUNT(*) FROM vantage_tg_signals
            WHERE group_id=? AND direction IN ('BUY','SELL')
            AND parsed_at >= ?
        """, (ref_group_id, day_start)).fetchone()[0]
        return (last_row["parsed_at"] if last_row else None), today_count
    finally:
        con.close()


# ── REF confirmation lookup (ref_confirmation) ───────────────────────────────

# Which channels count as confirmation is read from channel_parser_config
# rather than hardcoded (reversal_engine_correlate still pins two group IDs as
# literals, which silently ignores any channel added since). Enabled channels
# only: a channel switched off in Parsing Settings must not be able to
# greenlight a live trade.
_ENABLED_CHANNELS_SQL = (
    "SELECT channel_name FROM channel_parser_config WHERE enabled=1"
)


def find_confirming_signal(
    direction: str, entry_mid: float, since: float, until: float,
    price_delta: float,
) -> Optional[dict]:
    """The most recent Telegram signal from an enabled channel that agrees
    with a Reversal Engine setup, or None.

    Same direction, inside the confirmation window, and priced within
    `price_delta` of the setup's entry mid -- "the same level", not "a nearby
    level". Rows with no entry zone are excluded rather than treated as a
    match at 0.

    Newest first: if a channel posted twice inside the window, the later
    message is the one that reflects what it currently thinks.
    """
    from backend.src.db import database as db_module
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT tg_message_id, group_name, direction, entry_low, entry_high, parsed_at "
            "FROM vantage_tg_signals "
            f"WHERE group_name IN ({_ENABLED_CHANNELS_SQL}) AND direction=? "
            "  AND parsed_at BETWEEN ? AND ? "
            "  AND entry_low IS NOT NULL AND entry_high IS NOT NULL "
            "  AND ABS((entry_low + entry_high) / 2.0 - ?) <= ? "
            "ORDER BY parsed_at DESC LIMIT 1",
            (direction.upper(), since, until, float(entry_mid), price_delta),
        ).fetchone()
    return db_module.row_to_dict(row) if row else None


def fetch_trade_id_and_strategy_for_signal(signal_id: str) -> tuple:
    """(trade_id, strategy) for a signal's trade, or (None, None)."""
    from backend.src.db import database as db_module
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT trade_id, strategy FROM vantage_simulated_trades "
            "WHERE signal_id=?", (signal_id,),
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)



# Sampled-while-open measurements live in measure_repo (2026-09-10, this file
# was at its 800-line ceiling). Re-exported so callers keep one import.
from backend.src.services.reversal_engine.measure_repo import (  # noqa: E402
    record_excursion,
    record_last_seen_sl,
)

# The panel's read-side statistics live in stats_repo (2026-09-11, this file
# was three lines under its ceiling). Re-exported so every caller keeps one
# import, exactly as measure_repo's writers are above.
from backend.src.services.reversal_engine.stats_repo import (  # noqa: E402
    get_max_drawdown,
    get_perf_by_bias,
    get_perf_by_level_type,
    get_perf_by_session,
    get_stats,
)

"""Position-management writes and reads on the shared trades tables.

Collected out of the position handlers (handle_*.py, tp_ladder, tp_tracking,
monitor_loop, safety_net) so no handler carries inline SQL. Verbatim moves:
same statements, same parameters, same single-connection blocks -- the
handlers call these through db_module.to_db_thread exactly where they used to
run their own closures.

Every function here is synchronous on purpose: the callers decide whether it
runs on the DB worker thread (hot loops do) or inline (one-shot sweeps).
Multi-statement writes use transaction() -- database.py's outermost-commit
boundary -- so both rows land or neither does.
"""
from __future__ import annotations

import logging

from backend.src.db import transaction
from backend.src.db.database import db, row_to_dict

log = logging.getLogger(__name__)


# ── vantage_simulated_trades: stop-loss management ────────────────────────────

def fetch_be_flag(trade_id: str):
    """The DB's own sl_moved_to_be, for when the in-memory snapshot may be stale."""
    with db() as conn:
        return conn.execute(
            "SELECT sl_moved_to_be FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()


def set_stop_loss(trade_id: str, new_sl: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
            (new_sl, trade_id),
        )


def set_stop_loss_be(trade_id: str, new_sl: float) -> None:
    """Move SL and latch the breakeven flag in one statement."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
            (new_sl, trade_id),
        )


def set_stop_loss_be_flag(trade_id: str, new_sl: float, sl_moved_be) -> None:
    """tp_ladder's variant: the flag value is computed by the ladder position."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=? WHERE trade_id=?",
            (new_sl, sl_moved_be, trade_id),
        )


def reclaim_management(trade_id: str) -> None:
    """Take a trade back from a dead local EA (monitor_loop's bridge-lost path)."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET managed_by='python' WHERE trade_id=?",
            (trade_id,),
        )


# ── vantage_partial_closes: zero-lot markers for the UI chips ─────────────────

def insert_tp_marker(trade_id: str, ts: float, price: float, reason: str) -> None:
    """A zero-lot, zero-pnl row recording that a TP level was reached/skipped."""
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
            " VALUES (?,?,?,?,?,?)",
            (trade_id, ts, 0.0, price, 0.0, reason),
        )


def lock_sl_with_marker(trade_id: str, new_sl: float, ts: float,
                        price: float, reason: str) -> None:
    """be_runner's step: latch SL at the locked level and record the marker."""
    with transaction() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
            (new_sl, trade_id),
        )
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
            " VALUES (?,?,?,?,?,?)",
            (trade_id, ts, 0.0, price, 0.0, reason),
        )


def mark_trailing_started(trade_id: str, ts: float, price: float) -> None:
    """trail_stop's first activation: latch the flag and record TP1_TRAIL_START.

    Deliberately does NOT touch stop_loss -- the caller writes the SL itself
    on the same cycle through set_stop_loss, exactly as the inline code did.
    """
    with transaction() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET sl_moved_to_be=1 WHERE trade_id=?",
            (trade_id,),
        )
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
            " VALUES (?,?,?,?,?,?)",
            (trade_id, ts, 0.0, price, 0.0, "TP1_TRAIL_START"),
        )


# ── vantage_partial_closes / trades: reads used by TP tracking ────────────────

def fetch_tp_marker_reasons(trade_id: str) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT reason FROM vantage_partial_closes WHERE trade_id=?", (trade_id,)
        ).fetchall()


def fetch_recent_real_close_reasons(trade_id: str) -> list:
    """Reasons of the latest real partial closes (lots > 0), newest first."""
    with db() as conn:
        return conn.execute(
            "SELECT reason FROM vantage_partial_closes "
            "WHERE trade_id=? AND lots_closed > 0 "
            "ORDER BY ts DESC LIMIT 10",
            (trade_id,),
        ).fetchall()


def fetch_remaining_lots(trade_id: str):
    with db() as conn:
        return conn.execute(
            "SELECT remaining_lots FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone()


def fetch_internal_open_exposure(sources: list[str]) -> list[tuple[str, float]]:
    """[(DIRECTION, lots), ...] for every open trade from an internal generator.

    COALESCE(remaining_lots, lot_size), not lot_size: remaining_lots is the
    live exposure, and a partially-closed trade no longer carries its original
    size. Falling back matters for rows written before that column existed.
    """
    placeholders = ",".join("?" for _ in sources)
    with db() as conn:
        rows = conn.execute(
            f"SELECT direction, COALESCE(remaining_lots, lot_size) AS lots "
            f"FROM vantage_simulated_trades "
            f"WHERE status='open' AND tg_source IN ({placeholders})",
            sources,
        ).fetchall()
    return [((r[0] or "").upper(), float(r[1] or 0)) for r in rows]


def fetch_recent_signals_for_groups(groups: list[str], cutoff: float) -> list[dict]:
    """Telegram signals from the watched groups since `cutoff`, oldest first.

    The cutoff is what keeps a snapshot contemporaneous: replaying an old
    signal against today's market would be worse than having no data, since
    the whole value of the reading is that it was taken at the time.
    """
    marks = ",".join("?" for _ in groups)
    with db() as conn:
        return [
            row_to_dict(r) for r in conn.execute(
                f"SELECT * FROM vantage_tg_signals "
                f"WHERE group_name IN ({marks}) AND parsed_at > ? "
                f"ORDER BY parsed_at",
                (*groups, cutoff),
            ).fetchall()
        ]

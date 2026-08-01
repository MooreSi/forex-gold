"""DPM's SQL, collected out of handler.py and bookkeeping.py (M1 SQL sweep).

Verbatim statements; the callers run them from the same places in the same
order they always did. The trade-table statements (BE flag, SL moves, TP
markers) deliberately mirror services/positions/repo.py rather than import
it -- a service's repo is private to that service, and two services managing
the same rows each carry their own copy of these three one-liners.
"""
from __future__ import annotations

import logging

from backend.src.db import transaction
from backend.src.db.database import db, row_to_dict

log = logging.getLogger(__name__)

# The only milestone columns that exist; anything else is silently ignored,
# exactly as bookkeeping.set_dpm_milestone always behaved.
MILESTONE_COLUMNS = ("reached_be", "reached_tp1", "reached_tp2")


# ── vantage_simulated_trades / vantage_partial_closes ────────────────────────

def fetch_be_flag(trade_id: str):
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
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1"
            " WHERE trade_id=?",
            (new_sl, trade_id),
        )


def insert_tp_marker(trade_id: str, ts: float, price: float, reason: str) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes"
            " (trade_id,ts,lots_closed,close_price,pnl,reason)"
            " VALUES (?,?,?,?,?,?)",
            (trade_id, ts, 0.0, price, 0.0, reason),
        )


# ── dpm_trade_performance ─────────────────────────────────────────────────────

def insert_trade_performance(trade: dict, params: dict, opened_at: float) -> None:
    """Snapshot market state and DPM parameters for a newly-managed trade."""
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO dpm_trade_performance
               (trade_id, direction, entry_price, lot_size, original_sl,
                atr_at_entry, session_at_entry, momentum_at_entry, momentum_label,
                regime_at_entry, adx_at_entry,
                be_multiplier_used, trail_multiplier_used,
                be_trigger_used, trail_dist_used, tp1_pct_used,
                used_calibrated, tg_source, opened_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade["trade_id"],
                trade.get("direction"),
                trade.get("entry_price"),
                trade.get("lot_size"),
                trade.get("stop_loss"),   # original SL at entry time
                params["atr"],
                params["session"],
                params["momentum"],
                params["momentum_label"],
                params["regime"],
                params.get("adx"),
                params["be_multiplier"],
                params["trail_multiplier"],
                params["be_trigger_usd"],
                params["trail_distance"],
                params["tp1_partial_pct"],
                1 if params.get("used_calibrated") else 0,
                trade.get("tg_source"),
                opened_at,
            ),
        )


def update_peak_pnl(trade_id: str, unrealized_pnl: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE dpm_trade_performance SET peak_pnl = MAX(peak_pnl, ?) WHERE trade_id=?",
            (unrealized_pnl, trade_id),
        )


def set_milestone(trade_id: str, column: str) -> None:
    if column not in MILESTONE_COLUMNS:
        return
    with db() as conn:
        conn.execute(
            f"UPDATE dpm_trade_performance SET {column}=1 WHERE trade_id=?",
            (trade_id,),
        )


def get_trade_performance(trade_id: str) -> dict:
    with db() as conn:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM dpm_trade_performance WHERE trade_id=?", (trade_id,)
            ).fetchone()
        )


def finalize_trade_performance(trade_id: str, close_price: float, exit_type: str,
                               final_pnl: float, r_multiple: float,
                               hold_minutes: float, closed_at: float) -> None:
    with db() as conn:
        conn.execute(
            """UPDATE dpm_trade_performance
               SET close_price=?, exit_type=?, final_pnl=?, r_multiple=?,
                   hold_minutes=?, closed_at=?
               WHERE trade_id=?""",
            (close_price, exit_type, final_pnl, r_multiple,
             hold_minutes, closed_at, trade_id),
        )


def fetch_closed_trade_performance() -> list[dict]:
    """Every closed, fully-recorded DPM trade -- the calibration corpus."""
    with db() as conn:
        return [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM dpm_trade_performance "
                "WHERE closed_at IS NOT NULL AND final_pnl IS NOT NULL"
            ).fetchall()
        ]


# ── dpm_calibration ───────────────────────────────────────────────────────────

def fetch_latest_calibration() -> list:
    """All rows of the most recent calibration run, in insertion order."""
    with db() as conn:
        return conn.execute(
            "SELECT session, momentum_bucket, be_multiplier, trail_multiplier, "
            "       tp1_partial_pct, sample_size "
            "FROM dpm_calibration "
            "WHERE calibrated_at = (SELECT MAX(calibrated_at) FROM dpm_calibration) "
            "ORDER BY id"
        ).fetchall()


def insert_calibration_rows(results: list[dict]) -> None:
    """One calibration run's grouped results, atomically."""
    with transaction() as conn:
        for r in results:
            conn.execute(
                """INSERT INTO dpm_calibration
                   (calibrated_at, session, momentum_bucket, be_multiplier, trail_multiplier,
                    tp1_partial_pct, sample_size, profit_factor, win_rate, avg_r_multiple, notes)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (r["calibrated_at"], r["session"], r["momentum_bucket"],
                 r["be_multiplier"], r["trail_multiplier"], r["tp1_partial_pct"],
                 r["sample_size"], r["profit_factor"], r["win_rate"],
                 r["avg_r_multiple"], r["notes"]),
            )

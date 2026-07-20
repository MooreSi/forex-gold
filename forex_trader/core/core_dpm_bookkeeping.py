"""DPM (Dynamic Position Management) bookkeeping -- extracted verbatim (no
logic changes) from core/engine.py's SimulationEngine._load_dpm_calibrated/
_record_dpm_entry/_update_dpm_peak/_set_dpm_milestone/_finalize_dpm_record,
as part of the core/engine.py migration series. See
docs/todo/refactor/core-dpm-bookkeeping-migration/020-*.md.

_load_dpm_calibrated and _record_dpm_entry originally read/wrote
self._dpm_calibrated/_dpm_cal_loaded_at/_dpm_recorded -- in-memory state
that isn't derivable from the database (a TTL cache and a per-trade dedup
guard). DPMCache carries that state explicitly instead of hiding it on a
SimulationEngine instance; callers own one DPMCache and pass it into both
functions. update_dpm_peak/set_dpm_milestone/finalize_dpm_record never used
`self` and extract as plain functions, same as the rest of this migration
series.
"""
from __future__ import annotations

import time

from forex_trader.core import database as db_module
from forex_trader.core.models import CONTRACT_SIZE


class DPMCache:
    """Per-engine in-memory state for the two DPM methods that need it."""
    def __init__(self):
        self.calibrated: dict = {}
        self.loaded_at: float = 0.0
        self.recorded: set[str] = set()


def load_dpm_calibrated(cache: DPMCache) -> dict:
    """Load calibrated DPM multipliers from app_config into a flat dict keyed
    "{session}_{momentum_bucket}".  Refreshed at most once per 10 minutes."""
    now = time.time()
    if now - cache.loaded_at < 600:
        return cache.calibrated
    cal: dict = {}
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT session, momentum_bucket, be_multiplier, trail_multiplier, "
            "       tp1_partial_pct, sample_size "
            "FROM dpm_calibration "
            "WHERE calibrated_at = (SELECT MAX(calibrated_at) FROM dpm_calibration) "
            "ORDER BY id"
        ).fetchall()
    for r in rows:
        key = f"{r[0]}_{r[1]}"
        cal[key] = {
            "be_multiplier":   r[2],
            "trail_multiplier": r[3],
            "tp1_partial_pct": r[4],
            "sample_size":     r[5],
        }
    cache.calibrated = cal
    cache.loaded_at  = now
    return cal


def record_dpm_entry(cache: DPMCache, trade: dict, params: dict) -> None:
    """Snapshot market state and DPM parameters on the first cycle for a trade."""
    trade_id = trade["trade_id"]
    if trade_id in cache.recorded:
        return
    cache.recorded.add(trade_id)
    with db_module.db() as conn:
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
                trade_id,
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
                time.time(),
            ),
        )


def update_dpm_peak(trade_id: str, unrealized_pnl: float) -> None:
    """Keep the high-water P&L for this trade so calibration can compute capture ratio."""
    if unrealized_pnl <= 0:
        return
    with db_module.db() as conn:
        conn.execute(
            "UPDATE dpm_trade_performance SET peak_pnl = MAX(peak_pnl, ?) WHERE trade_id=?",
            (unrealized_pnl, trade_id),
        )


def set_dpm_milestone(trade_id: str, column: str) -> None:
    """Mark a milestone column (reached_be, reached_tp1, reached_tp2) as 1."""
    if column not in ("reached_be", "reached_tp1", "reached_tp2"):
        return
    with db_module.db() as conn:
        conn.execute(
            f"UPDATE dpm_trade_performance SET {column}=1 WHERE trade_id=?",
            (trade_id,),
        )


def finalize_dpm_record(trade_id: str, close_price: float,
                        exit_type: str, final_pnl: float) -> None:
    """Write close-time fields to the DPM performance record."""
    with db_module.db() as conn:
        row = db_module.row_to_dict(
            conn.execute(
                "SELECT * FROM dpm_trade_performance WHERE trade_id=?", (trade_id,)
            ).fetchone()
        )
    if not row:
        return

    # R-multiple: final_pnl / initial_risk
    entry_price = float(row.get("entry_price") or 0)
    orig_sl     = float(row.get("original_sl") or 0)
    lot_size    = float(row.get("lot_size")     or 0.01)
    initial_risk = abs(entry_price - orig_sl) * lot_size * CONTRACT_SIZE if orig_sl and entry_price else 0
    r_multiple   = round(final_pnl / initial_risk, 3) if initial_risk > 0 else 0.0

    # Hold duration
    opened_at    = float(row.get("opened_at") or time.time())
    hold_minutes = round((time.time() - opened_at) / 60, 1)

    with db_module.db() as conn:
        conn.execute(
            """UPDATE dpm_trade_performance
               SET close_price=?, exit_type=?, final_pnl=?, r_multiple=?,
                   hold_minutes=?, closed_at=?
               WHERE trade_id=?""",
            (close_price, exit_type, final_pnl, r_multiple,
             hold_minutes, time.time(), trade_id),
        )

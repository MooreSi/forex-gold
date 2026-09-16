"""Excursion rows for the breakout engine: what to measure and what to read.

Split out of `breakout_signal_repo.py` rather than added to it -- that file
was at 806 lines with these in it, over the 800 ceiling, and the Reversal
Engine already keeps exactly this concern in its own `measure_repo.py`. Two
engines, one shape.

The filters are the interesting part and each keeps a different fiction out
of the fit; see `excursion_backfill.py`'s docstring for why.
"""
from __future__ import annotations

from backend.src.services.breakout_signal.breakout_signal_repo import get_db

SOURCE_TICKS = "ticks"
SOURCE_LIVE = "live"


def signals_awaiting_excursion_backfill(limit: int = 500) -> list[dict]:
    """Executed, closed signals with no excursion yet, oldest close first."""
    rows = get_db().all(
        "SELECT id, direction, trigger_price, trigger_time, close_time, sl_dist "
        "FROM bo_signals "
        "WHERE live_exec_status='executed' AND status='closed' "
        "  AND mfe_pts IS NULL "
        "ORDER BY close_time ASC LIMIT ?",
        limit,
    )
    return [dict(r) for r in rows]


def record_backfilled_excursion(sig_id: int, favourable_pts: float,
                                adverse_pts: float,
                                source: str = SOURCE_TICKS) -> None:
    """SET, not MAX. A tick reconstruction is the answer, not another sample.

    The WHERE clause still refuses to touch a row that already has one.
    """
    get_db().run(
        "UPDATE bo_signals SET mfe_pts=?, mae_pts=?, excursion_source=? "
        "WHERE id=? AND mfe_pts IS NULL",
        round(max(0.0, favourable_pts), 2), round(max(0.0, adverse_pts), 2),
        source, sig_id,
    )


def excursion_coverage() -> dict:
    """How many executed signals carry an excursion, by how it was obtained."""
    rows = get_db().all(
        "SELECT COALESCE(excursion_source, ?) AS src, COUNT(*) AS n "
        "FROM bo_signals "
        "WHERE live_exec_status='executed' AND mfe_pts IS NOT NULL "
        "GROUP BY src",
        SOURCE_LIVE,
    )
    return {r["src"]: r["n"] for r in rows}


def excursion_observations(limit: int = 5000) -> list[dict]:
    """The population `market/barrier_fit.fit_barriers` fits on.

    Live-executed only, for the same reason the backfill measures only those.
    """
    rows = get_db().all(
        "SELECT outcome, mfe_pts, mae_pts, atr_m15 AS atr, sl_dist, close_time, "
        "       excursion_source "
        "FROM bo_signals "
        "WHERE live_exec_status='executed' AND status='closed' "
        "  AND mfe_pts IS NOT NULL AND mae_pts IS NOT NULL "
        "ORDER BY close_time DESC LIMIT ?",
        limit,
    )
    return [dict(r) for r in rows]

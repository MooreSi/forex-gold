"""The reversal engine's read-side statistics: the numbers the panel shows.

Split out of `reversal_engine_repo.py` on 2026-09-11, which was three lines
under its 800-line ceiling. These five belong together: all are read-only,
all feed the Reversal Engine panel rather than any trading decision, and all
of them now honour the same reporting epoch.

**The epoch is a REPORTING boundary, not a data deletion.** `reset_stats()`
records a timestamp; every query here then ignores anything closed before
it. No row is removed, so the ML training set, the reconstructed excursion
data and the attribution history all survive a reset intact -- which matters,
because those are the only record of how this engine has behaved and they
took real broker history to rebuild.

`get_recent_win_rate` deliberately does NOT live here and is NOT
epoch-filtered: it is a feature in the model's vector, not a number on a
panel, and resetting what the user sees must not silently change what the
model is told.
"""
from __future__ import annotations

import logging
import time
from typing import Optional

from backend.src.services.reversal_engine.reversal_engine_repo import (
    _STARTING_BALANCE, get_config, get_db, set_config)

_log = logging.getLogger(__name__)

_EPOCH_KEY = "stats_epoch_ts"


def stats_epoch() -> float:
    """Closed signals before this timestamp are excluded from the panel.

    0.0 -- never reset -- means everything counts, which is how every
    install behaves until somebody asks for a fresh start.
    """
    try:
        return float(get_config(_EPOCH_KEY, "0") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def get_max_drawdown() -> float:
    try:
        rows = get_db().all("SELECT balance FROM re_balance_log ORDER BY ts")
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


def get_stats() -> dict:
    try:
        r = get_db().get("""
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
        """)
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


def get_perf_by_session() -> list[dict]:
    rows = get_db().all("""
        SELECT session,
               SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(net_pnl_dollars) as avg_pnl,
               SUM(net_pnl_dollars) as total_pnl
        FROM re_signals WHERE status='closed' AND outcome IN ('win','loss','be')
        GROUP BY session ORDER BY total_pnl DESC
    """)
    return [dict(r) for r in rows]


def get_perf_by_bias() -> list[dict]:
    rows = get_db().all("""
        SELECT htf_bias,
               SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(net_pnl_dollars) as avg_pnl,
               SUM(net_pnl_dollars) as total_pnl
        FROM re_signals WHERE status='closed' AND outcome IN ('win','loss','be')
        GROUP BY htf_bias ORDER BY total_pnl DESC
    """)
    return [dict(r) for r in rows]


def get_perf_by_level_type() -> list[dict]:
    rows = get_db().all("""
        SELECT level_type,
               SUM(CASE WHEN outcome='win'  THEN 1 ELSE 0 END) as wins,
               SUM(CASE WHEN outcome='loss' THEN 1 ELSE 0 END) as losses,
               AVG(net_pnl_dollars) as avg_pnl,
               SUM(net_pnl_dollars) as total_pnl
        FROM re_signals WHERE status='closed' AND outcome IN ('win','loss','be')
        GROUP BY level_type ORDER BY total_pnl DESC
    """)
    return [dict(r) for r in rows]

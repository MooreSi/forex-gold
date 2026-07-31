"""Analytics — split from core/database.py.
Extracted from forex_trader/core/database.py -- see
docs/todo/refactor/core-database-migration/. Verbatim port: same functions,
same SQL, same behavior, using database.py's own db()/to_db_thread()
machinery (unchanged, already correct -- this is a pure file-size split,
not a connection-layer migration). Re-exported from database.py so every
existing `db_module.<name>` call site works completely unchanged.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402
from backend.src.services.cluster.sync_repo import _ensure_sync_tables  # noqa: E402

# ── Performance analytics (heat map + channel scorecard) ──────────────────────

def _session_for_hour(h: int) -> str:
    """Map a UTC hour to a trading session (mirrors dpm_engine.detect_session)."""
    london = 7 <= h < 16
    ny     = 12 <= h < 21
    if london and ny:
        return "overlap"
    if london:
        return "london"
    if ny:
        return "ny"
    return "asian"


def _trade_pts(direction: str, entry: float, close: float) -> float:
    """Signed points gained on a trade (positive = profit)."""
    if not entry or not close:
        return 0.0
    return (close - entry) if (direction or "").upper() == "BUY" else (entry - close)


def get_hourly_pnl_grid(days: int = 90) -> dict:
    """Return {(weekday, hour): {'pnl': total, 'n': count, 'avg': avg}} from closed
    trades over the last `days`, bucketed by UTC weekday (0=Mon) and hour.

    Merges in the consolidated ledger (see consolidated_trades) as a fallback
    for trades the OTHER paired node closed and has no local
    vantage_simulated_trades row for — same cross-node gap/fallback pattern
    already used by History's Trade History tab (get_consolidated_extra_maps).
    Deduped by trade_id since every locally-closed trade already has its own
    row in this node's own local copy of consolidated_trades too."""
    import time as _t
    from datetime import datetime as _dt, timezone as _tz
    _ensure_sync_tables()
    cutoff = _t.time() - days * 86400
    grid: dict[tuple, dict] = {}
    local_ids: set[str] = set()
    with db() as conn:
        rows = conn.execute(
            "SELECT close_time, net_pnl, trade_id FROM vantage_simulated_trades "
            "WHERE status='closed' AND close_time >= ?",
            (cutoff,),
        ).fetchall()
        ledger_rows = conn.execute(
            "SELECT close_time, pnl_dollars, trade_id FROM consolidated_trades "
            "WHERE close_time >= ?", (cutoff,),
        ).fetchall()
    for ct, pnl, tid in rows:
        local_ids.add(tid)
        if not ct:
            continue
        d = _dt.fromtimestamp(float(ct), tz=_tz.utc)
        key = (d.weekday(), d.hour)
        cell = grid.setdefault(key, {"pnl": 0.0, "n": 0})
        cell["pnl"] += float(pnl or 0)
        cell["n"]   += 1
    for ct, pnl, tid in ledger_rows:
        if tid in local_ids or not ct:
            continue
        d = _dt.fromtimestamp(float(ct), tz=_tz.utc)
        key = (d.weekday(), d.hour)
        cell = grid.setdefault(key, {"pnl": 0.0, "n": 0})
        cell["pnl"] += float(pnl or 0)
        cell["n"]   += 1
    for cell in grid.values():
        cell["avg"] = round(cell["pnl"] / cell["n"], 2) if cell["n"] else 0.0
    return grid


# ── Equity curve helpers ──────────────────────────────────────────────────────

def get_equity_drawdown_pct() -> float:
    """
    Current drawdown from peak equity as a fraction [0, 1].
    Uses the simulation account balance and peak_balance if stored, or
    derives it from the recent trade P&L history.
    Returns 0.0 if equity data is unavailable.
    """
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT balance, peak_balance FROM vantage_simulation_account WHERE id=1"
            ).fetchone()
            if row and row["peak_balance"] and float(row["peak_balance"]) > 0:
                peak = float(row["peak_balance"])
                bal  = float(row["balance"])
                dd   = max(0.0, (peak - bal) / peak)
                return round(min(dd, 1.0), 4)
            # Fallback: derive from last 50 closed trades
            rows = conn.execute(
                "SELECT SUM(profit) as cumulative FROM ("
                "  SELECT profit FROM vantage_simulated_trades "
                "  WHERE status='closed' ORDER BY close_time DESC LIMIT 50"
                ")"
            ).fetchone()
            if rows and rows[0]:
                recent_pnl = float(rows[0])
                if recent_pnl < 0:
                    return round(min(abs(recent_pnl) / 1000.0, 1.0), 4)
    except Exception:
        pass
    return 0.0


# ── Regime detection helper ───────────────────────────────────────────────────

def get_regime_score(adx: float, atr: float, atr_avg: float = 0.0) -> float:
    """
    Classify current market regime as a normalised score [0, 1]:
      1.0  strongly trending   (ADX ≥ 40)
      0.75 trending            (ADX 28-40)
      0.5  mixed/volatile      (ADX 20-28 or ATR >> average)
      0.25 weakly ranging      (ADX 14-20)
      0.0  ranging/choppy      (ADX < 14)
    ATR expansion (atr / atr_avg > 1.5) promotes to volatile regardless of ADX.
    """
    if adx >= 40:
        regime = 1.0
    elif adx >= 28:
        regime = 0.75
    elif adx >= 20:
        regime = 0.5
    elif adx >= 14:
        regime = 0.25
    else:
        regime = 0.0

    # ATR spike → volatile regime
    if atr_avg > 0 and atr / atr_avg > 1.5:
        regime = max(regime, 0.5)

    return regime


# ── AI research inputs ────────────────────────────────────────────────────────
# Moved from frontend/pages/ai_summary.py. All three feed the AI market-research
# prompt; none is on any trading path.

def custom_strategy_blurb(strategy_id: str):
    """First line of a custom strategy's description, or None.

    Built-in strategies have hardcoded blurbs in the view; only user-defined
    ones live in the DB.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT name, description FROM custom_strategies WHERE id=?",
            (strategy_id,),
        ).fetchone()
    if not row:
        return None
    return (row[1] or "").split("\n")[0].strip().lstrip("#").strip()


def recent_tg_signals(cutoff: float) -> list[dict]:
    """Parsed Telegram signals since `cutoff`, newest first."""
    with db() as conn:
        return [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_tg_signals WHERE parsed_at>? "
                "ORDER BY parsed_at DESC",
                (cutoff,),
            ).fetchall()
        ]


def all_custom_strategies() -> list[dict]:
    """Every user-defined strategy, oldest first (creation order)."""
    with db() as conn:
        return [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT id, name, description FROM custom_strategies ORDER BY created_at"
            ).fetchall()
        ]

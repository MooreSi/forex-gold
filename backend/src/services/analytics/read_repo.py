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

from backend.src.db.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402
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


def fetch_signal_execution_lags(db_path: str) -> list:
    """Telegram->execution latency samples for the diagnostics panel (M3
    page drain): seconds between a signal's parsed_at and its trade's
    open_time, newest 200, by explicit env-DB path -- same fresh connection
    the Settings page always made."""
    import sqlite3 as _sqlite3
    _conn = _sqlite3.connect(db_path, check_same_thread=False)
    _conn.row_factory = _sqlite3.Row
    try:
        return _conn.execute("""
            SELECT
                (st.open_time - ts.parsed_at) AS lag_s,
                ts.parsed_at
            FROM vantage_tg_signals ts
            JOIN vantage_simulated_trades st ON ts.signal_id = st.signal_id
            WHERE st.status IN ('open','closed')
              AND ts.parsed_at IS NOT NULL
              AND st.open_time IS NOT NULL
              AND (st.open_time - ts.parsed_at) BETWEEN 0 AND 300
            ORDER BY ts.parsed_at DESC
            LIMIT 200
        """).fetchall()
    finally:
        _conn.close()


def fetch_realised_pnl_last_24h(cutoff: float) -> float:
    """Sum of closed-trade P&L since cutoff (prefers the real MT5 figure) --
    the History-tab celebration check (M3 app-shell drain)."""
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(COALESCE(mt5_profit, net_pnl, 0)), 0) "
            "FROM vantage_simulated_trades "
            "WHERE status='closed' AND close_time > ?",
            (cutoff,),
        ).fetchone()
    return float(row[0] or 0.0)


def realised_pnl_for_source(source: str) -> dict:
    """Closed-trade count, total P&L and per-trade average for one channel.

    Reads vantage_simulated_trades on purpose. An engine's own database records
    every signal it produced and prices them all at the virtual lot, whether or
    not the trade was ever placed; only rows here correspond to orders that
    really went to MT5.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT COUNT(*) n, COALESCE(SUM(net_pnl), 0) total "
            "FROM vantage_simulated_trades "
            "WHERE status='closed' AND tg_source=?",
            (source,),
        ).fetchone()
    n = int(row[0] or 0)
    total = float(row[1] or 0.0)
    return {"n": n, "total": total, "per_trade": (total / n) if n else 0.0}


def closed_pnls_since(close_time_from: float) -> list[float]:
    """Every closed trade's net P&L since a timestamp, oldest first.

    Keyed on CLOSE time, so a trade opened yesterday and closed today counts.
    The risk governor replays these in order to rebuild the day's running total
    and its high-water mark -- see day_pnl_and_peak, which deliberately
    recomputes rather than persisting a counter that can drift.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT net_pnl FROM vantage_simulated_trades "
            "WHERE close_time >= ? ORDER BY close_time", (close_time_from,),
        ).fetchall()
    return [float(r[0] or 0.0) for r in rows]


def _rung_index(max_tp_hit) -> int:
    """0 for "never reached a TP", else the rung number from a "TP<n>" value.

    The column carries 'none', 'n/a', '' and NULL for the same outcome (all
    four are present in the live database), so every non-"TP<n>" value means
    no rung. Reading any of them as a rung would overstate ladder depth, which
    is the one number this aggregate exists to make trustworthy.
    """
    s = str(max_tp_hit or "").strip().upper()
    if not s.startswith("TP"):
        return 0
    try:
        return int(s[2:])
    except ValueError:
        return 0


def get_strategy_ladder_reach(days: int = 30, min_n: int = 5) -> dict[str, dict]:
    """How far up its TP ladder each strategy's trades ACTUALLY get.

    A template's configured ladder says what is reachable in principle; this
    says what was reached in practice, which is a different quantity whenever
    a trailing stop is armed inside the ladder. "GD VIP - Single" is the
    worked example (2026-09-04): eight rungs at 20/40/60/80/100/120/170/270
    pips, a trail armed at 40 pips (TP2) with a 50-pip distance, and 50 pips
    is around 42% of a typical H1 range on XAUUSD. Over 30 days, 50 of its 85
    closed trades topped out at TP1 or TP2, 3 reached TP5 or beyond, and one
    completed the ladder. The AI Analysis prompt recommended it for "letting
    profits run up the ladder" -- reasoning from the configuration, because
    the configuration was all it was given.

    `stopped_after_tp` is the truncation signature: a rung was banked and the
    trade then exited on a stop. Those are WINS, so win rate cannot show it.

    Returns {strategy: {n, win_rate, net_pnl, no_tp, tp1_2, tp3_4, tp5_plus,
    stopped_after_tp, top_rung}}, strategies under `min_n` trades dropped.
    Never raises: this feeds a prompt, and a failure must cost the evidence,
    not the analysis.
    """
    import time as _t
    cutoff = _t.time() - days * 86400
    acc: dict[str, dict] = {}
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT strategy, max_tp_hit, exit_reason, net_pnl "
                "FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time >= ? AND strategy IS NOT NULL",
                (cutoff,),
            ).fetchall()
    except Exception as exc:
        log.warning("strategy ladder reach unavailable: %s", exc)
        return {}

    for strategy, max_tp_hit, exit_reason, net_pnl in rows:
        r = acc.setdefault(str(strategy), {
            "n": 0, "wins": 0, "net_pnl": 0.0, "no_tp": 0, "tp1_2": 0,
            "tp3_4": 0, "tp5_plus": 0, "stopped_after_tp": 0, "top_rung": 0,
        })
        pnl  = float(net_pnl or 0.0)
        rung = _rung_index(max_tp_hit)
        r["n"] += 1
        r["net_pnl"] += pnl
        if pnl > 0:
            r["wins"] += 1
        if rung <= 0:
            r["no_tp"] += 1
        else:
            r["top_rung"] = max(r["top_rung"], rung)
            if rung <= 2:
                r["tp1_2"] += 1
            elif rung <= 4:
                r["tp3_4"] += 1
            else:
                r["tp5_plus"] += 1
            # Only a banked rung can be truncated -- a stop with no rung
            # behind it is an ordinary loser, not a ladder cut short.
            if str(exit_reason or "").upper() == "SL":
                r["stopped_after_tp"] += 1

    out: dict[str, dict] = {}
    for strategy, r in acc.items():
        if r["n"] < min_n:
            continue
        out[strategy] = {
            "n":                r["n"],
            "win_rate":         round(100.0 * r["wins"] / r["n"], 1),
            "net_pnl":          round(r["net_pnl"], 2),
            "no_tp":            r["no_tp"],
            "tp1_2":            r["tp1_2"],
            "tp3_4":            r["tp3_4"],
            "tp5_plus":         r["tp5_plus"],
            "stopped_after_tp": r["stopped_after_tp"],
            "top_rung":         r["top_rung"],
        }
    return out


def realised_pnl_opened_since(open_time_from: float) -> float:
    """Total net P&L of CLOSED trades OPENED since a timestamp.

    Keyed on OPEN time, unlike closed_pnls_since above -- the trading
    schedule's daily target counts a day's own trades, so one opened yesterday
    and closed today belongs to yesterday. The two predicates look
    interchangeable and are not.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM vantage_simulated_trades "
            "WHERE status='closed' AND open_time >= ?", (open_time_from,),
        ).fetchone()
    return float(row[0] or 0.0)

"""Per-engine edge statistics, computed live from each engine's own database.

Moved verbatim from frontend/pages/edge_dashboard.py -- the page keeps only the
widgets. Everything here is read-only by construction (SQLite mode=ro URIs), and
a missing engine database returns empty results rather than raising, because the
engines are optional installs.

The registry maps a display label to (db file, table, pnl expression, time
column). The pnl expression must be a closed-trade net-dollar value; the WHERE
clause keying on outcome IN ('win','loss','be') is what keeps open trades out of
the profit factor.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path

log = logging.getLogger(__name__)

# Engine registry: label → (db filename, table, pnl expression, time column)
# pnl expression must be a closed-trade net-dollar value.
ENGINES: dict[str, dict] = {
    "Breakout": {
        "db": "breakout_signal.db", "table": "bo_signals",
        "pnl": "net_pnl_dollars", "tcol": "COALESCE(trigger_time, close_time)",
    },
    "Bounce": {
        "db": "test_signal.db", "table": "test_signals",
        "pnl": "pnl_dollars", "tcol": "COALESCE(trigger_time, close_time)",
    },
    "Reversal Engine": {
        "db": "reversal_engine.db", "table": "re_signals",
        "pnl": "net_pnl_dollars", "tcol": "COALESCE(trigger_time, close_time)",
    },
}

def _data_dir() -> Path:
    from backend.src.config import DATA_DIR
    return Path(DATA_DIR)


def query(db_file: str, sql: str, params: tuple = ()) -> list[tuple]:
    """Read-only query against an engine DB; missing DB returns []."""
    path = _data_dir() / db_file
    if not path.exists():
        return []
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0) as conn:
            return conn.execute(sql, params).fetchall()
    except Exception as e:
        log.debug("[Edge] query failed on %s: %s", db_file, e)
        return []


def engine_stats(cfg: dict, days: int) -> dict:
    cutoff = time.time() - days * 86400 if days > 0 else 0
    rows = query(cfg["db"], f"""
        SELECT {cfg['pnl']} FROM {cfg['table']}
        WHERE outcome IN ('win','loss','be') AND {cfg['pnl']} IS NOT NULL
          AND COALESCE(close_time, 0) >= ?
    """, (cutoff,))
    pnls = [float(r[0]) for r in rows]
    n = len(pnls)
    if n == 0:
        return {"n": 0}
    wins    = [p for p in pnls if p > 0]
    losses  = [p for p in pnls if p < 0]
    gross_w = sum(wins)
    gross_l = -sum(losses)
    return {
        "n":          n,
        "net":        sum(pnls),
        "pf":         (gross_w / gross_l) if gross_l > 0 else float("inf"),
        "win_rate":   len(wins) / n,
        "expectancy": sum(pnls) / n,
        "avg_win":    (gross_w / len(wins)) if wins else 0.0,
        "avg_loss":   (gross_l / len(losses)) if losses else 0.0,
    }


def heatmap_data(cfg: dict, days: int) -> list[list]:
    """Return [[hour, weekday_idx, net_pnl], ...] for closed trades."""
    cutoff = time.time() - days * 86400 if days > 0 else 0
    rows = query(cfg["db"], f"""
        SELECT CAST(strftime('%H', {cfg['tcol']}, 'unixepoch') AS INT)  AS h,
               CAST(strftime('%w', {cfg['tcol']}, 'unixepoch') AS INT)  AS dow,
               ROUND(SUM({cfg['pnl']}), 2)
        FROM {cfg['table']}
        WHERE outcome IN ('win','loss','be') AND {cfg['pnl']} IS NOT NULL
          AND COALESCE(close_time, 0) >= ?
        GROUP BY h, dow
    """, (cutoff,))
    # SQLite %w: 0=Sunday … 6=Saturday → remap to Mon=0 … Sun=6
    return [[int(h), (int(dow) + 6) % 7, float(v or 0)] for h, dow, v in rows]


def hour_totals(cfg: dict, days: int) -> dict[int, float]:
    out: dict[int, float] = {}
    for h, _dow, v in heatmap_data(cfg, days):
        out[h] = out.get(h, 0.0) + v
    return out



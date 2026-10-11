"""SQL for the volatility regime. docs/todo/016.

The reference series is `re_analysis_log.atr`: the engine writes one row per
cycle, so it is a dense local record of M15 ATR going back to 2026-07-23 on
the owner's Mac (72,953 rows, about 99% of training signals covered).
Nothing here calls the broker.
"""
from __future__ import annotations

from typing import Iterable, Optional

from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def atr_series(since_ts: float, until_ts: float) -> list[tuple[float, float]]:
    """(ts, atr) for every logged cycle in [since_ts, until_ts), oldest first."""
    rows = get_db().all(
        "SELECT ts, atr FROM re_analysis_log "
        "WHERE ts >= ? AND ts < ? AND atr > 0 ORDER BY ts",
        since_ts, until_ts)
    return [(float(r["ts"]), float(r["atr"])) for r in rows]


def latest_atr(since_ts: float) -> Optional[float]:
    """The most recent logged ATR at or after `since_ts`, or None."""
    row = get_db().get(
        "SELECT atr FROM re_analysis_log WHERE ts >= ? AND atr > 0 "
        "ORDER BY ts DESC LIMIT 1", since_ts)
    return float(row["atr"]) if row else None


def signals_missing_ratio(limit: int) -> list[dict]:
    """Up to `limit` signals whose `atr_ratio` is unmeasured, oldest first.
    A measured row always holds a number (0.0 = no reference available), so
    this cannot re-select a row the sweep already wrote."""
    rows = get_db().all(
        "SELECT id, created_at, atr FROM re_signals "
        "WHERE atr_ratio IS NULL AND created_at IS NOT NULL ORDER BY created_at "
        "LIMIT ?", limit)
    return [dict(r) for r in rows]


def store_ratios(pairs: Iterable[tuple[int, float]]) -> None:
    for sid, ratio in pairs:
        get_db().run("UPDATE re_signals SET atr_ratio=? WHERE id=?", ratio, sid)

"""Reads for the brain view. SELECT only -- nothing here writes.

Three logs the app already keeps: the Telegram decision log and the Reversal
engine's signals (both in reversal_engine.db) and the Breakout engine's
signals (breakout_signal.db). An engine row with no live_exec_status has not
been decided yet and is not an event.
"""
from __future__ import annotations

from backend.src.services.breakout_signal import breakout_signal_repo as bo_db
from backend.src.services.reversal_engine import reversal_engine_repo as re_db


def recent_telegram(limit: int) -> list[dict]:
    rows = re_db.get_db().all(
        "SELECT id, decided_at AS ts, channel_name AS source, direction, "
        "executed, skip_reason AS reason FROM tg_decisions "
        "ORDER BY decided_at DESC LIMIT ?", limit,
    )
    return [dict(r) for r in rows]


def recent_reversal(limit: int) -> list[dict]:
    rows = re_db.get_db().all(
        "SELECT id, COALESCE(trigger_time, created_at) AS ts, direction, "
        "live_exec_status AS status FROM re_signals "
        "WHERE live_exec_status IS NOT NULL "
        "ORDER BY COALESCE(trigger_time, created_at) DESC LIMIT ?", limit,
    )
    return [dict(r) for r in rows]


def recent_breakout(limit: int) -> list[dict]:
    rows = bo_db.get_db().all(
        "SELECT id, COALESCE(trigger_time, created_at) AS ts, direction, "
        "live_exec_status AS status FROM bo_signals "
        "WHERE live_exec_status IS NOT NULL "
        "ORDER BY COALESCE(trigger_time, created_at) DESC LIMIT ?", limit,
    )
    return [dict(r) for r in rows]

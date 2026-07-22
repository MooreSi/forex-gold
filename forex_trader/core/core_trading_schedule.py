"""Trading Schedule: a per-day, per-window profit-target discipline gate for
AUTOMATED order execution only.

Purpose: cap over-trading by blocking new automated entries once a
configurable profit target has been hit within a specific time-of-day
window, resuming at the start of the next window. Also blocks entries
entirely outside every enabled window for the current day. Signal
generation and Telegram ingestion are never affected -- this only gates
the final "place an order" step, and only on the automated path.

Wired in from core_signal_resolution.py's resolve_open_trade_params(), the
same place is_session_allowed() is checked -- that function is reachable
only from the automated open_trade_from_signal() path. core_manual_market_order.py
never calls resolve_open_trade_params(), so manual orders are exempt by
construction, with no special-casing needed here or in open_trade() itself.

Storage: app_config keys "trading_schedule_enabled" (plain "1"/"0") and
"trading_schedule" (JSON), same pattern as trading.py's hidden_strategies.

Profit-per-window is computed on demand -- SUM(net_pnl) of closed trades
whose open_time falls within today's window -- rather than maintaining a
separate running counter, so it can't drift out of sync with the real
trade history and needs no reset-at-midnight bookkeeping.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from forex_trader.core.database import db
from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]
BLOCKS_PER_DAY = 3


def _default_block() -> dict:
    return {"enabled": False, "start": "00:00", "end": "23:59", "target": 0.0}


def _default_schedule() -> dict:
    return {day: [_default_block() for _ in range(BLOCKS_PER_DAY)] for day in DAY_NAMES}


def get_trading_schedule() -> dict:
    """Return the full 7-day x 3-block schedule, filling in defaults for any
    missing/malformed day so callers never need to guard against KeyError."""
    raw = db_module.get_app_config("trading_schedule")
    schedule = _default_schedule()
    if not raw:
        return schedule
    try:
        stored = json.loads(raw)
    except Exception:
        return schedule
    for day in DAY_NAMES:
        blocks = stored.get(day)
        if not isinstance(blocks, list) or len(blocks) != BLOCKS_PER_DAY:
            continue
        merged = []
        for b in blocks:
            block = _default_block()
            if isinstance(b, dict):
                block.update({
                    "enabled": bool(b.get("enabled", False)),
                    "start":   str(b.get("start", "00:00")),
                    "end":     str(b.get("end", "23:59")),
                    "target":  float(b.get("target", 0) or 0),
                })
            merged.append(block)
        schedule[day] = merged
    return schedule


def set_trading_schedule(schedule: dict) -> None:
    db_module.set_app_config("trading_schedule", json.dumps(schedule))


def is_trading_schedule_enabled() -> bool:
    return db_module.get_app_config("trading_schedule_enabled") == "1"


def set_trading_schedule_enabled(enabled: bool) -> None:
    db_module.set_app_config("trading_schedule_enabled", "1" if enabled else "0")


def _parse_hm(hhmm: str) -> int:
    """'HH:MM' -> minutes since midnight."""
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _find_active_block(schedule: dict, now: datetime) -> tuple[Optional[int], Optional[dict]]:
    """Return (block_index, block) for the enabled block covering `now`'s
    time-of-day today, or (None, None) if outside every enabled block."""
    day_blocks = schedule.get(DAY_NAMES[now.weekday()], [])
    cur_min = now.hour * 60 + now.minute
    for i, block in enumerate(day_blocks):
        if not block.get("enabled"):
            continue
        try:
            start_min = _parse_hm(block["start"])
            end_min   = _parse_hm(block["end"])
        except Exception:
            continue
        if start_min <= cur_min < end_min:
            return i, block
    return None, None


def _block_realized_pnl(block: dict, now: datetime) -> float:
    """Sum net_pnl of closed trades opened within today's occurrence of this
    block's [start, end) window."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = day_start.timestamp() + _parse_hm(block["start"]) * 60
    window_end   = day_start.timestamp() + _parse_hm(block["end"]) * 60
    with db() as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(net_pnl), 0) FROM vantage_simulated_trades "
            "WHERE status='closed' AND open_time >= ? AND open_time < ?",
            (window_start, window_end),
        ).fetchone()
    return float(row[0] or 0.0)


def check_trading_schedule(now: Optional[datetime] = None) -> tuple[bool, str]:
    """Return (allowed, reason). `now` is injectable for tests; defaults to
    local wall-clock time, matching the plain HH:MM inputs in the UI."""
    if not is_trading_schedule_enabled():
        return True, ""
    now = now or datetime.now()
    schedule = get_trading_schedule()
    idx, block = _find_active_block(schedule, now)
    if block is None:
        return False, f"outside today's trading schedule ({DAY_NAMES[now.weekday()].title()})"
    target = float(block.get("target", 0) or 0)
    if target > 0:
        pnl = _block_realized_pnl(block, now)
        if pnl >= target:
            return False, (
                f"profit target reached for this window (${pnl:.2f} of ${target:.2f}) "
                "-- resumes at the next scheduled window"
            )
    return True, ""

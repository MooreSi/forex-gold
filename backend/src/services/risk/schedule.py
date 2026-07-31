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

Per-source toggles (2026-07-24): each of the 7x3 windows also independently
gates Telegram / Reversal Engine / Breakout Engine (SOURCE_KEYS) -- see
check_trading_schedule()'s `source` parameter. Reversal Engine performs well
overnight (Asia) but loses during London/NY, the opposite of the Telegram
channels, so a single blanket automated-order switch isn't enough; each
engine's own live-execution path (reversal_engine_live_execute.py,
breakout_signal_live_execute.py) now calls this with its own source key,
alongside the four pre-existing Telegram call sites.

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

from forex_trader.core.database import db, _schedule_coro
from forex_trader.core import database as db_module

log = logging.getLogger(__name__)

DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]
BLOCKS_PER_DAY = 3


SOURCE_KEYS = ("telegram", "reversal_engine", "breakout_engine")
_SOURCE_LABELS = {
    "telegram": "Telegram", "reversal_engine": "Reversal Engine", "breakout_engine": "Breakout Engine",
}


def _default_block() -> dict:
    # Per-source toggles (2026-07-24) default True -- a schedule saved before
    # this feature existed must keep allowing every source exactly as before,
    # not suddenly block Reversal Engine/Breakout Engine because a new field
    # is missing.
    return {
        "enabled": False, "start": "00:00", "end": "23:59", "target": 0.0,
        **{k: True for k in SOURCE_KEYS},
    }


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
                    **{k: bool(b.get(k, True)) for k in SOURCE_KEYS},
                })
            merged.append(block)
        schedule[day] = merged
    return schedule


def set_trading_schedule(schedule: dict, _from_sync: bool = False) -> None:
    db_module.set_app_config("trading_schedule", json.dumps(schedule))
    _maybe_forward_trading_schedule(_from_sync)


def is_trading_schedule_enabled() -> bool:
    return db_module.get_app_config("trading_schedule_enabled") == "1"


def set_trading_schedule_enabled(enabled: bool, _from_sync: bool = False) -> None:
    db_module.set_app_config("trading_schedule_enabled", "1" if enabled else "0")
    _maybe_forward_trading_schedule(_from_sync)


_applying_sync_trading_schedule = False  # re-entrancy guard — see set_trading_schedule/set_trading_schedule_enabled


def _maybe_forward_trading_schedule(_from_sync: bool) -> None:
    """Forward this node's current combined schedule snapshot to the paired
    Local/Remote node, unless this call is itself applying a value that just
    arrived over sync (_from_sync=True) or a forward is already in flight —
    without this guard, mirroring an incoming sync snapshot would immediately
    re-forward it back out, an infinite propose/confirm ping-pong between
    the two nodes. Same pattern as update_risk_settings()."""
    global _applying_sync_trading_schedule
    if _from_sync or _applying_sync_trading_schedule:
        return
    _applying_sync_trading_schedule = True
    try:
        _forward_trading_schedule_over_sync()
    finally:
        _applying_sync_trading_schedule = False


def trading_schedule_snapshot() -> dict:
    """Combined {enabled, schedule} snapshot -- the unit sent/received over
    the Local/Remote sync channel, since the UI edits and saves both pieces
    as one atomic action."""
    return {"enabled": is_trading_schedule_enabled(), "schedule": get_trading_schedule()}


def apply_trading_schedule_snapshot(snapshot: dict) -> None:
    """Apply a combined snapshot that just arrived over sync from the paired
    node -- always applies with _from_sync=True, since receiving IS the sync
    path (forwarding it again would ping-pong back to the sender)."""
    if snapshot.get("schedule"):
        set_trading_schedule(snapshot["schedule"], _from_sync=True)
    if "enabled" in snapshot:
        set_trading_schedule_enabled(bool(snapshot["enabled"]), _from_sync=True)


def _forward_trading_schedule_over_sync() -> None:
    """Send this node's current trading schedule snapshot to the paired
    node, whichever role this process has. No-op (and near-zero cost) if
    sync isn't configured -- both get_instance() calls return None until
    sync.server.init()/sync.client.get_instance() have actually been used.
    Mirrors core_db_risk_settings._forward_settings_over_sync() exactly."""
    try:
        from forex_trader.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None:
            _schedule_coro(cli.propose_trading_schedule(trading_schedule_snapshot()))
            return
    except Exception as e:
        log.debug("[Sync] trading schedule forward (client) failed: %s", e)

    try:
        from forex_trader.sync import server as _sync_srv_mod
        srv = _sync_srv_mod.get_instance()
        if srv is not None:
            _schedule_coro(srv.broadcast_trading_schedule())
    except Exception as e:
        log.debug("[Sync] trading schedule forward (server) failed: %s", e)


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


def check_trading_schedule(
    now: Optional[datetime] = None, source: str = "telegram",
) -> tuple[bool, str]:
    """Return (allowed, reason). `now` is injectable for tests; defaults to
    local wall-clock time, matching the plain HH:MM inputs in the UI.

    `source` (2026-07-24) is one of SOURCE_KEYS -- Reversal Engine performs
    well overnight (Asia) but loses during London/NY, while Telegram signals
    are the opposite, so each of the 7x3 windows independently gates each
    source rather than one blanket automated-order switch. Defaults to
    "telegram" for the four pre-existing call sites (core_signal_resolution.py,
    ea_bridge.py x2, core_instant_entry.py), all of which are Telegram-signal
    paths."""
    if not is_trading_schedule_enabled():
        return True, ""
    now = now or datetime.now()
    schedule = get_trading_schedule()
    idx, block = _find_active_block(schedule, now)
    if block is None:
        return False, f"outside today's trading schedule ({DAY_NAMES[now.weekday()].title()})"
    if not block.get(source, True):
        label = _SOURCE_LABELS.get(source, source)
        return False, f"{label} disabled for this window (Trading > Schedule)"
    target = float(block.get("target", 0) or 0)
    if target > 0:
        pnl = _block_realized_pnl(block, now)
        if pnl >= target:
            return False, (
                f"profit target reached for this window (${pnl:.2f} of ${target:.2f}) "
                "-- resumes at the next scheduled window"
            )
    return True, ""

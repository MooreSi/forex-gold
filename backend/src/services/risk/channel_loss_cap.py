"""Per-channel daily loss cap: one bad signal provider cannot spend the day.

Once a Telegram channel's realised P&L for today reaches minus its cap, new
automated entries from THAT channel are refused until the day turns over.
Every other channel keeps trading. Open positions are left alone: this is an
entry gate, not a liquidation, and it never reaches the close path.

Where it is enforced. `schedule.check_trading_schedule` calls `check()` first
thing, ahead of the schedule's own master switch. Every automated route to
the broker that carries a channel name already calls that function with it
(signal resolution, IME, scan auto-execute, pending activation, resting
revalidation, both EA event paths), so one call covers them all. A check
repeated per route is how a route gets missed (20-trading-safety.md, "Gate
the funnel"). Manual orders never reach it, the same way they never reach the
schedule.

What counts. Net P&L of broker trades (non-null mt5_ticket) CLOSED since
midnight on the trading clock, grouped by canonical channel name. Close time,
not open time: a loss taken today was lost today. Net, not gross: a channel
that is up on the day has not lost anything yet. The engines (Reversal,
Breakout, Bounce, ORB) are not channels and have their own loss controls.

Settings. `channel_daily_loss_cap` is the cap every channel gets; 0 is off,
and is the default, so an install that never sets it trades as before.
`channel_daily_loss_caps` holds per-channel overrides as JSON, keyed by
canonical name; an override of 0 exempts that channel. Both ride the trading
schedule's sync snapshot to the paired node.

Failure direction. A setting that cannot be read is off -- the same answer as
never having set it. An armed cap that cannot read today's trades refuses:
it has not been told the channel is fine (risk domain: refuse-to-trade).
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.risk import clock as _clock
from backend.src.services.risk import repo as risk_repo

log = logging.getLogger(__name__)

DEFAULT_KEY = "channel_daily_loss_cap"
OVERRIDES_KEY = "channel_daily_loss_caps"

# Keys the schedule gate is called with that name no channel.
_NOT_A_CHANNEL = {"", "telegram", "reversal_engine", "breakout_engine", "trend_pa_engine"}


def _canonical(source: str) -> str:
    from backend.src.services.channels.repo import canonical_channel_name
    return canonical_channel_name(source or "")


def _is_channel(canonical: str) -> bool:
    from backend.src.services.channels.performance import internal_engine_names
    return canonical not in _NOT_A_CHANNEL and canonical not in internal_engine_names()


def _channel_names() -> list[str]:
    from backend.src.services.channels.performance import get_telegram_channel_names
    return get_telegram_channel_names()


def _valid(value, what: str) -> float:
    try:
        v = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{what} must be a number.") from None
    if not math.isfinite(v) or v < 0:
        raise ValueError(f"{what} must be 0 (off) or a positive dollar amount.")
    return v


# ── Settings ────────────────────────────────────────────────────────────────

def get_default_cap() -> float:
    try:
        v = float(db_module.get_app_config(DEFAULT_KEY) or 0)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) and v > 0 else 0.0


def get_overrides() -> dict[str, float]:
    try:
        raw = json.loads(db_module.get_app_config(OVERRIDES_KEY) or "{}")
    except (TypeError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, float] = {}
    for name, value in raw.items():
        try:
            v = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(v) and v >= 0:
            out[str(name)] = v
    return out


def set_caps(default_cap: float, overrides: dict, _from_sync: bool = False) -> None:
    """Store both settings, or neither: everything is validated first."""
    default = _valid(default_cap, "The daily loss cap")
    clean: dict[str, float] = {}
    for name, value in (overrides or {}).items():
        canonical = _canonical(str(name)).strip()
        if not canonical:
            continue
        clean[canonical] = _valid(value, f"The cap for {canonical}")
    db_module.set_app_config(DEFAULT_KEY, str(default))
    db_module.set_app_config(OVERRIDES_KEY, json.dumps(clean, sort_keys=True))
    from backend.src.services.risk import schedule as _schedule
    _schedule._maybe_forward_trading_schedule(_from_sync)


def cap_for(channel: str) -> float:
    """The cap that applies to a canonical channel name. 0 means no cap."""
    overrides = get_overrides()
    if channel in overrides:
        return overrides[channel]
    return get_default_cap()


# ── Today's P&L ─────────────────────────────────────────────────────────────

def _day_start(now: datetime) -> float:
    return now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def day_pnl_by_channel(now: Optional[datetime] = None) -> dict[str, float]:
    """Net realised P&L per canonical channel since midnight. Raises when the
    trades cannot be read -- callers decide what that means."""
    now = now or _clock.now()
    totals: dict[str, float] = {}
    for source, pnl in risk_repo.closed_pnl_by_source_since(_day_start(now)):
        name = _canonical(source)
        totals[name] = totals.get(name, 0.0) + pnl
    return totals


# ── The gate ────────────────────────────────────────────────────────────────

def check(source: str, now: Optional[datetime] = None) -> tuple[bool, str]:
    """(allowed, reason) for a new automated entry from `source`."""
    channel = _canonical(source)
    if not _is_channel(channel):
        return True, ""
    limit = cap_for(channel)
    if limit <= 0:
        return True, ""
    try:
        pnl = day_pnl_by_channel(now).get(channel, 0.0)
    except Exception as exc:
        log.error("[ChannelLossCap] could not read today's P&L for %s: %s", channel, exc)
        return False, (
            f"{channel}: could not read today's P&L to check its daily loss cap "
            "-- entry refused (Trading > Schedule)"
        )
    if pnl > -limit:
        return True, ""
    return False, (
        f"{channel} daily loss cap reached (${pnl:.2f} today, cap ${limit:.2f}) "
        "-- resumes tomorrow (Trading > Schedule)"
    )


def state(now: Optional[datetime] = None) -> dict:
    """What the Schedule screen's card shows: the settings, and per channel
    the cap in force, today's P&L and whether entries are being held."""
    default = get_default_cap()
    overrides = get_overrides()
    try:
        pnl = day_pnl_by_channel(now)
        pnl_error = ""
    except Exception as exc:
        log.warning("[ChannelLossCap] state: could not read today's P&L: %s", exc)
        pnl, pnl_error = {}, "Could not read today's trades."
    names = list(dict.fromkeys(
        [n for n in _channel_names() if _is_channel(n)]
        + [n for n in overrides if _is_channel(n)]
    ))
    rows = []
    for name in names:
        limit = overrides.get(name, default)
        day = round(pnl.get(name, 0.0), 2)
        rows.append({
            "channel": name, "cap": limit, "day_pnl": day,
            "held": limit > 0 and day <= -limit,
        })
    return {
        "default_cap": default, "overrides": overrides,
        "channels": rows, "error": pnl_error,
    }


async def state_async() -> dict:
    """The same state, read off the event loop."""
    return await db_module.to_db_thread(state)


def _screen_state() -> Optional[dict]:
    try:
        return state()
    except Exception as exc:
        log.warning("[ChannelLossCap] unreadable for the schedule screen: %s", exc)
        return None


async def screen_state_async() -> Optional[dict]:
    """The card's state as an addition to the schedule screen's one read.

    None when it cannot be read, never a cheerful default: a card showing
    "no caps set" would say entries are allowed that may be held. And an
    addition that cannot be read costs itself, not the page."""
    return await db_module.to_db_thread(_screen_state)


# ── Sync ────────────────────────────────────────────────────────────────────

def snapshot_fields() -> dict:
    return {"channel_loss_cap": get_default_cap(), "channel_loss_caps": get_overrides()}


def apply_snapshot_fields(snapshot: dict) -> None:
    """Apply the caps from a peer's schedule snapshot. A peer that predates
    the fields sends neither, and that leaves them alone rather than clearing
    them on every sync tick."""
    if "channel_loss_cap" not in snapshot and "channel_loss_caps" not in snapshot:
        return
    try:
        set_caps(
            snapshot.get("channel_loss_cap", get_default_cap()),
            snapshot.get("channel_loss_caps", get_overrides()),
            _from_sync=True,
        )
    except ValueError as exc:
        log.warning("[ChannelLossCap] ignored an invalid synced cap: %s", exc)

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

Per-source toggles (2026-07-24): each of the 7x4 windows also independently
gates Telegram / Reversal Engine / Breakout Engine (SOURCE_KEYS) -- see
check_trading_schedule()'s `source` parameter. Reversal Engine performs well
overnight (Asia) but loses during London/NY, the opposite of the Telegram
channels, so a single blanket automated-order switch isn't enough; each
engine's own live-execution path (reversal_engine_live_execute.py,
breakout_signal_live_execute.py) now calls this with its own source key,
alongside the four pre-existing Telegram call sites.

Per-window strategy/EA-template override (2026-08-01): each window also
carries an optional `strategy_override` -- a STRATEGY_* key or a
"template:<name>" override string, same shape as
core_db_channel.get_channel_strategy_override()'s return value. When the
schedule is enabled and the current time falls inside a window with this
set, get_schedule_strategy_override() returns it and
core_signal_resolution.resolve_open_trade_params() substitutes it in place
of that signal's normal per-channel override -- so a window can force
Reversal Engine and/or Breakout Engine (whichever are ticked) onto one
strategy/template regardless of what's picked per-channel on the Trading
page, for as long as that window is active. A 4th window per day was added
alongside this (BLOCKS_PER_DAY 3 -> 4).

Per-channel Telegram toggle+override (2026-08-03): the single "telegram"
switch above was too coarse for anyone running multiple Telegram channels
with different personalities (e.g. one scalps London, one only performs
overnight) -- each window now carries `telegram_channels`, a
{channel_name: {"enabled", "strategy_override"}} map keyed by the same
canonical channel name core_db_channel.get_channel_strategy_override() uses,
so channel A can run Strategy X and channel B can run Strategy Y within the
same window. A channel never explicitly added to a window's map (including
every channel, for a schedule saved before this feature existed) falls back
to that window's `telegram_default_enabled` bool with no override -- see
_migrate_telegram_field()'s docstring for the exact migration from the old
flat "telegram" bool. Reversal Engine/Breakout Engine are unaffected --
neither has a live Telegram identity to split by, so they keep the single
window-level strategy_override above.

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

from backend.src.db.database import db, _schedule_coro
from backend.src.db import database as db_module
from backend.src.services.risk import repo as risk_repo

log = logging.getLogger(__name__)

DAY_NAMES = [
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
]
BLOCKS_PER_DAY = 4
_PRE_WINDOW4_BLOCKS_PER_DAY = 3  # schedules saved before the 4th window existed


# "telegram" was a SOURCE_KEYS member (one flat bool for every Telegram
# channel combined) until 2026-08-03, when it split into per-channel
# toggles+overrides (telegram_channels/telegram_default_enabled below) so a
# window can run a different strategy per channel. ENGINE_SOURCE_KEYS keeps
# the two internal engines' simple single-toggle behaviour unchanged --
# neither has a live Telegram identity to split by.
ENGINE_SOURCE_KEYS = ("reversal_engine", "breakout_engine")
SOURCE_KEYS = ENGINE_SOURCE_KEYS  # back-compat alias -- "telegram" no longer applies here
_SOURCE_LABELS = {
    "reversal_engine": "Reversal Engine", "breakout_engine": "Breakout Engine",
}


def _default_block() -> dict:
    # Per-source toggles (2026-07-24) default True -- a schedule saved before
    # this feature existed must keep allowing every source exactly as before,
    # not suddenly block Reversal Engine/Breakout Engine because a new field
    # is missing. strategy_override (2026-08-01) defaults "" -- no override,
    # same "fall back to normal per-channel resolution" behaviour a schedule
    # saved before this field existed must keep getting. telegram_channels/
    # telegram_default_enabled (2026-08-03) replace the old flat "telegram"
    # bool -- an empty telegram_channels dict + default_enabled=True means
    # every channel is allowed with no override, the same as the old
    # "telegram": True default. reversal_engine_override/breakout_engine_
    # override (2026-08-03) replace the single shared strategy_override for
    # engines -- kept in the dict (always "") only so a block that predates
    # this split still round-trips through save/load without a stray key
    # error; _migrate_engine_overrides() is what actually carries an old
    # shared value forward into both new fields on first load.
    return {
        "enabled": False, "start": "00:00", "end": "23:59", "target": 0.0,
        "strategy_override": "",
        "reversal_engine_override": "",
        "breakout_engine_override": "",
        "telegram_channels": {},
        "telegram_default_enabled": True,
        **{k: True for k in ENGINE_SOURCE_KEYS},
    }


def _default_schedule() -> dict:
    return {day: [_default_block() for _ in range(BLOCKS_PER_DAY)] for day in DAY_NAMES}


def get_trading_schedule() -> dict:
    """Return the full 7-day x 4-block schedule, filling in defaults for any
    missing/malformed day so callers never need to guard against KeyError.

    A schedule saved before the 4th window was added (2026-08-01) has exactly
    _PRE_WINDOW4_BLOCKS_PER_DAY (3) stored blocks per day -- padded with a
    trailing default (disabled) block rather than discarded outright, so
    upgrading doesn't silently wipe every previously-configured window back
    to defaults. Any OTHER wrong count is still treated as malformed data
    and reset to full defaults for that day, exactly as before."""
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
        if not isinstance(blocks, list):
            continue
        if len(blocks) == _PRE_WINDOW4_BLOCKS_PER_DAY:
            blocks = blocks + [_default_block()]
        elif len(blocks) != BLOCKS_PER_DAY:
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
                    "strategy_override": str(b.get("strategy_override", "") or ""),
                    **{k: bool(b.get(k, True)) for k in ENGINE_SOURCE_KEYS},
                })
                block["telegram_channels"], block["telegram_default_enabled"] = (
                    _migrate_telegram_field(b)
                )
                block["reversal_engine_override"], block["breakout_engine_override"] = (
                    _migrate_engine_overrides(b)
                )
            merged.append(block)
        schedule[day] = merged
    return schedule


def _migrate_engine_overrides(b: dict) -> tuple[str, str]:
    """Read a stored block's per-engine strategy overrides, migrating the
    pre-2026-08-03 single shared "strategy_override" (one dropdown for
    whichever of Reversal/Breakout Engine were ticked) into two independent
    fields the first time it's loaded -- so an existing override keeps
    applying to both engines exactly as before until the user deliberately
    picks a different one for either."""
    legacy = str(b.get("strategy_override", "") or "")
    re_ov = str(b.get("reversal_engine_override", "") or "") or legacy
    bo_ov = str(b.get("breakout_engine_override", "") or "") or legacy
    return re_ov, bo_ov


def _migrate_telegram_field(b: dict) -> tuple[dict, bool]:
    """Read a stored block's per-channel Telegram settings, migrating the
    pre-2026-08-03 flat "telegram" bool (one switch for every channel) into
    the new {channel: {"enabled", "strategy_override"}} shape the first time
    it's loaded. A block with no explicit per-channel entry for a given
    channel falls back to telegram_default_enabled -- so migrating an old
    "telegram": False block blocks every channel exactly as before, and a
    channel added later (never explicitly configured) inherits whatever the
    window's default already is rather than silently going unblocked."""
    raw_channels = b.get("telegram_channels")
    if isinstance(raw_channels, dict):
        channels = {}
        for name, cfg in raw_channels.items():
            if not isinstance(cfg, dict):
                continue
            channels[str(name)] = {
                "enabled": bool(cfg.get("enabled", True)),
                "strategy_override": str(cfg.get("strategy_override", "") or ""),
            }
        default_enabled = bool(b.get("telegram_default_enabled", True))
        return channels, default_enabled
    # Old format: a flat "telegram" bool, no per-channel breakdown yet.
    return {}, bool(b.get("telegram", True))


def set_trading_schedule(schedule: dict, _from_sync: bool = False) -> None:
    db_module.set_app_config("trading_schedule", json.dumps(schedule))
    _maybe_forward_trading_schedule(_from_sync)


def is_trading_schedule_enabled() -> bool:
    return db_module.get_app_config("trading_schedule_enabled") == "1"


def set_trading_schedule_enabled(enabled: bool, _from_sync: bool = False) -> None:
    db_module.set_app_config("trading_schedule_enabled", "1" if enabled else "0")
    _maybe_forward_trading_schedule(_from_sync)


def get_daily_profit_target() -> float:
    """Cumulative profit target across the WHOLE day (every window combined),
    checked ahead of and independent of whichever per-window target is also
    configured. 0 (default) disables this gate entirely -- trading discipline
    then falls back to each window's own target exactly as before this
    feature existed."""
    raw = db_module.get_app_config("trading_schedule_daily_target")
    try:
        return float(raw) if raw else 0.0
    except (TypeError, ValueError):
        return 0.0


def set_daily_profit_target(value: float, _from_sync: bool = False) -> None:
    db_module.set_app_config("trading_schedule_daily_target", str(float(value or 0)))
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
    """Combined {enabled, schedule, daily_target} snapshot -- the unit sent/
    received over the Local/Remote sync channel, since the UI edits and
    saves all three pieces as one atomic action."""
    return {
        "enabled": is_trading_schedule_enabled(),
        "schedule": get_trading_schedule(),
        "daily_target": get_daily_profit_target(),
    }


def apply_trading_schedule_snapshot(snapshot: dict) -> None:
    """Apply a combined snapshot that just arrived over sync from the paired
    node -- always applies with _from_sync=True, since receiving IS the sync
    path (forwarding it again would ping-pong back to the sender)."""
    if snapshot.get("schedule"):
        set_trading_schedule(snapshot["schedule"], _from_sync=True)
    if "enabled" in snapshot:
        set_trading_schedule_enabled(bool(snapshot["enabled"]), _from_sync=True)
    if "daily_target" in snapshot:
        set_daily_profit_target(float(snapshot["daily_target"] or 0), _from_sync=True)


def _forward_trading_schedule_over_sync() -> None:
    """Send this node's current trading schedule snapshot to the paired
    node, whichever role this process has. No-op (and near-zero cost) if
    sync isn't configured -- both get_instance() calls return None until
    sync.server.init()/sync.client.get_instance() have actually been used.
    Mirrors core_db_risk_settings._forward_settings_over_sync() exactly."""
    try:
        from backend.src.services.cluster.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None:
            _schedule_coro(cli.propose_trading_schedule(trading_schedule_snapshot()))
            return
    except Exception as e:
        log.debug("[Sync] trading schedule forward (client) failed: %s", e)

    try:
        from backend.src.services.cluster.sync import server as _sync_srv_mod
        srv = _sync_srv_mod.get_instance()
        if srv is not None:
            _schedule_coro(srv.broadcast_trading_schedule())
    except Exception as e:
        log.debug("[Sync] trading schedule forward (server) failed: %s", e)


def _resolve_source_gate(block: dict, source: str) -> tuple[bool, Optional[str]]:
    """Return (enabled, strategy_override) for `source` within `block`.

    `source` is either an ENGINE_SOURCE_KEYS member (its own toggle + its
    own "<source>_engine_override" -- e.g. reversal_engine_override -- since
    each internal engine can run a different strategy) or a Telegram channel
    name -- canonicalised the same way core_db_channel.get_channel_strategy_
    override() does, then looked up in telegram_channels. A channel with no
    explicit entry yet (never configured, or migrated from the old flat
    "telegram" bool) falls back to telegram_default_enabled with no
    override, exactly matching pre-split behaviour."""
    if source in ENGINE_SOURCE_KEYS:
        return bool(block.get(source, True)), (block.get(f"{source}_override") or None)
    from backend.src.services.channels.repo import canonical_channel_name
    canon = canonical_channel_name(source)
    cfg = block.get("telegram_channels", {}).get(canon)
    if cfg is not None:
        return bool(cfg.get("enabled", True)), (cfg.get("strategy_override") or None)
    return bool(block.get("telegram_default_enabled", True)), None


def get_schedule_strategy_override(source: str) -> Optional[str]:
    """Return the active window's strategy_override for `source` (a
    STRATEGY_* key or a "template:<name>" override string, same shape as
    core_db_channel.get_channel_strategy_override()'s return value), or None
    if the schedule is off, no window is active, this window doesn't have
    `source` enabled, or the active window has no override configured.

    None means "no opinion" -- the caller should fall back to its own normal
    (per-channel) strategy resolution exactly as before this feature
    existed. This never blocks a trade the way check_trading_schedule's
    profit-target gate does; it only substitutes which strategy is used."""
    if not is_trading_schedule_enabled():
        return None
    now = datetime.now()
    schedule = get_trading_schedule()
    _idx, block = _find_active_block(schedule, now)
    if block is None:
        return None
    enabled, override = _resolve_source_gate(block, source)
    if not enabled:
        return None
    return override


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


def _day_realized_pnl(now: datetime) -> float:
    """Sum net_pnl of closed trades opened any time today (00:00-24:00 local),
    across every window -- the daily cumulative target's denominator, as
    opposed to _block_realized_pnl's single-window one."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    from backend.src.services.analytics import read_repo as _reads
    return _reads.realised_pnl_opened_since(day_start.timestamp())


def _block_realized_pnl(block: dict, now: datetime) -> float:
    """Sum net_pnl of closed trades opened within today's occurrence of this
    block's [start, end) window."""
    day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    window_start = day_start.timestamp() + _parse_hm(block["start"]) * 60
    window_end   = day_start.timestamp() + _parse_hm(block["end"]) * 60
    return risk_repo.sum_closed_pnl_opened_between(window_start, window_end)


def check_trading_schedule(
    now: Optional[datetime] = None, source: str = "telegram",
) -> tuple[bool, str]:
    """Return (allowed, reason). `now` is injectable for tests; defaults to
    local wall-clock time, matching the plain HH:MM inputs in the UI.

    `source` (2026-07-24) is either an ENGINE_SOURCE_KEYS member -- Reversal
    Engine performs well overnight (Asia) but loses during London/NY, while
    Telegram signals are the opposite, so each of the 7x4 windows
    independently gates each engine -- or (2026-08-03) the actual Telegram
    channel name a signal came from, gated per-channel within the window.
    The four pre-existing Telegram call sites (core_signal_resolution.py,
    ea_bridge.py x2, core_instant_entry.py) each pass their signal's own
    channel name, not the literal "telegram" default below (which only
    exists so a caller that can't determine a channel falls back to that
    window's telegram_default_enabled rather than erroring)."""
    if not is_trading_schedule_enabled():
        return True, ""
    now = now or datetime.now()

    # Cumulative daily target (2026-07-27) -- checked ahead of, and
    # independent of, the per-window schedule below: once the day's running
    # total clears this, trading stops for the rest of the day regardless of
    # which window/hours would otherwise still be open. 0 (default) disables
    # this gate and falls straight through to the per-window target(s) below,
    # exactly as before this feature existed.
    daily_target = get_daily_profit_target()
    if daily_target > 0:
        day_pnl = _day_realized_pnl(now)
        if day_pnl >= daily_target:
            return False, (
                f"daily profit target reached (${day_pnl:.2f} of ${daily_target:.2f}) "
                "-- resumes tomorrow (Trading > Schedule)"
            )

    schedule = get_trading_schedule()
    idx, block = _find_active_block(schedule, now)
    if block is None:
        return False, f"outside today's trading schedule ({DAY_NAMES[now.weekday()].title()})"
    src_enabled, _src_override = _resolve_source_gate(block, source)
    if not src_enabled:
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

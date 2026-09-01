"""The Trading Schedule editor: day and window screens, the per-window
toggles, and the typed-value replies."""
from __future__ import annotations

import datetime
import re

from backend.src.utils.uk_clock import uk_now
from typing import Optional

from backend.src.services.risk import schedule as schedule_mod

from backend.src.services.positions._panel_shared import (
    Screen, _btn, _dot, _money, _short, _slug,
)


_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

_SCHED_FIELDS = {
    "start":  {"label": "Start time", "hint": "24h time, e.g. 08:30"},
    "end":    {"label": "End time",   "hint": "24h time, e.g. 17:00"},
    "target": {"label": "Profit target ($)", "hint": "0 turns the target off"},
    "daily":  {"label": "Daily profit target ($)", "hint": "0 turns the target off"},
}

def _sched_blocks(day: int) -> Optional[list]:
    if day < 0 or day >= len(schedule_mod.DAY_NAMES):
        return None
    return schedule_mod.get_trading_schedule().get(schedule_mod.DAY_NAMES[day])

def _sched_block(day: int, idx: int) -> tuple[Optional[dict], Optional[dict]]:
    """(whole schedule, one window) -- both, because every write has to save
    the entire schedule back, not just the window that changed."""
    if day < 0 or day >= len(schedule_mod.DAY_NAMES):
        return None, None
    full = schedule_mod.get_trading_schedule()
    blocks = full.get(schedule_mod.DAY_NAMES[day]) or []
    if idx < 0 or idx >= len(blocks):
        return None, None
    return full, blocks[idx]

def _save_block(full: dict, day: int, idx: int, changes: dict) -> None:
    full[schedule_mod.DAY_NAMES[day]][idx].update(changes)
    schedule_mod.set_trading_schedule(full)

def schedule_screen() -> Screen:
    enabled = schedule_mod.is_trading_schedule_enabled()
    daily = schedule_mod.get_daily_profit_target()
    # UK wall time, the same clock the gate reads -- otherwise the day
    # highlighted on this screen can differ from the day being enforced.
    today = uk_now().weekday()

    rows = [
        [_btn(f"{_dot(enabled)} Schedule: {'ON' if enabled else 'OFF'}", "sch2", "en")],
        [_btn(f"\U0001f3af Daily target: {_money(daily) if daily > 0 else 'OFF'}",
              "schx", 0, 0, "daily")],
    ]
    pair = []
    for day in range(len(schedule_mod.DAY_NAMES)):
        blocks = _sched_blocks(day) or []
        live = sum(1 for b in blocks if b.get("enabled"))
        mark = "▶ " if day == today else ""
        pair.append(_btn(f"{mark}{_DAY_ABBR[day]} ({live}/{len(blocks)})", "schd", day))
        if len(pair) == 3:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("← Back to Main Menu", "root")])

    if enabled:
        allowed, reason = schedule_mod.check_trading_schedule()
        state = "Trading window open" if allowed else f"Blocked — {reason}"
    else:
        state = "Off — automated entries are never gated by time."
    text = (f"\U0001f5d3️ *Trading Schedule*\n\n{state}\n\n"
            f"_Gates automated entries only. Manual orders placed from this "
            f"panel are never blocked. Each day has "
            f"{schedule_mod.BLOCKS_PER_DAY} windows._")
    return Screen(text, rows)

def schedule_day_screen(day: int) -> Screen:
    blocks = _sched_blocks(day)
    if blocks is None:
        return Screen(toast="Unknown day.", mode="noop")
    rows = []
    for i, b in enumerate(blocks):
        target = float(b.get("target") or 0)
        label = (f"{_dot(b.get('enabled'))} W{i + 1}  {b.get('start')}–{b.get('end')}"
                 f"{f'  {_money(target)}' if target > 0 else ''}")
        rows.append([_btn(label, "schw", day, i)])
    rows.append([_btn("← Back", "sch")])
    return Screen(f"\U0001f5d3️ *{schedule_mod.DAY_NAMES[day].title()}*\n\n"
                  f"Pick a window to edit its hours, profit target and which "
                  f"sources may trade in it.", rows)

def schedule_window_screen(day: int, idx: int) -> Screen:
    _full, block = _sched_block(day, idx)
    if block is None:
        return Screen(toast="Unknown window.", mode="noop")
    target = float(block.get("target") or 0)
    rows = [
        [_btn(f"{_dot(block.get('enabled'))} Window: "
              f"{'ON' if block.get('enabled') else 'OFF'}", "scht", day, idx, "enabled")],
        [_btn(f"\U0001f552 Start: {block.get('start')}", "schx", day, idx, "start"),
         _btn(f"\U0001f556 End: {block.get('end')}", "schx", day, idx, "end")],
        [_btn(f"\U0001f3af Target: {_money(target) if target > 0 else 'OFF'}",
              "schx", day, idx, "target")],
        [_btn("\U0001f4e2 Telegram channels", "schc", day, idx)],
    ]
    for key in schedule_mod.ENGINE_SOURCE_KEYS:
        label = key.replace("_", " ").title()
        rows.append([_btn(f"{_dot(block.get(key, True))} {label}", "scht", day, idx, key)])
    rows.append([_btn("← Back", "schd", day)])

    overrides = [f"{k.replace('_', ' ').title()} → {block.get(f'{k}_override')}"
                 for k in schedule_mod.ENGINE_SOURCE_KEYS if block.get(f"{k}_override")]
    note = ("\n\n_Strategy overrides on this window: "
            + "; ".join(overrides) + ". Change those on the Trading page._"
            if overrides else "")
    return Screen(f"\U0001f5d3️ *{schedule_mod.DAY_NAMES[day].title()} — "
                  f"Window {idx + 1}*\n"
                  f"{block.get('start')}–{block.get('end')}{note}", rows)

def _window_channel_enabled(block: dict, name: str) -> bool:
    cfg = (block.get("telegram_channels") or {}).get(name)
    if cfg is not None:
        return bool(cfg.get("enabled", True))
    return bool(block.get("telegram_default_enabled", True))

def _telegram_channel_names() -> list[str]:
    from backend.src.services.channels.repo import get_telegram_channel_names
    return get_telegram_channel_names()

def schedule_channels_screen(day: int, idx: int) -> Screen:
    _full, block = _sched_block(day, idx)
    if block is None:
        return Screen(toast="Unknown window.", mode="noop")
    names = _telegram_channel_names()
    rows = []
    for name in names:
        on = _window_channel_enabled(block, name)
        rows.append([_btn(f"{_dot(on)} {_short(name, 24)}", "schtc", day, idx, _slug(name))])
    default_on = bool(block.get("telegram_default_enabled", True))
    rows.append([_btn(f"{_dot(default_on)} Default (unlisted channels): "
                      f"{'ON' if default_on else 'OFF'}", "scht", day, idx, "tgdef")])
    rows.append([_btn("← Back", "schw", day, idx)])
    body = ("Which channels may trade in this window."
            if names else "No Telegram channels are configured yet.")
    return Screen(f"\U0001f4e2 *{schedule_mod.DAY_NAMES[day].title()} — "
                  f"Window {idx + 1}*\n{block.get('start')}–{block.get('end')}\n\n{body}", rows)

def _toggle_schedule_enabled() -> Screen:
    enabled = not schedule_mod.is_trading_schedule_enabled()
    schedule_mod.set_trading_schedule_enabled(enabled)
    screen = schedule_screen()
    screen.toast = f"Trading Schedule {'ON' if enabled else 'OFF'}"
    return screen

def _toggle_window_flag(day: int, idx: int, key: str) -> Screen:
    """`key` is 'enabled', 'tgdef' (the window's telegram_default_enabled) or
    an ENGINE_SOURCE_KEYS member."""
    full, block = _sched_block(day, idx)
    if block is None:
        return Screen(toast="Unknown window.", mode="noop")
    field = "telegram_default_enabled" if key == "tgdef" else key
    if field not in ("enabled", "telegram_default_enabled") \
            and field not in schedule_mod.ENGINE_SOURCE_KEYS:
        return Screen(toast="Unknown setting.", mode="noop")
    # Only `enabled` defaults off -- every source toggle defaults on, same as
    # _default_block, so a window missing the key isn't read as blocking.
    value = not bool(block.get(field, field != "enabled"))
    _save_block(full, day, idx, {field: value})
    screen = (schedule_channels_screen(day, idx) if key == "tgdef"
              else schedule_window_screen(day, idx))
    screen.toast = f"{field.replace('_', ' ')} = {'ON' if value else 'OFF'}"
    return screen

def _toggle_window_channel(day: int, idx: int, slug: str) -> Screen:
    full, block = _sched_block(day, idx)
    if block is None:
        return Screen(toast="Unknown window.", mode="noop")
    name = next((n for n in _telegram_channel_names() if _slug(n) == slug), None)
    if name is None:
        return Screen(toast="That channel no longer exists.", mode="noop")
    channels = dict(block.get("telegram_channels") or {})
    cfg = dict(channels.get(name) or {})
    value = not _window_channel_enabled(block, name)
    cfg["enabled"] = value
    # Preserve any strategy override this window already carries for the
    # channel -- it is not editable here, so a toggle must not clear it.
    cfg.setdefault("strategy_override", "")
    channels[name] = cfg
    _save_block(full, day, idx, {"telegram_channels": channels})
    screen = schedule_channels_screen(day, idx)
    screen.toast = f"{name}: {'ON' if value else 'OFF'}"[:190]
    return screen

def schedule_prompt_text(day: int, idx: int, field: str) -> str:
    meta = _SCHED_FIELDS.get(field) or {"label": field, "hint": ""}
    if field == "daily":
        where = "the whole day (every window combined)"
        token = "sch.daily"
    else:
        where = (f"{schedule_mod.DAY_NAMES[day].title()} window {idx + 1}")
        token = f"sch.{day}.{idx}"
    return (f"Send the new {meta['label']} for {where}.\n"
            f"{meta['hint']}\n[{field}@{token}]")

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

def _schedule_value_reply(field: str, token: str, raw: str) -> Screen:
    """A typed reply to one of the schedule's 'set exact value' prompts."""
    if field == "daily":
        try:
            value = max(0.0, float(raw.lstrip("$")))
        except ValueError:
            return Screen(f"`{raw}` is not a number.", mode="send")
        schedule_mod.set_daily_profit_target(value)
        return Screen(f"✅ Daily profit target set to "
                      f"{_money(value) if value > 0 else 'OFF'}.", mode="send")

    parts = token.split(".")
    if len(parts) != 3:
        return Screen(mode="noop")
    try:
        day, idx = int(parts[1]), int(parts[2])
    except ValueError:
        return Screen(mode="noop")
    full, block = _sched_block(day, idx)
    if block is None:
        return Screen("That window no longer exists.", mode="send")

    if field in ("start", "end"):
        match = _TIME_RE.match(raw.strip())
        if not match:
            return Screen(f"`{raw}` is not a 24-hour time. Use HH:MM, e.g. 08:30.",
                          mode="send")
        value = f"{int(match.group(1)):02d}:{match.group(2)}"
        other = block.get("end" if field == "start" else "start")
        start, end = (value, other) if field == "start" else (other, value)
        # An end at or before its start matches no minute of the day, so the
        # window would silently never open -- refuse rather than save a
        # window that looks configured and does nothing.
        try:
            if schedule_mod._parse_hm(start) >= schedule_mod._parse_hm(end):
                return Screen(f"Start ({start}) must be before end ({end}).", mode="send")
        except Exception:
            pass
    elif field == "target":
        try:
            value = max(0.0, float(raw.lstrip("$")))
        except ValueError:
            return Screen(f"`{raw}` is not a number.", mode="send")
    else:
        return Screen(mode="noop")

    _save_block(full, day, idx, {field: value})
    label = _SCHED_FIELDS[field]["label"]
    shown = _money(value) if field == "target" else value
    if field == "target" and not float(value):
        shown = "OFF"
    return Screen(f"✅ {label} for {schedule_mod.DAY_NAMES[day].title()} "
                  f"window {idx + 1} set to `{shown}`.", mode="send")

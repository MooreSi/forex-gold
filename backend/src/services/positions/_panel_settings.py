"""Editing a channel's settings from the panel: the field machinery
(read/write/coerce/clamp) and the screens that drive it.

The field helpers and their screens live together on purpose -- a screen here
is little more than a rendering of what _meta says a field is, and splitting
the two would mean reading both files to answer any question about either.
"""
from __future__ import annotations

import logging
import re
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.broker import ea_templates

from backend.src.utils.models import STRATEGY_NAMES

from backend.src.services.positions._panel_shared import (
    MANUAL,
    Screen, _btn, _channel, _short, _slug, _strategy_label,
)

log = logging.getLogger(__name__)

FIELDS: dict[str, dict] = {
    "anchors":       {"emoji": "⚓", "label": "Anchors",      "kind": "int",   "step": 1},
    "lot_anchor":    {"emoji": "\U0001f4b0", "label": "Lot (Anchor)",  "kind": "float", "step": 0.01, "dp": 2},
    "pendings":      {"emoji": "\U0001f4ca", "label": "Layers",        "kind": "int",   "step": 1},
    "lot_pending":   {"emoji": "\U0001f4b0", "label": "Lot (Pending)", "kind": "float", "step": 0.01, "dp": 2},
    "risk_pct":      {"emoji": "\U0001f3af", "label": "Risk %",        "kind": "float", "step": 0.1,  "dp": 2},
    "grid_step_pts": {"emoji": "\U0001f4cf", "label": "Ladder Step",   "kind": "float", "step": 1.0,  "dp": 1},
    "sl_pips":       {"emoji": "\U0001f4cf", "label": "SL Pips",       "kind": "float", "step": 5.0,  "dp": 1},
    "harvest_enabled": {"emoji": "\U0001f33e", "label": "Harvest",     "kind": "bool"},
    "mode":          {"emoji": "\U0001fa9c", "label": "Grid Mode",     "kind": "choice"},
    "tpsl_mode":     {"emoji": "\U0001f3c1", "label": "TP Mode",       "kind": "choice"},
    "anc_shave":     {"emoji": "\U0001f528", "label": "Anchor Shave",  "kind": "bool"},
    "be_mode":       {"emoji": "\U0001f4cd", "label": "BE Mode",       "kind": "choice"},
    "cancel_pending_level": {"emoji": "\U0001f6ab", "label": "Delete Pending", "kind": "int", "step": 1,
                             "zero": "OFF", "prefix": "TP"},
    "be_trigger":    {"emoji": "⚡", "label": "BreakEven",     "kind": "int",   "step": 1, "prefix": "TP"},
    "trail_mode":    {"emoji": "⏰", "label": "Trail",         "kind": "choice"},
    "sig_guard":     {"emoji": "\U0001f6e1", "label": "SIG GUARD",  "kind": "bool"},
    "late_guard_pips": {"emoji": "\U0001f6e1", "label": "Late Guard Pips", "kind": "float", "step": 5.0,
                        "dp": 1, "zero": "OFF"},
    "equity_protect": {"emoji": "\U0001f6df", "label": "Equity Protect $", "kind": "float", "step": 10.0,
                       "dp": 0, "zero": "OFF"},
    "max_spread_pips": {"emoji": "\U0001f4d0", "label": "Max Spread", "kind": "float", "step": 1.0, "dp": 1},
    "tp_from_telegram":     {"emoji": "\U0001f4e5", "label": "TP from Telegram", "kind": "bool"},
    "tp_pen_from_telegram": {"emoji": "\U0001f4e5", "label": "TP from Telegram", "kind": "bool"},
}
# Global (non-template) settings the slim screen edits, stored in risk
# settings rather than on a template.
GLOBAL_FIELDS: dict[str, dict] = {
    "risk_per_trade_pct": {"emoji": "\U0001f3af", "label": "Risk % (global)", "kind": "float",
                           "step": 0.1, "dp": 2},
    "strategy_lot_size":  {"emoji": "\U0001f4b0", "label": "Lot (global)", "kind": "float",
                           "step": 0.01, "dp": 2, "zero": "AUTO"},
    "strategy_lot_size_grid": {"emoji": "\U0001f4b0", "label": "Lot (grid legs)", "kind": "float",
                               "step": 0.01, "dp": 2, "zero": "OFF"},
}




def _active_btn(chan: dict) -> dict:
    """The channel on/off row. Built here rather than inline because the label
    needs an emoji literal chosen by a conditional, which cannot live inside
    an f-string expression on Python 3.11 (the repo's floor)."""
    dot = "\U0001f534" if chan["paused"] else "\U0001f7e2"
    state = "Paused" if chan["paused"] else "Active"
    return _btn(f"{dot} Channel {state}", "pause", chan["slug"])

def _meta(field: str) -> dict:
    return FIELDS.get(field) or GLOBAL_FIELDS.get(field) or {
        "emoji": "⚙", "label": field, "kind": "float", "step": 1.0, "dp": 2}

def _fmt_value(field: str, value) -> str:
    m = _meta(field)
    kind = m["kind"]
    if kind == "bool":
        return "ON" if value else "OFF"
    if kind == "choice":
        return str(value).upper()
    try:
        num = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not num and m.get("zero"):
        return m["zero"]
    if kind == "int":
        return f"{m.get('prefix', '')}{int(num)}"
    return f"{num:.{m.get('dp', 2)}f}"

def _choices(field: str) -> tuple:
    return ea_templates._CHOICES.get(field, ())

def _read_value(chan: dict, field: str):
    if field in GLOBAL_FIELDS:
        return db_module.get_risk_settings().get(field, 0)
    tpl = ea_templates.get_ea_template(chan["template"]) if chan.get("template") else None
    if tpl is None:
        return ea_templates.DEFAULTS.get(field, 0)
    return tpl.get(field, ea_templates.DEFAULTS.get(field, 0))

def _write_value(chan: dict, field: str, value) -> None:
    """Persist one field. Template writes go through save_ea_template, which
    rewrites every column from DEFAULTS -- so the current row must be loaded
    and merged first, or every other field silently resets to its default."""
    if field in GLOBAL_FIELDS:
        db_module.update_risk_settings({field: value})
        return
    name = chan.get("template")
    if not name:
        raise ValueError("This channel is not bound to an EA Template.")
    current = ea_templates.get_ea_template(name)
    if current is None:
        raise ValueError(f"Template {name!r} no longer exists.")
    fields = dict(current)
    fields[field] = value
    ea_templates.save_ea_template(name, fields)

def _coerce(field: str, raw) -> Any:
    m = _meta(field)
    kind = m["kind"]
    if kind == "bool":
        return bool(raw)
    if kind == "choice":
        return str(raw)
    if kind == "int":
        return int(round(float(raw)))
    return round(float(raw), 4)

def _clamp(field: str, value):
    """Keep steppers inside the same bounds _clean_fields enforces, so a tap
    that would be silently corrected shows the corrected value immediately
    rather than appearing to do nothing."""
    m = _meta(field)
    if m["kind"] in ("bool", "choice"):
        return value
    if field in ("be_trigger", "tp1_trigger_level"):
        return max(1, min(ea_templates.MAX_TP_LEVELS, value))
    if field == "cancel_pending_level":
        return max(0, min(ea_templates.MAX_TP_LEVELS, value))
    if field in ("anchors", "pendings"):
        return max(0, min(20, value))
    return max(0, value) if m["kind"] == "int" else max(0.0, value)

def _field_btn(chan: dict, field: str) -> dict:
    m = _meta(field)
    value = _fmt_value(field, _read_value(chan, field))
    kind = m["kind"]
    if kind == "bool":
        act = "fb"
    elif kind == "choice":
        act = "fc"
    else:
        act = "f"
    return _btn(f"{m['emoji']} {m['label']}: {value}", act, chan["slug"], field)

def _template_settings_screen(chan: dict) -> Screen:
    s = chan["slug"]
    tpl = ea_templates.get_ea_template(chan["template"])
    if tpl is None:
        return Screen(toast=f"Template {chan['template']!r} is missing.", mode="noop")
    rows = [
        [_btn("\U0001f4cb Current Settings", "cur", s)],
        [_field_btn(chan, "anchors"), _field_btn(chan, "lot_anchor")],
        [_field_btn(chan, "pendings"), _field_btn(chan, "lot_pending")],
        [_field_btn(chan, "risk_pct")],
        [_field_btn(chan, "grid_step_pts"), _field_btn(chan, "sl_pips")],
        [_btn("\U0001f3af TP & Pcts Settings", "tpm", s)],
        [_field_btn(chan, "harvest_enabled"), _field_btn(chan, "mode")],
        [_field_btn(chan, "tpsl_mode"), _field_btn(chan, "anc_shave")],
        [_field_btn(chan, "be_mode"), _field_btn(chan, "cancel_pending_level")],
        [_field_btn(chan, "be_trigger"), _field_btn(chan, "trail_mode")],
        [_field_btn(chan, "sig_guard"), _field_btn(chan, "late_guard_pips")],
        [_field_btn(chan, "equity_protect"), _field_btn(chan, "max_spread_pips")],
        [_active_btn(chan)],
        [_btn("\U0001f39b️ Change Strategy", "strat", s)],
        [_btn("← Back to Main Menu", "root")],
    ]
    text = (f"⚙️ *{chan['name']}* settings\n"
            f"Template: `{chan['template']}`")
    return Screen(text, rows)

def _basic_settings_screen(chan: dict) -> Screen:
    """A channel on a built-in strategy has none of the template's grid
    fields, so it gets the settings it actually has, plus a route to bind it
    to a template if the full grid is what's wanted."""
    s = chan["slug"]
    rows = [
        [_btn("\U0001f4cb Current Settings", "cur", s)],
        [_btn(f"\U0001f39b️ Strategy: {_short(_strategy_label(chan['strategy']), 22)}", "strat", s)],
        [_field_btn(chan, "risk_per_trade_pct")],
        [_field_btn(chan, "strategy_lot_size"), _field_btn(chan, "strategy_lot_size_grid")],
        [_active_btn(chan)],
        [_btn("← Back to Main Menu", "root")],
    ]
    text = (
        f"⚙️ *{chan['name']}* settings\n"
        f"Strategy: {_strategy_label(chan['strategy'])}\n\n"
        f"_This channel runs a built-in strategy, so the grid fields "
        f"(anchors, layers, ladder, TP pips) do not apply. Bind it to an EA "
        f"Template via Strategy to get those._"
    )
    return Screen(text, rows)

def field_screen(slug: str, field: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    m = _meta(field)
    value = _read_value(chan, field)
    step = m.get("step", 1)
    step_label = f"{step:g}"
    rows = [
        [_btn(f"− {step_label}", "fa", slug, field, "d"),
         _btn(_fmt_value(field, value), "noop"),
         _btn(f"+ {step_label}", "fa", slug, field, "u")],
        [_btn("✏️ Set exact value", "fx", slug, field)],
        [_btn("← Back", *_field_parent(slug, field))],
    ]
    return Screen(f"{m['emoji']} *{m['label']}*\n{chan['name']}\n\n"
                  f"Current: `{_fmt_value(field, value)}`", rows)

def _field_parent(slug: str, field: str) -> tuple:
    if field == "tp_pen_from_telegram" or re.fullmatch(r"tp_pen\d+_(pips|pct)", field):
        return ("tpl", slug, "p")
    if field == "tp_from_telegram" or re.fullmatch(r"tp\d+_(pips|pct)", field):
        return ("tpl", slug, "a")
    return ("cs", slug)

async def _parent_screen(slug: str, field: str) -> "Screen":
    """Re-render whichever screen a field lives on, so an edit returns the
    user to where they were rather than always bouncing to the channel root."""
    action, *args = _field_parent(slug, field)
    # Lazy: core_bot_panel imports this module, so reaching for its
    # dispatcher at module scope would be a circular import.
    from backend.src.services.positions.core_bot_panel import _dispatch
    return await _dispatch(action, args, None)

def choice_screen(slug: str, field: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    m = _meta(field)
    current = _read_value(chan, field)
    rows = [[_btn(f"{'✅ ' if str(current) == c else ''}{c.upper()}", "fs", slug, field, c)]
            for c in _choices(field)]
    rows.append([_btn("← Back", "cs", slug)])
    return Screen(f"{m['emoji']} *{m['label']}*\n{chan['name']}\n\n"
                  f"Current: `{str(current).upper()}`", rows)

def tp_menu_screen(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    rows = [
        [_btn("⚓ Anchor ladder", "tpl", slug, "a")],
        [_btn("\U0001f4ca Pending ladder", "tpl", slug, "p")],
        [_btn("← Back", "cs", slug)],
    ]
    return Screen(f"\U0001f3af *TP & Pcts* — {chan['name']}\n\n"
                  f"The anchor leg and the resting legs carry separate ladders.", rows)

def tp_ladder_screen(slug: str, which: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    prefix = "tp_pen" if which == "p" else "tp"
    tg_field = "tp_pen_from_telegram" if which == "p" else "tp_from_telegram"
    from_tg = bool(_read_value(chan, tg_field))
    rows = [[_field_btn(chan, tg_field)]]
    for n in range(1, ea_templates.MAX_TP_LEVELS + 1):
        pips_f, pct_f = f"{prefix}{n}_pips", f"{prefix}{n}_pct"
        pips_label = f"TP{n} pips: {_fmt_value(pips_f, _read_value(chan, pips_f))}"
        if from_tg:
            # The pips are still live for internal-generator trades, so they
            # stay editable -- marked, not hidden, so it is obvious which
            # column a Telegram trade will actually use.
            pips_label = f"({pips_label})"
        rows.append([
            _btn(pips_label, "f", slug, pips_f),
            _btn(f"%: {_fmt_value(pct_f, _read_value(chan, pct_f))}", "f", slug, pct_f),
        ])
    rows.append([_btn("← Back", "tpm", slug)])
    label = "Pending" if which == "p" else "Anchor"
    note = (
        "_Telegram signals take these levels from the message. The pips in "
        "brackets still apply to internal signals (Reversal, Breakout, "
        "Bounce, ORB), which have no message._"
        if from_tg else
        "_Pips are measured from the fill; % is how much of the position "
        "closes at that level._"
    )
    return Screen(f"\U0001f3af *{label} TP ladder* — {chan['name']}\n\n{note}", rows)

def strategy_screen(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    rows, pair = [], []
    for name in [t["name"] for t in ea_templates.list_ea_templates()]:
        key = ea_templates.override_for_template(name)
        mark = "✅ " if chan["strategy"] == key else ""
        rows.append([_btn(f"{mark}\U0001f4d0 Template: {_short(name, 22)}", "sset", slug, f"t:{name}")])
    for key, label in STRATEGY_NAMES.items():
        mark = "✅ " if chan["strategy"] == key else ""
        pair.append(_btn(f"{mark}{_short(label, 20)}", "sset", slug, key))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("↩️ Inherit global", "sset", slug, "-")])
    rows.append([_btn("← Back", "cs", slug)])
    return Screen(f"\U0001f39b️ *Strategy* — {chan['name']}\n\n"
                  f"Current: {_strategy_label(chan['strategy'])}", rows)

def _adjust_field(slug: str, field: str, direction: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    m = _meta(field)
    step = m.get("step", 1)
    current = float(_read_value(chan, field) or 0)
    value = _clamp(field, _coerce(field, current + (step if direction == "u" else -step)))
    try:
        _write_value(chan, field, value)
    except Exception as e:
        return Screen(toast=f"Error: {e}"[:190], mode="noop")
    screen = field_screen(slug, field)
    screen.toast = f"{m['label']} = {_fmt_value(field, value)}"
    return screen

async def _toggle_field(slug: str, field: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    value = not bool(_read_value(chan, field))
    try:
        _write_value(chan, field, value)
    except Exception as e:
        return Screen(toast=f"Error: {e}"[:190], mode="noop")
    screen = await _parent_screen(slug, field)
    screen.toast = f"{_meta(field)['label']} = {'ON' if value else 'OFF'}"
    return screen

def _set_choice(slug: str, field: str, value: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    if value not in _choices(field):
        return Screen(toast="Unknown option.", mode="noop")
    try:
        _write_value(chan, field, value)
    except Exception as e:
        return Screen(toast=f"Error: {e}"[:190], mode="noop")
    from backend.src.services.positions.core_bot_panel import channel_settings_screen
    screen = channel_settings_screen(slug)
    screen.toast = f"{_meta(field)['label']} = {value.upper()}"
    return screen

def _set_strategy(slug: str, key: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    if key.startswith("t:"):
        strategy = ea_templates.override_for_template(key[2:])
    elif key == "-":
        strategy = None
    else:
        strategy = key
    if chan["name"] == MANUAL:
        # Manual has no channel_performance override -- its strategy IS the
        # app's global Active Strategy, so set that instead of writing a
        # per-channel row the rest of the app would never read.
        if strategy is None:
            return Screen(toast="Manual always uses the global strategy.", mode="noop")
        db_module.update_risk_settings({"trade_strategy": strategy})
    else:
        db_module.set_channel_strategy_override(chan["name"], strategy, auto=False)
    from backend.src.services.positions.core_bot_panel import channel_settings_screen
    screen = channel_settings_screen(slug)
    screen.toast = f"Strategy = {_strategy_label(strategy)}"[:190]
    return screen

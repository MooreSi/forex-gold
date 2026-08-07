"""Button-driven Telegram control panel.

Replaces the typed slash-command surface (see telegram_alerts.BOT_COMMANDS,
now trimmed to /panel, /status and /help) with inline-keyboard screens: a
root menu, a per-channel strategy editor, per-channel trade operations, and
a system menu.

This module is pure logic -- it builds screens and mutates settings, but
performs NO Telegram HTTP itself. Every handler returns a Screen, and
engine.py's _bot_command_loop does the sending/editing/answering. That split
keeps the whole panel testable without a network, and keeps the one place
that knows the bot token unchanged.

Design notes:

* Channels are addressed by an 8-hex slug (md5 of the canonical channel
  name), never by list index. An index would silently re-point at a
  different channel if the channel set changed between rendering a keyboard
  and the user tapping it -- which for the Trades screens means closing
  someone else's positions. The slug is derived, not stored, so it survives
  restarts and cannot go stale.

* Settings screens are not uniform, because our channels are not. A channel
  bound to an EA Template (strategy 'template:<name>') gets the full grid --
  anchors, layers, lots, ladder step, TP ladders, grid/BE/trail modes -- since
  those fields genuinely exist on the template. A channel running a built-in
  strategy has no such fields, so it gets the slim screen (strategy picker,
  risk, lot, active) plus a button to bind it to a template. Showing the full
  grid everywhere would mean either inventing fields or silently rebinding a
  channel's whole execution model as a side effect of opening a menu.

* Numeric fields offer both -/+ steppers and a 'Set exact value' button. The
  exact-value path uses Telegram's force_reply: the prompt text carries a
  '[field@target]' token that parse_prompt reads back off the user's reply,
  since callback data cannot round-trip through a text reply. `target` is a
  channel slug, or one of the Trading Schedule's dotted tokens.

* The Trading Schedule screens (2026-08-07) edit core_trading_schedule's
  7x4 grid from the same keyboards -- see the section comment above
  schedule_screen for why windows are addressed positionally and why the
  per-window strategy overrides stay on the desktop UI.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime
from typing import Any, Optional

from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core import core_trading_schedule as schedule_mod
from forex_trader.core import database as db_module
from forex_trader.core.models import STRATEGY_NAMES

log = logging.getLogger(__name__)

CB = "p"          # callback-data namespace
SEP = "|"

# The panel's own pseudo-channel for orders placed by hand rather than from
# any signal source. Matches open_manual_market_order's default source tag so
# manual trades opened here land in the bucket the rest of the app already
# reports them under.
MANUAL = "Manual"
MANUAL_SOURCE = "manual_market"


class Screen:
    """What engine.py should do with the result of a panel interaction.

    mode:
      'edit'         -- replace the panel message in place (normal navigation)
      'send'         -- post a new message, leaving the panel intact
      'force_reply'  -- post a reply-prompt for a typed value
      'delete'       -- remove the panel message
      'noop'         -- nothing but the toast
    """

    __slots__ = ("text", "keyboard", "toast", "mode")

    def __init__(self, text: str = "", keyboard: Optional[list] = None,
                 toast: str = "", mode: str = "edit"):
        self.text = text
        self.keyboard = keyboard
        self.toast = toast
        self.mode = mode


# ── Callback-data helpers ─────────────────────────────────────────────────────

def _cb(*parts) -> str:
    """Build callback data. Telegram hard-caps this at 64 bytes and silently
    rejects the whole keyboard if any button exceeds it, so assert rather
    than ship a panel whose buttons do nothing."""
    data = SEP.join([CB] + [str(p) for p in parts])
    if len(data.encode()) > 64:
        raise ValueError(f"callback data too long ({len(data.encode())}B): {data}")
    return data


def _btn(text: str, *parts) -> dict:
    return {"text": text, "callback_data": _cb(*parts)}


# ── Channels ──────────────────────────────────────────────────────────────────

def _slug(name: str) -> str:
    return hashlib.md5((name or "").encode()).hexdigest()[:8]


def _channel_rec(name: str, strategy: Optional[str]) -> dict:
    template = None
    if ea_templates.is_template_override(strategy):
        template = ea_templates.template_name_from_override(strategy)
    paused = False
    try:
        _, paused = db_module.get_channel_lot_mult(name)
    except Exception:
        pass
    return {
        "name":     name,
        "slug":     _slug(name),
        "strategy": strategy,
        "template": template,
        "paused":   bool(paused),
    }


def channel_list() -> list[dict]:
    """Every channel the panel can act on, in the same order as the app's own
    Channel Strategy tab, with Manual appended."""
    recs: list[dict] = []
    try:
        for name, info in db_module.get_all_channel_strategy_overrides().items():
            recs.append(_channel_rec(name, info.get("strategy")))
    except Exception as e:
        log.warning("[Panel] channel list failed: %s", e)
    try:
        rs = db_module.get_risk_settings()
        manual_strategy = rs.get("trade_strategy")
    except Exception:
        manual_strategy = None
    recs.append(_channel_rec(MANUAL, manual_strategy))
    return recs


def _channel(slug: str) -> Optional[dict]:
    return next((c for c in channel_list() if c["slug"] == slug), None)


def _short(name: str, limit: int = 18) -> str:
    """Channel names run long ('GOLD DIGGERS INSTITUTIONAL'); Telegram button
    labels get clipped mid-word by the client, so clip deliberately instead."""
    name = name or "?"
    return name if len(name) <= limit else name[: limit - 1].rstrip() + "…"


def _strategy_label(strategy: Optional[str]) -> str:
    if not strategy:
        return "inherit global"
    if strategy == "auto":
        return "Auto (AI)"
    if ea_templates.is_template_override(strategy):
        return f"Template: {ea_templates.template_name_from_override(strategy)}"
    return STRATEGY_NAMES.get(strategy, strategy)


# ── Template field metadata ───────────────────────────────────────────────────
#
# kind: 'float' | 'int' | 'bool' | 'choice'. `step` is the -/+ increment.
# Only fields worth driving from a phone are listed -- the full set stays in
# Settings > EA Templates. Adding a field here is enough to make it editable;
# the generic field editor handles the rest.

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

# TP ladder fields are generated rather than listed -- 32 near-identical
# entries would bury the ones above.
for _n in range(1, ea_templates.MAX_TP_LEVELS + 1):
    FIELDS[f"tp{_n}_pips"] = {"emoji": "\U0001f3c1", "label": f"TP{_n} pips", "kind": "float",
                              "step": 5.0, "dp": 1, "zero": "OFF"}
    FIELDS[f"tp{_n}_pct"] = {"emoji": "\U0001f3c1", "label": f"TP{_n} close %", "kind": "float",
                             "step": 5.0, "dp": 0, "zero": "OFF"}
    FIELDS[f"tp_pen{_n}_pips"] = {"emoji": "\U0001f3c1", "label": f"TP{_n} pips (pending)",
                                  "kind": "float", "step": 5.0, "dp": 1, "zero": "OFF"}
    FIELDS[f"tp_pen{_n}_pct"] = {"emoji": "\U0001f3c1", "label": f"TP{_n} close % (pending)",
                                 "kind": "float", "step": 5.0, "dp": 0, "zero": "OFF"}

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


# ── Value storage ─────────────────────────────────────────────────────────────

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


# ── Screens ───────────────────────────────────────────────────────────────────

def root_screen() -> Screen:
    kb = [
        [_btn("\U0001f4ca Status", "st"), _btn("\U0001f4cb All Settings", "allset")],
        [_btn("⚙️ Channel Strategy", "chlist", "s"),
         _btn("\U0001f3af Channel Trades", "chlist", "t")],
        [_btn("\U0001f4b5 Balance", "bal"), _btn("\U0001f4c8 Daily", "daily")],
        [_btn("\U0001f4dc Open Trades", "trades"), _btn("\U0001f6e0️ System", "sys")],
        [_btn("\U0001f5d3️ Trading Schedule", "sch")],
        [_btn("⛔ CLOSE ALL TRADES", "closeall", "*")],
        [_btn("❌ Close Control Panel", "x")],
    ]
    return Screen("\U0001f3ae *FOREX Control Panel*\n\nChoose an action from the buttons below:", kb)


def _channel_list_screen(kind: str) -> Screen:
    """kind: 's' = settings, 't' = trade operations."""
    rows, pair = [], []
    for c in channel_list():
        mark = "" if not c["paused"] else "⏸ "
        icon = "⚙️" if kind == "s" else "\U0001f3af"
        pair.append(_btn(f"{icon} {mark}{_short(c['name'])}", "cs" if kind == "s" else "ct", c["slug"]))
        if len(pair) == 2:
            rows.append(pair)
            pair = []
    if pair:
        rows.append(pair)
    rows.append([_btn("← Back to Main Menu", "root")])
    title = "Channel settings" if kind == "s" else "Channel trade operations"
    return Screen(f"⚙️ *{title}*\n\nPick a channel:", rows)


def channel_settings_screen(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    if chan["template"]:
        return _template_settings_screen(chan)
    return _basic_settings_screen(chan)


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


def trades_screen(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    s = chan["slug"]
    open_n = len(_channel_open_trades(chan))
    rows = [
        [_btn("\U0001f7e2 BUY", "buy", s), _btn("\U0001f534 SELL", "sell", s)],
        [_btn("\U0001f5d1️ DELETE PENDING", "delp", s), _btn("\U0001f6e1️ RISK FREE", "rf", s)],
        [_btn(f"\U0001f4dc Open trades ({open_n})", "tlist", s)],
        [_btn("❌ CLOSE ALL", "call", s)],
        [_btn("← Back to Main Menu", "root")],
    ]
    return Screen(f"\U0001f3af *{chan['name']}* — trade operations\n"
                  f"Strategy: {_strategy_label(chan['strategy'])}", rows)


def _channel_open_trades(chan: dict) -> list[dict]:
    """Open trades belonging to this channel.

    Matches the channel name and the 'Telegram Auto (<name>)' variant the
    auto-execution path writes, the same pair get_channel_trust checks -- a
    channel's trades are split across both spellings, and closing only one
    set would leave live positions behind while reporting 'all closed'."""
    name = chan["name"]
    variants = [name, f"Telegram Auto ({name})"]
    if name == MANUAL:
        variants = [MANUAL_SOURCE, "Manual Signal"]
    marks = ",".join("?" for _ in variants)
    with db_module.db() as conn:
        rows = conn.execute(
            f"SELECT * FROM vantage_simulated_trades "
            f"WHERE status='open' AND tg_source IN ({marks}) ORDER BY open_time",
            variants,
        ).fetchall()
    return [db_module.row_to_dict(r) for r in rows]


def _trade_push_sl_pips(t: dict) -> float:
    """manual_sl_push_pips for this trade's template, or 0 if it isn't a
    template trade, the template has bot commands off (tg_cmd_enabled), or
    no push amount is configured -- any of which hides the Push SL button
    rather than showing one that would just refuse when tapped."""
    strategy = t.get("strategy") or ""
    if not ea_templates.is_template_override(strategy):
        return 0.0
    tpl = ea_templates.get_ea_template(ea_templates.template_name_from_override(strategy))
    if not tpl or not tpl.get("tg_cmd_enabled"):
        return 0.0
    return float(tpl.get("manual_sl_push_pips") or 0)


def trade_list_screen(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    trades = _channel_open_trades(chan)
    if not trades:
        return Screen(toast="No open trades on this channel.", mode="noop")
    rows = []
    lines = []
    for t in trades:
        tid = t["trade_id"]
        ticket = t.get("mt5_ticket") or 0
        entry = t.get("entry_price") or 0
        label = (f"❌ {t.get('direction', '?')} {t.get('lot_size', '?')} @ "
                 f"{entry:.2f}" if entry else
                 f"❌ {t.get('direction', '?')} {t.get('lot_size', '?')} (pending)")
        row = [_btn(label, "tc", tid[:16])]
        push_pips = _trade_push_sl_pips(t)
        if push_pips > 0 and ticket:
            row.append(_btn(f"\U0001f527 Push SL +{push_pips:.0f}p", "ps", tid[:16]))
        rows.append(row)
        lines.append(f"• `{ticket or 'pending'}` {t.get('direction')} "
                     f"{t.get('lot_size')} @ {entry or '—'}")
    rows.append([_btn("← Back", "ct", slug)])
    return Screen(f"\U0001f4dc *{chan['name']}* — open trades\n\n" + "\n".join(lines), rows)


def system_screen() -> Screen:
    rs = {}
    try:
        rs = db_module.get_risk_settings()
    except Exception:
        pass
    dpm = "ON" if rs.get("dpm_enabled") else "OFF"
    ime = "ON" if rs.get("ime_enabled") else "OFF"
    rows = [
        [_btn("⏸️ Pause 30m", "sys2", "pause"), _btn("▶️ Resume", "sys2", "resume")],
        [_btn(f"\U0001f504 DPM: {dpm}", "sys2", "dpm"), _btn(f"⚡ IME: {ime}", "sys2", "ime")],
        [_btn("✅ Activate pending signal", "sys2", "activate")],
        [_btn("\U0001f4e7 Email report", "sys2", "report")],
        [_btn("\U0001f501 Restart bridge", "sys2", "restartbridge"),
         _btn("\U0001f504 Restart app", "sys2", "restartapp")],
        [_btn("\U0001f7e2 Switch DEMO", "sys2", "demo"), _btn("\U0001f534 Switch LIVE", "sys2", "live")],
        [_btn("← Back to Main Menu", "root")],
    ]
    return Screen("\U0001f6e0️ *System*\n\nInfrastructure and mode controls.", rows)


# ── Trading Schedule ─────────────────────────────────────────────────────────
#
# The 7-day x 4-window gate from core_trading_schedule, driven from a phone.
# Windows are addressed by (day index, window index) rather than by a slug:
# unlike channels, the grid is a fixed 7x4 that cannot be reordered or
# renamed, so an index can never come to mean a different window between
# rendering a keyboard and the user tapping it.
#
# Per-window *strategy overrides* are deliberately not editable here -- they
# are the one part of a window that changes what a trade does rather than
# whether it happens, and picking one needs the full strategy/template list
# next to the backtest numbers on the Trading page. The screens below say
# when a window carries one so it is never a silent surprise.

_DAY_ABBR = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_SCHED_FIELDS = {
    "start":  {"label": "Start time", "hint": "24h time, e.g. 08:30"},
    "end":    {"label": "End time",   "hint": "24h time, e.g. 17:00"},
    "target": {"label": "Profit target ($)", "hint": "0 turns the target off"},
    "daily":  {"label": "Daily profit target ($)", "hint": "0 turns the target off"},
}


def _dot(flag) -> str:
    return "\U0001f7e2" if flag else "\U0001f534"


def _money(value) -> str:
    return f"${float(value or 0):g}"


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
    today = datetime.now().weekday()

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
    from forex_trader.core.core_db_channel import get_telegram_channel_names
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


# ── force_reply prompt tokens ─────────────────────────────────────────────────

# [field@target]. `target` is either a channel's 8-hex slug or one of the
# Trading Schedule's dotted tokens ("sch.3.1", "sch.daily") -- the two are
# distinguishable on sight, and handle_value_reply routes on the prefix.
_PROMPT_RE = re.compile(r"\[([A-Za-z0-9_]+)@([0-9a-z.]{1,20})\]")


def prompt_text(slug: str, field: str) -> str:
    m = _meta(field)
    chan = _channel(slug)
    where = f" ({chan['name']})" if chan else ""
    return f"Send the new value for {m['label']}{where}.\n[{field}@{slug}]"


def parse_prompt(text: str) -> Optional[tuple]:
    match = _PROMPT_RE.search(text or "")
    return (match.group(1), match.group(2)) if match else None


async def handle_value_reply(prompt: str, value_text: str) -> Screen:
    """User replied to a 'Set exact value' prompt with a typed number."""
    parsed = parse_prompt(prompt)
    if not parsed:
        return Screen(mode="noop")
    field, slug = parsed
    if slug.startswith("sch."):
        return _schedule_value_reply(field, slug, value_text.strip())
    chan = _channel(slug)
    if not chan:
        return Screen("That channel no longer exists.", mode="send")
    try:
        value = _clamp(field, _coerce(field, value_text.strip()))
    except (TypeError, ValueError):
        return Screen(f"`{value_text.strip()}` is not a valid number for "
                      f"{_meta(field)['label']}.", mode="send")
    try:
        _write_value(chan, field, value)
    except Exception as e:
        return Screen(f"Could not save: {e}", mode="send")
    return Screen(f"✅ {_meta(field)['label']} set to "
                  f"`{_fmt_value(field, value)}` for {chan['name']}.", mode="send")


# ── Callback routing ──────────────────────────────────────────────────────────

async def handle_callback(data: str, ctx: Any) -> Screen:
    """Route one inline-button tap. `ctx` is the SimulationEngine -- the panel
    reuses its existing _cmd_* implementations for the read-only and system
    screens rather than duplicating their formatting."""
    parts = (data or "").split(SEP)
    if not parts or parts[0] != CB:
        return Screen(mode="noop")
    action = parts[1] if len(parts) > 1 else "root"
    args = parts[2:]
    try:
        return await _dispatch(action, args, ctx)
    except Exception as e:
        log.warning("[Panel] %s failed: %s", action, e)
        return Screen(toast=f"Error: {e}"[:190], mode="noop")


async def _dispatch(action: str, args: list, ctx: Any) -> Screen:
    if action == "root":
        return root_screen()
    if action == "noop":
        return Screen(mode="noop")
    if action == "x":
        return Screen(mode="delete", toast="Panel closed")
    if action == "chlist":
        return _channel_list_screen(args[0])
    if action == "cs":
        return channel_settings_screen(args[0])
    if action == "ct":
        return trades_screen(args[0])
    if action == "tpm":
        return tp_menu_screen(args[0])
    if action == "tpl":
        return tp_ladder_screen(args[0], args[1])
    if action == "strat":
        return strategy_screen(args[0])
    if action == "f":
        return field_screen(args[0], args[1])
    if action == "fc":
        return choice_screen(args[0], args[1])
    if action == "tlist":
        return trade_list_screen(args[0])
    if action == "sys":
        return system_screen()
    if action == "sch":
        return schedule_screen()
    if action == "schd":
        return schedule_day_screen(int(args[0]))
    if action == "schw":
        return schedule_window_screen(int(args[0]), int(args[1]))
    if action == "schc":
        return schedule_channels_screen(int(args[0]), int(args[1]))
    if action == "schx":
        return Screen(schedule_prompt_text(int(args[0]), int(args[1]), args[2]),
                      mode="force_reply")
    if action == "sch2":
        return _toggle_schedule_enabled()
    if action == "scht":
        return _toggle_window_flag(int(args[0]), int(args[1]), args[2])
    if action == "schtc":
        return _toggle_window_channel(int(args[0]), int(args[1]), args[2])

    if action == "fa":
        return _adjust_field(args[0], args[1], args[2])
    if action == "fx":
        return Screen(prompt_text(args[0], args[1]), mode="force_reply")
    if action == "fb":
        return await _toggle_field(args[0], args[1])
    if action == "fs":
        return _set_choice(args[0], args[1], args[2])
    if action == "sset":
        return _set_strategy(args[0], args[1])
    if action == "pause":
        return _toggle_pause(args[0])
    if action == "reg_ap":
        return _approve_registration(args[0], args[1])
    if action == "reg_rj":
        return _reject_registration(args[0])

    if action in ("st", "allset", "bal", "daily", "trades"):
        return await _readonly(action, ctx)
    if action == "sys2":
        return await _system_action(args[0], ctx)

    if action == "buy":
        return await _market_order(args[0], "BUY", ctx)
    if action == "sell":
        return await _market_order(args[0], "SELL", ctx)
    if action == "delp":
        return await _delete_pending(args[0], ctx)
    if action == "rf":
        return await _risk_free(args[0], ctx)
    if action == "call":
        return await _close_channel(args[0], ctx)
    if action == "closeall":
        return await _close_all(ctx)
    if action == "tc":
        return await _close_one(args[0], ctx)
    if action == "ps":
        return await _push_sl_one(args[0], ctx)
    if action == "cur":
        return _current_settings(args[0])
    return Screen(mode="noop")


# ── Setting mutations ─────────────────────────────────────────────────────────

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
    screen = channel_settings_screen(slug)
    screen.toast = f"Strategy = {_strategy_label(strategy)}"[:190]
    return screen


def _toggle_pause(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    if chan["name"] == MANUAL:
        return Screen(toast="Manual orders are placed by hand — nothing to pause.",
                      mode="noop")
    paused = not chan["paused"]
    db_module.set_channel_paused(chan["name"], paused)
    screen = channel_settings_screen(slug)
    screen.toast = f"{chan['name']}: {'PAUSED' if paused else 'ACTIVE'}"[:190]
    return screen


# ── Registration approval (from _notify_new_registration's buttons) ──────────
# Lives here rather than in remote/server.py because this module is the one
# place that owns Telegram Screen/keyboard construction; server.py just holds
# the pending-registration/approval state these handlers read and mutate.

_REG_DURATION_LABELS = {"6m": "6 Months", "1y": "1 Year", "2y": "2 Years",
                        "3y": "3 Years", "perp": "Perpetual"}


def _resolve_pending_token(short: str):
    """Pending registrations are addressed by their token's own first 8 hex
    chars in callback_data (a full token is 64 hex chars — far too long for
    Telegram's 64-byte cap). Resolve back to the real token here."""
    from forex_trader.remote import server as _remote_server
    for tok in _remote_server._pending:
        if tok.startswith(short):
            return tok
    return None


def _record_licence_issued(token: str) -> None:
    """Mirror forex_admin.py's own post-approval step so a Telegram approval
    shows up in the admin console's Licence Manager the same way a WS-console
    approval does. No-ops cleanly if KeyGen's DB callbacks aren't registered
    (e.g. this instance isn't the one running the admin console)."""
    from forex_trader.remote import server as _remote_server
    if not _remote_server._kg_insert_fn:
        return
    tok_meta   = _remote_server._allowed_tokens.get(token, {})
    lic_key    = tok_meta.get("licence_key", "")
    machine_id = tok_meta.get("machine_id", "")
    if not (lic_key and machine_id):
        return
    try:
        already = False
        if _remote_server._kg_get_all_fn:
            already = any(r.get("licence_key") == lic_key
                          for r in _remote_server._kg_get_all_fn())
        if already:
            return
        plat = tok_meta.get("platform", "")
        os_str = ("macOS" if plat == "darwin" else
                  "Windows" if "win" in plat.lower() else plat or "Unknown")
        _remote_server._kg_insert_fn({
            "email":           tok_meta.get("email", ""),
            "registration_id": machine_id,
            "sha256":          "",
            "machine_model":   os_str,
            "hostname":        tok_meta.get("hostname", ""),
            "macos_version":   os_str,
            "licence_key":     lic_key,
            "expiry_date":     tok_meta.get("expiry_date", ""),
            "licence_type":    tok_meta.get("subscription_type", ""),
            "notes":           "Auto-issued via Telegram approval",
        })
        import asyncio
        asyncio.create_task(_remote_server._push_licences_to_all_admins())
    except Exception as exc:
        log.warning("[Panel] licence DB insert failed for %s: %s", token[:8], exc)


def _approve_registration(short: str, duration_code: str) -> Screen:
    import asyncio
    from forex_trader.remote import server as _remote_server

    token = _resolve_pending_token(short)
    if not token:
        return Screen(toast="Request no longer pending — maybe already handled.", mode="noop")

    pending      = _remote_server._pending.get(token, {})
    sub_type     = _REG_DURATION_LABELS.get(duration_code, "Perpetual")
    display_name = pending.get("nickname") or pending.get("hostname") or token[:8]

    ok = _remote_server.approve_registration(token, display_name, sub_type)
    if not ok:
        return Screen(toast="Approval failed — request may have expired.", mode="noop")

    _record_licence_issued(token)
    asyncio.create_task(_remote_server._push_pending_to_all_admins())
    asyncio.create_task(_remote_server._push_clients_to_all_admins())

    tok_meta = _remote_server._allowed_tokens.get(token, {})
    warn = "" if tok_meta.get("licence_key") else \
        "\n⚠️ Licence key generation failed — check the signing key is registered."
    return Screen(
        text=f"✅ Approved — {display_name}\nSubscription: {sub_type}{warn}",
        keyboard=[],
        toast=f"Approved ({sub_type})",
        mode="edit",
    )


def _reject_registration(short: str) -> Screen:
    from forex_trader.remote import server as _remote_server
    import asyncio

    token = _resolve_pending_token(short)
    if not token:
        return Screen(toast="Request no longer pending — maybe already handled.", mode="noop")

    pending = _remote_server._pending.pop(token, None) or {}
    _remote_server._save_pending()
    asyncio.create_task(_remote_server._push_pending_to_all_admins())

    name = pending.get("nickname") or pending.get("hostname") or token[:8]
    return Screen(text=f"❌ Rejected — {name}", keyboard=[], toast="Rejected", mode="edit")


def _current_settings(slug: str) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    lines = [f"\U0001f4cb *{chan['name']}* — current settings",
             f"Strategy: {_strategy_label(chan['strategy'])}",
             f"Active: {'NO (paused)' if chan['paused'] else 'YES'}"]
    if chan["template"]:
        tpl = ea_templates.get_ea_template(chan["template"]) or {}
        lines.append(f"Template: `{chan['template']}`")
        for field in ("anchors", "lot_anchor", "pendings", "lot_pending", "risk_pct",
                      "grid_step_pts", "sl_pips", "mode", "tpsl_mode", "be_mode",
                      "be_trigger", "trail_mode", "harvest_enabled", "anc_shave",
                      "sig_guard", "cancel_pending_level"):
            lines.append(f"• {_meta(field)['label']}: "
                         f"{_fmt_value(field, tpl.get(field))}")
        for label, prefix in (("Anchor", "tp"), ("Pending", "tp_pen")):
            pips = "/".join(_fmt_value(f"{prefix}1_pips", tpl.get(f"{prefix}{n}_pips"))
                            for n in range(1, ea_templates.MAX_TP_LEVELS + 1))
            pcts = "/".join(_fmt_value(f"{prefix}1_pct", tpl.get(f"{prefix}{n}_pct"))
                            for n in range(1, ea_templates.MAX_TP_LEVELS + 1))
            lines.append(f"• {label} TP pips: {pips}")
            lines.append(f"• {label} TP pcts: {pcts}")
    else:
        rs = db_module.get_risk_settings()
        for field in GLOBAL_FIELDS:
            lines.append(f"• {_meta(field)['label']}: {_fmt_value(field, rs.get(field))}")
    return Screen("\n".join(lines), mode="send")


# ── Read-only + system screens (reuse the existing command implementations) ───

async def _readonly(action: str, ctx: Any) -> Screen:
    if action in ("st", "allset"):
        return Screen(await ctx._cmd_status([]), mode="send")
    if action == "bal":
        return Screen(await ctx._cmd_balance([]), mode="send")
    if action == "daily":
        return Screen(await ctx._cmd_daily([]), mode="send")
    return Screen(await ctx._cmd_trades([]), mode="send")


async def _system_action(act: str, ctx: Any) -> Screen:
    rs = db_module.get_risk_settings()
    if act == "pause":
        return Screen(await ctx._cmd_pause(["30m"]), mode="send")
    if act == "resume":
        return Screen(await ctx._cmd_resume([]), mode="send")
    if act == "dpm":
        on = not bool(rs.get("dpm_enabled"))
        return Screen(await (ctx._cmd_dpm_on([]) if on else ctx._cmd_dpm_off([])), mode="send")
    if act == "ime":
        on = not bool(rs.get("ime_enabled"))
        return Screen(await (ctx._cmd_ime_on([]) if on else ctx._cmd_ime_off([])), mode="send")
    if act == "activate":
        return Screen(await ctx._cmd_activate([]), mode="send")
    if act == "report":
        return Screen(await ctx._cmd_report([]), mode="send")
    if act == "restartbridge":
        return Screen(await ctx._cmd_restart_bridge([]), mode="send")
    if act == "restartapp":
        return Screen(await ctx._cmd_restart_app([]), mode="send")
    if act == "demo":
        return Screen(await ctx._cmd_switch_demo([]), mode="send")
    if act == "live":
        return Screen(await ctx._cmd_switch_live([]), mode="send")
    return Screen(mode="noop")


# ── Trade operations ──────────────────────────────────────────────────────────

async def _market_order(slug: str, direction: str, ctx: Any) -> Screen:
    from forex_trader.core.core_manual_market_order import open_manual_market_order
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    tick = await ctx.get_tick()
    if tick is None:
        return Screen("No price available — is the MT5 bridge connected?", mode="send")

    # A market order needs a stop. A template channel states one (sl_pips); a
    # built-in-strategy channel does not, so fall back to the same DPM/ATR
    # path open_manual_market_order already uses when stop_loss is None
    # rather than inventing a distance here.
    stop_loss = None
    strategy = chan["strategy"]
    if chan["template"]:
        tpl = ea_templates.get_ea_template(chan["template"]) or {}
        sl_pips = float(tpl.get("sl_pips") or 0)
        if sl_pips > 0:
            price = tick.ask if direction == "BUY" else tick.bid
            stop_loss = price - sl_pips if direction == "BUY" else price + sl_pips
    try:
        result = await open_manual_market_order(
            ctx._bridge, direction,
            stop_loss=stop_loss,
            strategy=strategy or None,
            source_name=chan["name"] if chan["name"] != MANUAL else MANUAL_SOURCE,
            starting_balance=ctx._cfg.get("starting_balance", 1000.0),
            background_open_commentary=ctx._background_open_commentary,
        )
    except Exception as e:
        return Screen(f"*{direction} failed* — {e}", mode="send")
    entry = float(result.get("entry_price") or 0)
    return Screen(
        f"*{direction} placed* — {chan['name']}\n"
        f"Entry: {entry:.2f}  |  Lots: {result.get('lot_size', '?')}\n"
        f"MT5 Ticket: {result.get('mt5_ticket') or 'pending'}",
        mode="send")


async def _delete_pending(slug: str, ctx: Any) -> Screen:
    from forex_trader.core import ea_bridge as ea_mod
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    variants = [chan["name"], f"Telegram Auto ({chan['name']})"]
    marks = ",".join("?" for _ in variants)
    with db_module.db() as conn:
        rows = [db_module.row_to_dict(r) for r in conn.execute(
            f"SELECT p.* FROM vantage_pending_orders p "
            f"JOIN vantage_signals s ON s.signal_id = p.signal_id "
            f"WHERE p.status='working' AND s.source_name IN ({marks})",
            variants,
        ).fetchall()]
    if not rows:
        return Screen(toast="No working pending orders on this channel.", mode="noop")
    ea = ea_mod.get_instance()
    done, failed = 0, 0
    for row in rows:
        try:
            ok = await ea.cancel_pending_order(
                row["trade_id"], int(row.get("mt5_ticket") or 0), "panel_delete_pending")
            done += 1 if ok else 0
            failed += 0 if ok else 1
        except Exception as e:
            log.warning("[Panel] cancel pending %s failed: %s", row.get("trade_id"), e)
            failed += 1
    return Screen(f"*Delete pending* — {chan['name']}\n"
                  f"Cancelled: {done}"
                  f"{f'  |  Failed: {failed}' if failed else ''}", mode="send")


async def _risk_free(slug: str, ctx: Any) -> Screen:
    """Move every open position on this channel to breakeven (SL = entry)."""
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    trades = _channel_open_trades(chan)
    if not trades:
        return Screen(toast="No open trades on this channel.", mode="noop")
    moved, skipped = [], 0
    for t in trades:
        ticket = int(t.get("mt5_ticket") or 0)
        entry = float(t.get("entry_price") or 0)
        if not ticket or not entry:
            skipped += 1          # still a staged template leg, nothing at the broker yet
            continue
        try:
            res = await ctx._bridge.modify_order(ticket, entry, None)
            if res.get("error"):
                skipped += 1
                continue
            with db_module.db() as conn:
                conn.execute("UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                             (entry, t["trade_id"]))
            moved.append(ticket)
        except Exception as e:
            log.warning("[Panel] risk-free %s failed: %s", ticket, e)
            skipped += 1
    return Screen(f"*Risk free* — {chan['name']}\n"
                  f"SL moved to entry on {len(moved)} position(s)"
                  f"{f', {skipped} skipped' if skipped else ''}.", mode="send")


async def _close_channel(slug: str, ctx: Any) -> Screen:
    chan = _channel(slug)
    if not chan:
        return Screen(toast="That channel no longer exists.", mode="noop")
    trades = _channel_open_trades(chan)
    if not trades:
        return Screen(toast="No open trades on this channel.", mode="noop")
    return Screen(await _close_many(trades, ctx, chan["name"]), mode="send")


async def _close_all(ctx: Any) -> Screen:
    from forex_trader.core.core_trade_reporting import get_open_trades
    trades = get_open_trades()
    if not trades:
        return Screen(toast="No open trades.", mode="noop")
    return Screen(await _close_many(trades, ctx, "all channels"), mode="send")


async def _close_many(trades: list, ctx: Any, label: str) -> str:
    lines = [f"*Closing {len(trades)} trade(s)* — {label}"]
    total = 0.0
    for t in trades:
        try:
            res = await ctx.close_trade(t["trade_id"], "manual_close")
            pnl = float(res.get("net_pnl", 0))
            total += pnl
            lines.append(f"{t.get('direction')} {t.get('lot_size')} @ "
                         f"{float(res.get('close_price', 0)):.2f}  "
                         f"P&L: {'+' if pnl >= 0 else ''}{pnl:.2f}")
        except Exception as e:
            lines.append(f"Failed {t.get('mt5_ticket') or t['trade_id'][:8]}: {e}")
    lines.append(f"Total P&L: {'+' if total >= 0 else ''}${total:.2f}")
    return "\n".join(lines)


async def _close_one(trade_prefix: str, ctx: Any) -> Screen:
    with db_module.db() as conn:
        row = db_module.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id LIKE ? AND status='open'",
            (trade_prefix + "%",),
        ).fetchone())
    if not row:
        return Screen(toast="That trade is no longer open.", mode="noop")
    try:
        res = await ctx.close_trade(row["trade_id"], "manual_close")
    except Exception as e:
        return Screen(f"Close failed: {e}", mode="send")
    pnl = float(res.get("net_pnl", 0))
    return Screen(f"*Closed* {row.get('direction')} {row.get('lot_size')} @ "
                  f"{float(res.get('close_price', 0)):.2f}\n"
                  f"P&L: {'+' if pnl >= 0 else ''}${pnl:.2f}", mode="send")


async def _push_sl_one(trade_prefix: str, ctx: Any) -> Screen:
    """manual_sl_push_pips / tg_cmd_enabled (2026-08-04 -- existed as
    template fields with no bot-command infrastructure to wire into at all;
    the old typed /commands were retired in favour of this button panel, so
    this is that panel's version rather than a new typed command). Nudges
    an EA Template trade's live broker SL by the template's own configured
    pip amount, same direct-modify pattern _risk_free above already uses
    for its own manual SL move, gated on tg_cmd_enabled per template."""
    with db_module.db() as conn:
        row = db_module.row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id LIKE ? AND status='open'",
            (trade_prefix + "%",),
        ).fetchone())
    if not row:
        return Screen(toast="That trade is no longer open.", mode="noop")

    push_pips = _trade_push_sl_pips(row)
    if push_pips <= 0:
        return Screen(toast="Push SL isn't available for this trade.", mode="noop")

    ticket = int(row.get("mt5_ticket") or 0)
    if not ticket:
        return Screen(toast="That leg hasn't filled yet.", mode="noop")

    positions = await ctx._bridge.get_positions()
    pos = next((p for p in positions if int(p.get("ticket") or 0) == ticket), None)
    if not pos:
        return Screen(toast="Couldn't read this position from the broker.", mode="noop")

    from forex_trader.core.core_pips import PIPS_TO_PRICE_XAUUSD
    direction  = row.get("direction", "BUY")
    cur_sl     = float(pos.get("sl") or 0)
    cur_price  = float(pos.get("current_price") or 0)
    push_dist  = push_pips * PIPS_TO_PRICE_XAUUSD
    new_sl     = (cur_sl + push_dist) if direction == "BUY" else (cur_sl - push_dist)

    # A push that would land at or past current price is an invalid stop,
    # not a tighter one -- refuse rather than send a request the broker
    # would reject anyway (or, worse, one it fills as an instant close).
    landed_past_price = (
        (direction == "BUY" and new_sl >= cur_price) or
        (direction == "SELL" and new_sl <= cur_price)
    )
    if cur_sl <= 0 or landed_past_price:
        return Screen(toast="Push SL would land at/past current price — refused.", mode="noop")

    try:
        res = await ctx._bridge.modify_order(ticket, round(new_sl, 2), None)
        if res.get("error"):
            return Screen(f"Push SL failed: {res['error']}", mode="send")
    except Exception as e:
        return Screen(f"Push SL failed: {e}", mode="send")

    with db_module.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                     (round(new_sl, 2), row["trade_id"]))
    return Screen(f"*SL pushed* {row.get('direction')} {row.get('lot_size')} — "
                  f"new SL ${new_sl:.2f} (+{push_pips:.1f} pips)", mode="send")

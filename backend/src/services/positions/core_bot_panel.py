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
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.risk import schedule as schedule_mod
from backend.src.db import database as db_module
from backend.src.utils.models import STRATEGY_NAMES
from backend.src.services.positions import panel_repo

# The panel's own modules. Split for the 800-line ceiling; the dispatch
# table below still names every screen and action, which is the point.
# Re-exported wholesale rather than only what this module calls: core_bot_panel
# is the name every caller and test already imports, and a partial surface just
# moves the breakage to whoever reaches for the rest.
from backend.src.services.positions._panel_registration import (  # noqa: F401
    _REG_DURATION_LABELS,
    _approve_registration,
    _record_licence_issued,
    _reject_registration,
    _resolve_pending_token,
)
from backend.src.services.positions._panel_schedule import (  # noqa: F401
    _DAY_ABBR,
    _SCHED_FIELDS,
    _TIME_RE,
    _save_block,
    _sched_block,
    _sched_blocks,
    _schedule_value_reply,
    _telegram_channel_names,
    _toggle_schedule_enabled,
    _toggle_window_channel,
    _toggle_window_flag,
    _window_channel_enabled,
    schedule_channels_screen,
    schedule_day_screen,
    schedule_prompt_text,
    schedule_screen,
    schedule_window_screen,
)
from backend.src.services.positions._panel_settings import (  # noqa: F401
    FIELDS,
    GLOBAL_FIELDS,
    _active_btn,
    _adjust_field,
    _basic_settings_screen,
    _choices,
    _clamp,
    _coerce,
    _field_btn,
    _field_parent,
    _fmt_value,
    _meta,
    _parent_screen,
    _read_value,
    _set_choice,
    _set_strategy,
    _template_settings_screen,
    _toggle_field,
    _write_value,
    choice_screen,
    field_screen,
    strategy_screen,
    tp_ladder_screen,
    tp_menu_screen,
)
from backend.src.services.positions._panel_shared import (  # noqa: F401
    CB,
    MANUAL,
    MANUAL_SOURCE,
    SEP,
    Screen,
    _btn,
    _cb,
    _channel,
    _channel_open_trades,
    _channel_rec,
    _dot,
    _money,
    _short,
    _slug,
    _strategy_label,
    _trade_push_sl_pips,
    channel_list,
)
from backend.src.services.positions._panel_trade_ops import (  # noqa: F401
    _close_all,
    _close_channel,
    _close_many,
    _close_one,
    _delete_pending,
    _market_order,
    _push_sl_one,
    _risk_free,
)

log = logging.getLogger(__name__)

# ── Callback-data helpers ─────────────────────────────────────────────────────

# ── Channels ──────────────────────────────────────────────────────────────────

# ── Template field metadata ───────────────────────────────────────────────────
#
# kind: 'float' | 'int' | 'bool' | 'choice'. `step` is the -/+ increment.
# Only fields worth driving from a phone are listed -- the full set stays in
# Settings > EA Templates. Adding a field here is enough to make it editable;
# the generic field editor handles the rest.

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

# ── Value storage ─────────────────────────────────────────────────────────────

# ── Screens ───────────────────────────────────────────────────────────────────

def root_screen() -> Screen:
    kb = [
        [_btn("\U0001f4ca Status", "st"), _btn("\U0001f4cb All Settings", "allset")],
        [_btn("⚙️ Channel Strategy", "chlist", "s"),
         _btn("\U0001f3af Channel Trades", "chlist", "t")],
        # Balance absorbed the old Daily button (2026-08-08) -- it now opens
        # with the account and today's P&L, then this week by day and the
        # month, so a separate Daily screen would answer the same question
        # with its own copy of the arithmetic.
        [_btn("\U0001f4b5 Balance", "bal"), _btn("\U0001f4dc Open Trades", "trades")],
        [_btn("\U0001f6e0️ System", "sys"), _btn("\U0001f5d3️ Trading Schedule", "sch")],
        # Labelled with the state it is in, not just the action: a panel that
        # says "Pause Trading" while trading is already paused is how you end
        # up believing you are flat when you are not.
        [_btn(_pause_root_label(), "pt")],
        [_btn("⛔ CLOSE ALL TRADES", "closeall", "*")],
        [_btn("❌ Close Control Panel", "x")],
    ]
    return Screen("\U0001f4b0 *FOREX Control Panel*\n\nChoose an action from the buttons below:", kb)


def _pause_root_label() -> str:
    paused, until_str = _pause_state()
    return f"⏸️ PAUSED until {until_str} — tap to change" if paused else "⏸️ Pause Trading"


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
    # immediate_market_entry, not ime_enabled: the latter is not a column and
    # rs.get() returned None for it, so this line always said OFF and the
    # toggle below could only ever switch IME on. docs/todo/bugs/012.
    ime = "ON" if rs.get("immediate_market_entry") else "OFF"
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


# ── Pause Trading ────────────────────────────────────────────────────────────
#
# Pausing until a session boundary, rather than for a duration, is what you
# actually want on a phone: "stop until London closes" is a decision about the
# market, and working out that it is 4h17m away is exactly the arithmetic you
# don't want to be doing to make it.
#
# Boundaries are UTC hours taken from dpm_engine.detect_session's own
# partition -- the same one is_session_allowed() and the analytics heat map
# read -- so "end of London" here means precisely what the rest of the app
# calls the end of London, and cannot drift from it:
#
#     asian   21:00-07:00      london  07:00-12:00
#     overlap 12:00-16:00      ny      16:00-21:00
#
# The pause itself reuses trade_pause_until (the /pause command's own key),
# so this adds a way to choose the moment, not a second pause mechanism with
# its own semantics to keep in step.
_SESSION_END_UTC = {
    "as": ("Asian",   7),
    "lo": ("London",  12),
    "ov": ("Overlap", 16),
    "ny": ("NY",      21),
}

# Order shown on the panel: soonest-ending session first is tempting, but the
# boundary that is soonest changes through the day and a keyboard whose
# buttons move is one you mis-tap. Fixed session order instead.
_SESSION_ORDER = ("ny", "ov", "lo", "as")


def _next_utc_hour(hour: int, now: Optional[datetime] = None) -> float:
    """Unix timestamp of the next time it is `hour`:00 UTC.

    Strictly in the future: at exactly 12:00:00 UTC, "end of London" means
    tomorrow's, not a zero-length pause that lifts on the same tick.
    """
    now = now or datetime.now(timezone.utc)
    target = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return target.timestamp()


def _pause_state() -> tuple[bool, str]:
    """(paused, 'HH:MM' local) for the current trade_pause_until value."""
    try:
        until = float(db_module.get_app_config("trade_pause_until") or 0)
    except (TypeError, ValueError):
        return False, ""
    if until <= time.time():
        return False, ""
    return True, datetime.fromtimestamp(until).strftime("%H:%M")


def pause_trading_screen() -> Screen:
    rows = []
    for key in _SESSION_ORDER:
        label, hour = _SESSION_END_UTC[key]
        # Each button states when it lands in the reader's own clock -- the
        # boundaries are defined in UTC and this panel is used from a phone
        # in whatever timezone it happens to be in.
        local = datetime.fromtimestamp(_next_utc_hour(hour)).strftime("%H:%M")
        rows.append([_btn(f"⏸ End of {label}  ({local})", "ptu", key)])
    rows.append([_btn("⏱ Custom (hrs)", "ptc")])

    paused, until_str = _pause_state()
    if paused:
        rows.append([_btn("▶️ Resume trading now", "ptr")])
    rows.append([_btn("← Back to Main Menu", "root")])

    head = (f"⏸️ *Pause Trading*\n\nCurrently *PAUSED* until {until_str}."
            if paused else
            "⏸️ *Pause Trading*\n\nTrading is currently *active*.")
    return Screen(
        f"{head}\n\nPause until the end of a session, or set your own length.\n"
        "_Times shown are your local clock._",
        rows,
    )


def _pause_until_session(key: str) -> Screen:
    entry = _SESSION_END_UTC.get(key)
    if not entry:
        return Screen(toast="Unknown session.", mode="noop")
    label, hour = entry
    until = _next_utc_hour(hour)
    db_module.set_app_config("trade_pause_until", str(until))
    local = datetime.fromtimestamp(until).strftime("%H:%M")
    hrs = (until - time.time()) / 3600.0
    log.info("[Panel] trading paused until end of %s (%s local, %.1fh)", label, local, hrs)
    return Screen(
        f"⏸️ Trading paused until the end of *{label}* — {local} "
        f"({hrs:.1f}h from now).\n\nTap Resume, or send /resume, to lift it early.",
        mode="send",
    )


def _resume_trading() -> Screen:
    db_module.set_app_config("trade_pause_until", "0")
    # See cmd_resume: without re-arming, a resume after a give-back halt is
    # undone by the next close.
    try:
        from backend.src.services.risk.governor import rearm_risk_guards
        rearm_risk_guards()
    except Exception:
        pass
    log.info("[Panel] trading resumed from the pause panel")
    return Screen("▶️ Trading resumed.", mode="send")


def pause_prompt_text() -> str:
    return ("Send the number of hours to pause trading (e.g. 2, or 1.5).\n"
            "[hrs@pause]")


def _pause_custom_reply(raw: str) -> Screen:
    try:
        hours = float(raw.strip().lower().rstrip("h").strip())
    except ValueError:
        return Screen(f"`{raw}` is not a number of hours.", mode="send")
    # Upper bound is a week: a pause is a decision to sit out a session or a
    # day, and a typo'd 240 that silently parks trading until next month is
    # not a pause anyone meant to set.
    if not (0 < hours <= 168):
        return Screen("Hours must be between 0 and 168 (one week).", mode="send")
    until = time.time() + hours * 3600.0
    db_module.set_app_config("trade_pause_until", str(until))
    local = datetime.fromtimestamp(until).strftime("%H:%M")
    log.info("[Panel] trading paused for %.2fh (until %s local)", hours, local)
    return Screen(f"⏸️ Trading paused for *{hours:g}h* — until {local}.\n\n"
                  "Send /resume to lift it early.", mode="send")


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
    if slug == "pause":
        return _pause_custom_reply(value_text)
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
    if action == "pt":
        return pause_trading_screen()
    if action == "ptu":
        return _pause_until_session(args[0])
    if action == "ptc":
        return Screen(pause_prompt_text(), mode="force_reply")
    if action == "ptr":
        return _resume_trading()
    if action == "sset":
        return _set_strategy(args[0], args[1])
    if action == "pause":
        return _toggle_pause(args[0])
    if action == "reg_ap":
        return _approve_registration(args[0], args[1])
    if action == "reg_rj":
        return _reject_registration(args[0])

    if action in ("st", "allset", "bal", "trades"):
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
        on = not bool(rs.get("immediate_market_entry"))
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


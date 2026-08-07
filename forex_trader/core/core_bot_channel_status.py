"""Per-channel status blocks for the Telegram panel's Status button.

The panel's Status screen used to report the system as a whole (strategy,
risk, open count) plus a one-line-per-slot list of which Telegram groups the
reader was attached to. That answered "is the app alive" but not the question
actually asked from a phone: *what will each channel do with the next signal
it gets* -- which lots, how many legs, which TP ladder, whether the grid/BE/
trail switches are the ones that were set last night.

So this module renders one block per Telegram channel in the same shape the
reference copier's own panel uses (the layout the blocks below deliberately
mirror), reading each channel's bound EA Template. A channel on a built-in
Python strategy has none of those fields -- it gets a short block naming its
strategy rather than a grid of invented values, the same distinction
core_bot_panel draws between its template and basic settings screens.

Numbering: "CHANNEL 1 (C1)" is the Telegram reader's own slot number, so it
matches the C1/C2 the copier and the EA's own comments use for the same
group. A configured channel with no live reader slot (reader not started, or
a channel whose group was removed) falls back to its position in the channel
list, so the block still renders rather than vanishing.

Read-only: nothing here places, closes, modifies or configures anything.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core import database as db_module
from forex_trader.core.core_db_channel import (
    canonical_channel_name,
    get_telegram_channel_names,
)
from forex_trader.core.models import STRATEGY_NAMES

log = logging.getLogger(__name__)

# Width the four TP-ladder labels are padded to so their values start in one
# column. "Take Profits Pips (Pending):" is the longest at 28.
_LADDER_COL = 28


def _num(value) -> str:
    """Trim a stored float to how it was typed: 20.0 -> '20', 12.5 -> '12.5'."""
    try:
        return f"{float(value):g}"
    except (TypeError, ValueError):
        return str(value)


def _onoff(flag) -> str:
    return "ON" if flag else "OFF"


def _md(text: str) -> str:
    """Telegram Markdown v1 has no reliable escape for '*', and an unpaired
    '_' in a channel name ('GOLD_DIGGERS') 400s the whole send -- which costs
    the formatting of every other block. Same treatment cmd_daily already
    gives strategy names."""
    return str(text or "").replace("_", "\\_").replace("*", "")


def _strategy_label(strategy: Optional[str]) -> str:
    if not strategy:
        return "inherit global"
    if strategy == "auto":
        return "Auto (AI)"
    if ea_templates.is_template_override(strategy):
        return f"Template: {ea_templates.template_name_from_override(strategy)}"
    return STRATEGY_NAMES.get(strategy, strategy)


def _slot_map(tg_reader: Optional[Any]) -> dict[str, dict]:
    """{canonical channel name: {'slot': n, 'active': bool}} from the live
    reader. Empty when the reader isn't running -- callers fall back to list
    position for the C-number and omit the feed line entirely."""
    out: dict[str, dict] = {}
    if tg_reader is None:
        return out
    try:
        status = tg_reader.get_status() or {}
    except Exception as e:
        log.debug("[Status] telegram reader status unavailable: %s", e)
        return out
    for slot in status.get("slots") or []:
        name = slot.get("group_name")
        if not name:
            continue
        out[canonical_channel_name(str(name))] = {
            "slot":   slot.get("slot"),
            "active": bool(slot.get("listener_active") or slot.get("poller_active")),
        }
    return out


def _ladder(tpl: dict, prefix: str) -> tuple[str, str]:
    """The pips and close-% rows for one ladder, trimmed to the deepest level
    that is actually configured -- printing all 8 columns when 4 are set
    reports six trailing zeros as if they were targets."""
    levels = range(1, ea_templates.MAX_TP_LEVELS + 1)
    pips = [float(tpl.get(f"{prefix}{n}_pips") or 0) for n in levels]
    pcts = [float(tpl.get(f"{prefix}{n}_pct") or 0) for n in levels]
    depth = 0
    for i, (a, b) in enumerate(zip(pips, pcts), 1):
        if a or b:
            depth = i
    if not depth:
        return "not set", "not set"
    return ("/".join(_num(v) for v in pips[:depth]),
            "/".join(_num(v) for v in pcts[:depth]))


def _template_block(tpl: dict, paused: bool) -> list[str]:
    lines = [
        f"  • 💰 Lots: Anchor = {float(tpl.get('lot_anchor') or 0):.2f} | "
        f"Pending = {float(tpl.get('lot_pending') or 0):.2f}",
        f"  • 🎯 Entries: Anchor Count = {int(tpl.get('anchors') or 0)} | "
        f"Pendings = {int(tpl.get('pendings') or 0)} | "
        f"SL = {float(tpl.get('sl_pips') or 0):.1f} pips",
    ]

    for label, prefix, tg_field in (("Anchor", "tp", "tp_from_telegram"),
                                    ("Pending", "tp_pen", "tp_pen_from_telegram")):
        pips, pcts = _ladder(tpl, prefix)
        # With "TP levels from Telegram" on, a Telegram signal's own stated
        # targets replace this column -- so print it in brackets rather than
        # as the levels the next signal will actually use. The pips still
        # apply to the internal generators, which have no message to read.
        if tpl.get(tg_field):
            pips = f"({pips}) — from signal"
        lines.append(f"  • 🏁 {f'Take Profits Pips ({label}):'.ljust(_LADDER_COL)}{pips}")
        lines.append(f"  • 🏁 {f'TP Close Pcts ({label}):'.ljust(_LADDER_COL)}{pcts}")

    lines.append("  • ⚙️ Settings:")
    lines.append(
        f"      └─ Harvest = {_onoff(tpl.get('harvest_enabled'))} | "
        f"Grid Mode = {_onoff(str(tpl.get('mode') or '') == 'grid')} | "
        f"TP = {str(tpl.get('tpsl_mode') or 'off').upper()}"
    )
    lines.append(
        f"      └─ Active = {_onoff(not paused)} | "
        f"BE = {str(tpl.get('be_mode') or 'entry').upper()} | "
        f"Trail = {str(tpl.get('trail_mode') or 'off').upper()}"
    )

    lines.append("  • ⚡️ Trigger:")
    cancel = int(tpl.get("cancel_pending_level") or 0)
    lines.append(
        f"      └─ BreakEven = TP{int(tpl.get('be_trigger') or 1)} | "
        f"Delete Pending = {f'TP{cancel}' if cancel else 'OFF'}"
    )
    guard_pips = float(tpl.get("sig_guard_pips") or 0)
    if not tpl.get("sig_guard"):
        guard = "OFF"
    elif guard_pips > 0:
        guard = f"{_num(guard_pips)} pips"
    else:
        # sig_guard_pips 0 is the original all-or-nothing guard, not "no
        # guard" -- saying "0 pips" would read as the opposite of what it does.
        guard = "ON (any same-direction trade)"
    lines.append(f"      └─ SIG GUARD = {guard}")
    risk = float(tpl.get("risk_pct") or 0)
    lines.append(f"      └─ Risk = {'OFF' if risk <= 0 else f'{_num(risk)}%'}")
    return lines


def _basic_block(strategy: Optional[str], paused: bool) -> list[str]:
    return [
        f"  • 🎛️ Strategy: {_md(_strategy_label(strategy))}",
        f"  • ⚙️ Settings: Active = {_onoff(not paused)}",
        "      └─ No EA Template bound, so lots, ladder and grid/BE/trail "
        "settings come from the strategy itself.",
    ]


def channel_block(name: str, number: int, feed_active: Optional[bool] = None) -> list[str]:
    """One channel's block. `feed_active` None omits the feed line (reader
    not running), rather than claiming the feed is idle."""
    strategy = None
    try:
        strategy = db_module.get_channel_strategy_override(name)
    except Exception as e:
        log.debug("[Status] strategy lookup failed for %s: %s", name, e)
    paused = False
    try:
        _mult, paused = db_module.get_channel_lot_mult(name)
    except Exception as e:
        log.debug("[Status] pause lookup failed for %s: %s", name, e)

    lines = [f"📢 *CHANNEL {number}* (C{number}) (Name: {_md(name)})"]
    if feed_active is not None:
        lines.append(f"  • 📡 Feed: {'listening' if feed_active else 'idle'}")

    tpl = None
    if ea_templates.is_template_override(strategy):
        tpl = ea_templates.get_ea_template(
            ea_templates.template_name_from_override(strategy))
    if tpl is None:
        lines.extend(_basic_block(strategy, paused))
    else:
        lines.extend(_template_block(tpl, paused))
    return lines


def channel_status_lines(tg_reader: Optional[Any] = None) -> list[str]:
    """Every configured Telegram channel, one block each, blank-line separated."""
    try:
        names = get_telegram_channel_names()
    except Exception as e:
        log.warning("[Status] channel list unavailable: %s", e)
        return []
    if not names:
        return ["_No Telegram channels are configured._"]

    slots = _slot_map(tg_reader)
    lines: list[str] = []
    for i, name in enumerate(names, 1):
        info = slots.get(name) or {}
        if lines:
            lines.append("")
        lines.extend(channel_block(name, int(info.get("slot") or i),
                                   info.get("active")))
    return lines

"""TP/SL in Second Message (Parsing Settings, 2026-07-31).

Some channels post the entry first ("XAU USD SELL NOW / 4150.5 - 4155.5")
and its Stop Loss and targets in a separate message moments later. Two
narrower mechanisms for this already existed and neither covers the general
case:

  - parse_gd2_partial + handle_signal_edit: completion only via a Telegram
    *edit* to the same message, GD2 layouts only, and no timeout at all --
    an edit that never comes leaves the row waiting forever.
  - Immediate Market Entry: matches a genuinely separate follow-up message,
    but only after having already entered at market, so it modifies a live
    trade rather than completing an unexecuted signal.

This one holds the unexecuted signal, completes it from a *separate* later
message in any supported format, and expires on a configurable window
(lk_second_message_match_window_sec, default 120s), executing bare on
timeout -- the same outcome the app already produces for a signal that
genuinely has no SL/TP, rather than silently dropping a real entry.

The hold is re-evaluated by _scan_messages re-reading the still-buffered
bare message each cycle, which is why nothing here writes to
vantage_tg_signals: a row there marks the message as seen and would stop
that re-read. Nothing in this module places, closes or modifies an order.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from backend.src.db import database as db_module

log = logging.getLogger(__name__)

_DEFAULT_WINDOW_SEC = 120


def match_window_sec(rs: dict) -> int:
    """Configured hold window, floored at 1s -- a 0 saved into the settings
    row would otherwise expire every hold instantly, turning the feature
    into a silent no-op that still looks enabled in the UI."""
    try:
        return max(1, int(rs.get("lk_second_message_match_window_sec", _DEFAULT_WINDOW_SEC)))
    except (TypeError, ValueError):
        return _DEFAULT_WINDOW_SEC


def is_enabled(rs: dict) -> bool:
    return bool(rs.get("lk_enable_second_message_tp_sl", 0))


def _get_hold(tg_id: str) -> Optional[dict]:
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT * FROM vantage_second_message_holds WHERE tg_message_id=? AND status='waiting'",
            (tg_id,),
        ).fetchone()
    return db_module.row_to_dict(row) if row else None


def attach_followup(channel_name: str, levels: dict) -> Optional[str]:
    """Applies a TP/SL-only message's levels to the newest still-waiting hold
    on the same channel. Returns that hold's tg_message_id, or None if the
    channel has nothing waiting (the overwhelmingly common case -- most
    SL/TP-shaped chatter isn't completing anything).

    Only the newest hold is completed: if a channel posted two bare entries
    back to back, a single follow-up belongs to the most recent one, and
    fanning it out across both would invent levels for a signal that never
    got any."""
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT tg_message_id FROM vantage_second_message_holds "
            "WHERE channel_name=? AND status='waiting' AND levels_json IS NULL "
            "ORDER BY first_seen_at DESC LIMIT 1",
            (channel_name,),
        ).fetchone()
        if not row:
            return None
        conn.execute(
            "UPDATE vantage_second_message_holds SET levels_json=? WHERE tg_message_id=?",
            (json.dumps(levels), row[0]),
        )
    log.info("[SecondMessage] %s — follow-up levels attached to held signal tg_id=%s",
             channel_name, row[0])
    return row[0]


def hold_or_resolve(
    tg_id: str, channel_name: str, partial: dict, rs: dict,
) -> Optional[dict]:
    """Called each time the still-buffered bare signal is re-scanned.

    Returns a complete signal dict once the hold resolves -- either merged
    with a follow-up's levels, or bare after the window expires -- and None
    while it is still waiting, in which case the caller drops the message for
    this cycle and picks it up again on the next one."""
    hold = _get_hold(tg_id)
    if hold is None:
        with db_module.db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO vantage_second_message_holds "
                "(tg_message_id, channel_name, partial_json, first_seen_at, status) "
                "VALUES (?,?,?,?,'waiting')",
                (tg_id, channel_name, json.dumps(partial), time.time()),
            )
        log.info("[SecondMessage] %s — holding bare %s signal tg_id=%s (entry %s-%s) for up to %ds",
                 channel_name, partial.get("direction"), tg_id,
                 partial.get("entry_low"), partial.get("entry_high"), match_window_sec(rs))
        return None

    # Seeded from the bare message's own levels so a partial that quoted,
    # say, its SL but no targets keeps that SL; the follow-up below only
    # fills the gaps it actually carries values for.
    signal = {
        "direction":  partial["direction"],
        "entry_low":  partial["entry_low"],
        "entry_high": partial["entry_high"],
        "stop_loss":  partial.get("stop_loss"),
        **{f"tp{i}": partial.get(f"tp{i}") for i in range(1, 9)},
    }
    if "tp_open" in partial:
        signal["tp_open"] = partial["tp_open"]

    if hold.get("levels_json"):
        try:
            levels = json.loads(hold["levels_json"])
        except Exception:
            levels = {}
        for key in ("stop_loss", *(f"tp{i}" for i in range(1, 9))):
            if levels.get(key) is not None:
                signal[key] = levels[key]
        if levels.get("tp_open") and "tp_open" in signal:
            signal["tp_open"] = True
        _resolve(tg_id)
        log.info("[SecondMessage] %s — tg_id=%s completed from follow-up (SL=%s TP1=%s)",
                 channel_name, tg_id, signal["stop_loss"], signal["tp1"])
        return signal

    if time.time() - float(hold["first_seen_at"]) >= match_window_sec(rs):
        _resolve(tg_id)
        log.info("[SecondMessage] %s — tg_id=%s window expired with no follow-up, executing bare",
                 channel_name, tg_id)
        return signal

    return None


def _resolve(tg_id: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_second_message_holds SET status='resolved' WHERE tg_message_id=?",
            (tg_id,),
        )

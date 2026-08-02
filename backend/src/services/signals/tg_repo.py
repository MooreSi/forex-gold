"""Telegram signal history reads -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine.get_tg_signals, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-tg-signals-migration/020-*.md.

Takes `tg_reader` as an explicit optional parameter (anything exposing
get_group_name(group_id: str) -> Optional[str], matching TelegramReader's
real shape) instead of reading self._tg_reader.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.src.db import database as db_module


def get_tg_signals(limit: int = 50, tg_reader: Optional[Any] = None) -> list[dict]:
    with db_module.db() as conn:
        rows = conn.execute(
            # Show all signals — historical/instant_historical are displayed
            # with a grey badge so the user can see what was received during
            # a restart backfill even if it was too old to execute.
            # instant_historical records (bare "Buy Now" messages) are excluded
            # as they are low-value noise.
            "SELECT * FROM vantage_tg_signals "
            "WHERE status != 'instant_historical' "
            "ORDER BY parsed_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
    result = [db_module.row_to_dict(r) for r in rows]
    # Resolve missing group names from TG reader
    for r in result:
        if not r.get("group_name") and r.get("group_id") and tg_reader:
            name = tg_reader.get_group_name(str(r["group_id"]))
            if name:
                r["group_name"] = name
    return result


# ── Writes collected from the scan/edit/activation paths (M1 SQL sweep) ──────
# Verbatim statements; the callers run them from the same places in the same
# order they always did.
import time as _time


def set_raw_text(tg_id, text: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_tg_signals SET raw_text=? WHERE tg_message_id=?",
            (text, tg_id),
        )


def correct_direction_and_text(tg_id, direction: str, text: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_tg_signals SET direction=?, raw_text=? "
            "WHERE tg_message_id=?",
            (direction, text, tg_id),
        )


def update_reparsed_fields(tg_id, text: str, reparse: dict) -> None:
    """Refresh levels from a successful re-parse of an edited message.

    A signal parked in pending_followup goes back to 'new' -- the edit
    supplied the levels the follow-up was waiting for.
    """
    with db_module.db() as conn:
        conn.execute(
            """UPDATE vantage_tg_signals
               SET raw_text=?, entry_low=?, entry_high=?, stop_loss=?,
                   tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?, tp7=?, tp8=?,
                   status=CASE WHEN status='pending_followup' THEN 'new' ELSE status END
               WHERE tg_message_id=?""",
            (text,
             reparse["entry_low"], reparse["entry_high"], reparse["stop_loss"],
             reparse.get("tp1"), reparse.get("tp2"), reparse.get("tp3"),
             reparse.get("tp4"), reparse.get("tp5"), reparse.get("tp6"),
             reparse.get("tp7"), reparse.get("tp8"),
             tg_id),
        )


def apply_direction_correction(tg_id, reparse: dict, text: str) -> None:
    """An edit flipped the direction while the signal was still 'new'."""
    with db_module.db() as conn:
        conn.execute(
            """UPDATE vantage_tg_signals
               SET direction=?, entry_low=?, entry_high=?, stop_loss=?,
                   tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?, tp7=?, tp8=?,
                   raw_text=?
               WHERE tg_message_id=?""",
            (
                reparse["direction"],
                reparse["entry_low"], reparse["entry_high"],
                reparse["stop_loss"],
                reparse.get("tp1"), reparse.get("tp2"), reparse.get("tp3"),
                reparse.get("tp4"), reparse.get("tp5"), reparse.get("tp6"),
                reparse.get("tp7"), reparse.get("tp8"),
                text,
                tg_id,
            ),
        )


def activate_pending_tg_signal(signal_id: str) -> None:
    """Flip the TG signal row 'pending' -> 'activated' so the UI updates."""
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_tg_signals SET status='activated'"
            " WHERE signal_id=? AND status='pending'",
            (signal_id,),
        )


def record_unsupported_currency(tg_id, group_id, channel_name: str,
                                sender_name: str, msg_ts_str, text: str,
                                direction, dup_window: float):
    """Log an unsupported-currency signal; report recent same-direction peers.

    Returns (was_new, recent_rows). The SELECT only runs when the INSERT
    actually landed (rowcount > 0), on the same connection, exactly as the
    inline block did.
    """
    with db_module.db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO vantage_tg_signals
               (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, group_id, channel_name,
             sender_name, msg_ts_str,
             text, _time.time(),
             direction, None, None, None,
             None, None, None, None, None, None, None, None,
             "unsupported_currency"),
        )
        was_new = cur.rowcount > 0
        if was_new:
            recent_rows = conn.execute(
                """SELECT raw_text FROM vantage_tg_signals
                   WHERE group_id=? AND status='unsupported_currency'
                   AND direction=? AND tg_message_id!=? AND parsed_at>?""",
                (group_id, direction, tg_id, _time.time() - dup_window),
            ).fetchall()
        else:
            recent_rows = []
    return was_new, recent_rows


def insert_tg_signal_if_new(tg_id, group_id, channel_name: str,
                            sender_name: str, msg_ts_str, text: str,
                            levels: dict, status: str) -> bool:
    """INSERT OR IGNORE a tg-signal row; True when the row was actually new.

    One statement shared by the parse/staleness paths -- they differ only in
    which levels are known (a GD2 partial has no SL/TPs yet) and the status
    they park the row in ('pending_followup', 'historical', 'new').
    """
    with db_module.db() as conn:
        cur = conn.execute(
            """INSERT OR IGNORE INTO vantage_tg_signals
               (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, group_id, channel_name, sender_name, msg_ts_str,
             text, _time.time(),
             levels.get("direction"), levels.get("entry_low"),
             levels.get("entry_high"), levels.get("stop_loss"),
             levels.get("tp1"), levels.get("tp2"), levels.get("tp3"),
             levels.get("tp4"), levels.get("tp5"), levels.get("tp6"),
             levels.get("tp7"), levels.get("tp8"),
             status),
        )
        return cur.rowcount > 0


def delete_tg_signal_row(row_id) -> None:
    with db_module.db() as conn:
        conn.execute("DELETE FROM vantage_tg_signals WHERE id=?", (row_id,))


def get_tg_signal_meta(tg_id):
    """The scan loop's dedup probe: id/direction/status/text/entry for an
    already-seen message, or None."""
    with db_module.db() as conn:
        return conn.execute(
            "SELECT id, direction, status, raw_text, entry_low FROM vantage_tg_signals "
            "WHERE tg_message_id=?", (tg_id,)
        ).fetchone()

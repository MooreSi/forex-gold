"""Learned Rules — split from core/database.py.
Extracted from forex_trader/core/database.py -- see
docs/todo/refactor/core-database-migration/. Verbatim port: same functions,
same SQL, same behavior, using database.py's own db()/to_db_thread()
machinery (unchanged, already correct -- this is a pure file-size split,
not a connection-layer migration). Re-exported from database.py so every
existing `db_module.<name>` call site works completely unchanged.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from backend.src.db.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402


# ── Learned rules ─────────────────────────────────────────────────────────────

def get_channel_learned_rules(channel_name: Optional[str] = None) -> list[dict]:
    try:
        with db() as conn:
            if channel_name:
                rows = conn.execute(
                    "SELECT * FROM channel_learned_rules WHERE channel_name=? ORDER BY id",
                    (channel_name,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM channel_learned_rules ORDER BY channel_name, id"
                ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def save_channel_learned_rule(
    channel_name: str,
    rule_type: str,
    pattern: str,
    action: str,
    notes: str,
    source_msg_id: Optional[str] = None,
) -> int:
    with db() as conn:
        cur = conn.execute(
            """INSERT INTO channel_learned_rules
               (channel_name, rule_type, pattern, action, notes, created_at, source_msg_id)
               VALUES (?,?,?,?,?,?,?)""",
            (channel_name, rule_type, pattern, action, notes, time.time(), source_msg_id),
        )
        return cur.lastrowid


def save_synced_learned_rule(
    channel_name: str, rule_type: str, pattern: str, action: str,
    notes: str, source_msg_id: Optional[str],
) -> int:
    """Like save_channel_learned_rule(), but for rules arriving over the
    Local/Remote sync link (see sync/protocol.py MSG_LEARNED_RULE_SYNC) —
    idempotent on (channel_name, rule_type, source_msg_id) so a duplicate
    delivery of the same peer-approved rule doesn't create a second row."""
    with db() as conn:
        if source_msg_id:
            existing = conn.execute(
                "SELECT id FROM channel_learned_rules "
                "WHERE channel_name=? AND rule_type=? AND source_msg_id=?",
                (channel_name, rule_type, source_msg_id),
            ).fetchone()
            if existing:
                return existing["id"]
        cur = conn.execute(
            """INSERT INTO channel_learned_rules
               (channel_name, rule_type, pattern, action, notes, created_at, source_msg_id)
               VALUES (?,?,?,?,?,?,?)""",
            (channel_name, rule_type, pattern, action, notes, time.time(), source_msg_id),
        )
        return cur.lastrowid


def get_learned_parser_rules(channel_name: str) -> list[dict]:
    """ai_derived_parser rules for this channel, most recently created first —
    used by signal_parser.parse_with_learned_rules() to try deterministic
    matching before ever falling back to the AI extractor again."""
    return get_learned_rules_by_type(channel_name, "ai_derived_parser")


def get_learned_rules_by_type(channel_name: str, rule_type: str) -> list[dict]:
    """Generalised form of get_learned_parser_rules() — also used for
    rule_type='ai_derived_sl_adjust' (see signal_parser.check_sl_adjustment_rules)."""
    try:
        with db() as conn:
            rows = conn.execute(
                """SELECT * FROM channel_learned_rules
                   WHERE channel_name=? AND rule_type=?
                   ORDER BY id DESC""",
                (channel_name, rule_type),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def delete_channel_learned_rule(rule_id: int) -> None:
    with db() as conn:
        conn.execute("DELETE FROM channel_learned_rules WHERE id=?", (rule_id,))

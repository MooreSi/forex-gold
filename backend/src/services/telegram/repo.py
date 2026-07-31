"""Telegram — split from core/database.py.
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


def get_telegram_config() -> dict:
    with db() as conn:
        return row_to_dict(conn.execute("SELECT * FROM telegram_config WHERE id=1").fetchone())


def save_telegram_config(bot_token: str, chat_id: str, enabled: bool) -> None:
    with db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO telegram_config (id,bot_token_enc,chat_id,enabled,updated_at)"
            " VALUES (1,?,?,?,?)",
            (bot_token, chat_id, int(enabled), time.time()),
        )


def log_telegram_event(event_type: str, trade_id: Optional[str], status: str, detail: Optional[str]) -> None:
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO vantage_telegram_log (ts,event_type,trade_id,status,detail) VALUES (?,?,?,?,?)",
                (time.time(), event_type, trade_id, status, detail),
            )
    except Exception as e:
        log.debug("telegram log write failed: %s", e)


def save_telegram_reader_event(event_type: str, status: str, message: str, details: Optional[dict] = None) -> None:
    from datetime import datetime, timezone
    try:
        with db() as conn:
            conn.execute(
                "INSERT INTO telegram_reader_events (timestamp,event_type,status,message,details_json)"
                " VALUES (?,?,?,?,?)",
                (datetime.now(timezone.utc).isoformat(), event_type, status, message,
                 json.dumps(details) if details else None),
            )
    except Exception as e:
        log.debug("reader event write failed: %s", e)


def store_telegram_message(msg: dict) -> None:
    try:
        with db() as conn:
            # received_at is supposed to be the moment this message was first
            # seen — a real latency signal. But every reconnect re-backfills
            # the last 20 messages per channel (TelegramReader._backfill) and
            # calls this same function again for messages already stored by
            # the live handler; INSERT OR REPLACE was unconditionally
            # overwriting received_at with the backfill's "now", silently
            # inflating it by however long the reconnect took (confirmed
            # live 2026-07-10 — ~20+ min gaps traced to restart timing, not
            # actual signal latency). Preserve the first-seen value if a row
            # already exists; still refresh every other field (text edits,
            # sender resolution, etc. should still take the latest).
            existing = conn.execute(
                "SELECT received_at FROM telegram_messages WHERE telegram_message_id=? AND group_id=?",
                (str(msg["id"]), str(msg["group_id"])),
            ).fetchone()
            received_at = existing[0] if existing and existing[0] else msg.get("received_at")
            raw_json_msg = dict(msg)
            raw_json_msg["received_at"] = received_at
            conn.execute(
                """INSERT OR REPLACE INTO telegram_messages
                   (telegram_message_id,group_id,group_name,sender_id,sender_name,
                    timestamp,received_at,text,raw_text,has_media,media_type,
                    reply_to_message_id,forwarded,raw_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    str(msg["id"]), str(msg["group_id"]), msg.get("group_name", ""),
                    str(msg.get("sender_id") or ""), msg.get("sender_name") or "",
                    msg.get("timestamp"), received_at,
                    msg.get("text") or "", msg.get("raw_text") or "",
                    1 if msg.get("has_media") else 0, msg.get("media_type") or "none",
                    str(msg["reply_to_message_id"]) if msg.get("reply_to_message_id") else None,
                    1 if msg.get("forwarded") else 0,
                    json.dumps(raw_json_msg),
                ),
            )
    except Exception as e:
        log.error("message store failed: %s", e)


def get_stored_messages(limit: int = 100, offset: int = 0, group_id: Optional[str] = None) -> list[dict]:
    with db() as conn:
        if group_id:
            rows = conn.execute(
                "SELECT * FROM telegram_messages WHERE group_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                (str(group_id), min(limit, 500), offset),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM telegram_messages ORDER BY id DESC LIMIT ? OFFSET ?",
                (min(limit, 500), offset),
            ).fetchall()
    return [dict(r) for r in rows]


def get_messages_for_research(group_ids: list[str], since_iso: str) -> list[dict]:
    """Telegram messages for the given group_ids received since since_iso
    (UTC ISO string) — feeds the nightly Reversal Engine research job. Includes
    has_media/media_type so the caller can decide which ones to fetch
    images for; does not itself touch Telegram, just the local log."""
    placeholders = ",".join("?" for _ in group_ids)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM telegram_messages WHERE group_id IN ({placeholders}) "
            f"AND received_at >= ? ORDER BY id ASC",
            (*group_ids, since_iso),
        ).fetchall()
    return [dict(r) for r in rows]

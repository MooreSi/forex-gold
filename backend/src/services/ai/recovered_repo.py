"""Ai Recovered — split from core/database.py.
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


# ── AI-recovered signal review (Telegram > Reader Logic > AI tab) ─────────────

def save_ai_recovered_signal(
    tg_message_id: str, channel_name: str, raw_text: str, parsed: dict,
    confidence: float, reasoning: str,
) -> None:
    """Upsert, not a plain insert: a Telegram edit can trigger the AI fallback
    a second time for the same tg_message_id (e.g. the first pass extracted a
    signal, then a later edit corrected a level) — refresh the still-unapproved
    row with the improved extraction rather than silently keeping the stale
    first pass forever. Once a row is approved (or a rule generated from it),
    leave it alone — the approved extraction is what generated the rule, so
    overwriting it here would make the review-tab row lie about what was
    actually approved."""
    try:
        with db() as conn:
            existing = conn.execute(
                "SELECT id, approved, rule_generated FROM ai_recovered_signals "
                "WHERE tg_message_id=?", (tg_message_id,),
            ).fetchone()
            if existing and (existing["approved"] or existing["rule_generated"]):
                return
            fields = (
                raw_text, parsed.get("direction"),
                parsed.get("entry_low"), parsed.get("entry_high"), parsed.get("stop_loss"),
                parsed.get("tp1"), parsed.get("tp2"), parsed.get("tp3"), parsed.get("tp4"),
                parsed.get("tp5"), parsed.get("tp6"), parsed.get("tp7"), parsed.get("tp8"),
                confidence, reasoning,
            )
            if existing:
                conn.execute(
                    """UPDATE ai_recovered_signals
                       SET raw_text=?, direction=?, entry_low=?, entry_high=?, stop_loss=?,
                           tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?, tp7=?, tp8=?,
                           confidence=?, reasoning=?
                       WHERE id=?""",
                    fields + (existing["id"],),
                )
            else:
                conn.execute(
                    """INSERT INTO ai_recovered_signals
                       (raw_text, direction, entry_low, entry_high, stop_loss,
                        tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8, confidence, reasoning,
                        tg_message_id, channel_name, created_at)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    fields + (tg_message_id, channel_name, time.time()),
                )
    except Exception as e:
        log.warning("save_ai_recovered_signal failed: %s", e)


def save_ai_recovered_sl_adjustment(
    tg_message_id: str, channel_name: str, raw_text: str,
    new_stop_loss: float, confidence: float, reasoning: str,
) -> None:
    """Same table, same upsert-while-unapproved rule as
    save_ai_recovered_signal() — a follow-up "Adjust SL to X" style message
    reviewed in the same AI tab, just with message_type='sl_adjustment' and
    only new_stop_loss populated (no direction/entry/TP fields, since this
    isn't a new entry)."""
    try:
        with db() as conn:
            existing = conn.execute(
                "SELECT id, approved, rule_generated FROM ai_recovered_signals "
                "WHERE tg_message_id=?", (tg_message_id,),
            ).fetchone()
            if existing and (existing["approved"] or existing["rule_generated"]):
                return
            if existing:
                conn.execute(
                    """UPDATE ai_recovered_signals
                       SET raw_text=?, new_stop_loss=?, confidence=?, reasoning=?,
                           message_type='sl_adjustment'
                       WHERE id=?""",
                    (raw_text, new_stop_loss, confidence, reasoning, existing["id"]),
                )
            else:
                conn.execute(
                    """INSERT INTO ai_recovered_signals
                       (raw_text, new_stop_loss, confidence, reasoning, message_type,
                        tg_message_id, channel_name, created_at)
                       VALUES (?,?,?,?,'sl_adjustment',?,?,?)""",
                    (raw_text, new_stop_loss, confidence, reasoning,
                     tg_message_id, channel_name, time.time()),
                )
    except Exception as e:
        log.warning("save_ai_recovered_sl_adjustment failed: %s", e)


def try_claim_sl_adjustment(tg_message_id: str, channel_name: str, new_stop_loss: float) -> bool:
    """Dedup guard for SimulationEngine._apply_sl_adjustment() — returns True
    (and records the claim) the first time this tg_message_id is seen, False
    on every subsequent call. Needed because neither the ai_derived_sl_adjust
    fast path nor the AI-fallback path have any other table tracking which
    messages were already actioned (unlike entry signals, which dedupe via
    vantage_tg_signals) — without this, a message sitting in the Telegram
    reader's message buffer keeps getting re-matched and re-applied on every
    scan cycle for as long as it stays buffered."""
    try:
        with db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO sl_adjustment_applied "
                "(tg_message_id, channel_name, new_stop_loss, applied_at) VALUES (?,?,?,?)",
                (tg_message_id, channel_name, new_stop_loss, time.time()),
            )
            return cur.rowcount > 0
    except Exception as e:
        log.warning("try_claim_sl_adjustment failed: %s", e)
        return False


def _text_hash(text: str) -> str:
    import hashlib
    return hashlib.md5(text.encode("utf-8", errors="ignore")).hexdigest()


def has_ai_fallback_check(tg_message_id: str, text: str) -> bool:
    """True if this exact (tg_message_id, text) has already been through
    SimulationEngine._try_ai_signal_fallback() with a definitive result —
    the dedup guard that stops chatter from being reclassified by a paid AI
    call on every scan cycle. See ai_fallback_checked table comment."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT 1 FROM ai_fallback_checked WHERE tg_message_id=? AND text_hash=?",
                (tg_message_id, _text_hash(text)),
            ).fetchone()
            return row is not None
    except Exception:
        return False


def record_ai_fallback_check(tg_message_id: str, text: str) -> None:
    """Call only after the AI call itself succeeded (any definitive result,
    including "not a signal") — deliberately not called on a transient
    exception, so a network blip gets retried next cycle instead of
    permanently giving up on a message."""
    try:
        with db() as conn:
            conn.execute(
                "INSERT OR IGNORE INTO ai_fallback_checked (tg_message_id, text_hash, checked_at) "
                "VALUES (?,?,?)",
                (tg_message_id, _text_hash(text), time.time()),
            )
    except Exception as e:
        log.warning("record_ai_fallback_check failed: %s", e)


def get_ai_recovered_signals(limit: int = 100) -> list[dict]:
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_recovered_signals ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def has_unreviewed_ai_recovered_signals() -> bool:
    """Cheap existence check for the Telegram nav tab's notification dot —
    avoids fetching full rows just to know whether any are pending."""
    try:
        with db() as conn:
            row = conn.execute(
                "SELECT 1 FROM ai_recovered_signals WHERE approved=0 LIMIT 1"
            ).fetchone()
            return row is not None
    except Exception:
        return False


def mark_ai_recovered_signal_approved(row_id: int) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE ai_recovered_signals SET approved=1, approved_at=? WHERE id=?",
            (time.time(), row_id),
        )


def mark_ai_recovered_signal_rule_result(row_id: int, rule_id: Optional[int], note: str) -> None:
    with db() as conn:
        conn.execute(
            """UPDATE ai_recovered_signals
               SET rule_generated=?, rule_id=?, rule_gen_note=? WHERE id=?""",
            (1 if rule_id else 0, rule_id, note, row_id),
        )


def discard_ai_recovered_signal(row_id: int) -> None:
    """Remove a row from the Reader Logic > AI review queue with no further
    action — no rule generated, nothing approved. Used for entries the user
    doesn't want to review/act on (e.g. a misclassified or no-longer-relevant
    extraction)."""
    with db() as conn:
        conn.execute("DELETE FROM ai_recovered_signals WHERE id=?", (row_id,))


# ── AI-recovered signal peer sync (sync/protocol.py MSG_AI_RECOVERED_*) ──────
# Mirrors of the row_id-based mutators above, keyed by tg_message_id instead
# — the peer's local autoincrement id for the same message is unrelated to
# this node's, so approve/discard actions arriving over the sync link have
# to look the row up by its natural key (tg_message_id is UNIQUE).

def get_unresolved_ai_recovered_signals() -> list[dict]:
    """Full still-pending queue snapshot — used for MSG_AI_RECOVERED_PUSH,
    the periodic full-resync that also backfills anything a peer missed
    while disconnected (mirrors get_consolidated_trades for the ledger)."""
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT * FROM ai_recovered_signals WHERE approved=0 ORDER BY created_at ASC"
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def mark_ai_recovered_signal_approved_by_tg_id(tg_message_id: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE ai_recovered_signals SET approved=1, approved_at=? WHERE tg_message_id=?",
            (time.time(), tg_message_id),
        )


def mark_ai_recovered_signal_rule_result_by_tg_id(
    tg_message_id: str, rule_generated: bool, note: str,
) -> None:
    """Mirrors the boolean/note outcome only — not the numeric rule_id, which
    is meaningless across nodes (each side generates and stores its own rule
    row via the separate MSG_LEARNED_RULE_SYNC channel, with its own id)."""
    with db() as conn:
        conn.execute(
            "UPDATE ai_recovered_signals SET rule_generated=? WHERE tg_message_id=?",
            (1 if rule_generated else 0, tg_message_id),
        )
        if note:
            conn.execute(
                "UPDATE ai_recovered_signals SET rule_gen_note=? WHERE tg_message_id=?",
                (note, tg_message_id),
            )


def discard_ai_recovered_signal_by_tg_id(tg_message_id: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM ai_recovered_signals WHERE tg_message_id=?", (tg_message_id,))

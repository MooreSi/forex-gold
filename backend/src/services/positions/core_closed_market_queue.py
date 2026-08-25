"""Queue Closed Market Limits (Parsing Settings, 2026-07-31).

A "BUY/SELL [LIMITS] GOLD @ high/low AREA" signal that arrives while the
forex week is shut was previously dropped: handle_limit_order_signal returns
on `not sess_ok` and nothing ever re-attempted it, so every limit setup a
channel posted over a weekend was lost. This holds the parsed signal and
replays it through the normal placement path once the market reopens.

Only ever queues on a genuine weekend close (dpm_engine.is_weekly_market_
closed) -- never when sess_ok is False merely because the user turned an
Asia/London/NY session off, which is a deliberate "don't trade now" choice
rather than a "can't trade now" one and must keep dropping the signal.

Nothing here places, closes or modifies an MT5 order; flushing hands the
stored dict back to handle_limit_order_signal, which owns that entirely.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Awaitable, Callable

from backend.src.db import database as db_module
from backend.src.services.dpm.engine import is_weekly_market_closed

log = logging.getLogger(__name__)

# A queued signal older than this is dropped unflushed rather than placed.
# The longest real close is Fri 21:00 -> Sun 22:00 UTC (~49h); anything past
# 72h means the app was down across a whole week rather than a weekend, and
# an entry zone that stale is worthless -- the same reasoning as the 4-minute
# staleness guard on live signals, scaled to the market-closed case.
_MAX_QUEUE_AGE_SEC = 72 * 3600


def queue_closed_market_limit(
    tg_id: str, channel_name: str, source_label: str, parsed: dict,
) -> bool:
    """Stores `parsed` for replay at market open. Returns True if this call
    created the row, False if the message was already queued (the buffered
    message gets re-scanned every cycle -- without the INSERT OR IGNORE the
    same signal would pile up once per second until the market reopened)."""
    try:
        with db_module.db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO vantage_closed_market_queue "
                "(tg_message_id, channel_name, source_label, parsed_json, queued_at, status) "
                "VALUES (?,?,?,?,?,'queued')",
                (tg_id, channel_name, source_label, json.dumps(parsed), time.time()),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning("[ClosedMarketQueue] failed to queue tg_id=%s: %s", tg_id, exc)
        return False


def get_queued_limits() -> list[dict]:
    try:
        with db_module.db() as conn:
            rows = conn.execute(
                "SELECT * FROM vantage_closed_market_queue WHERE status='queued' "
                "ORDER BY queued_at ASC"
            ).fetchall()
        return [db_module.row_to_dict(r) for r in rows]
    except Exception as exc:
        log.warning("[ClosedMarketQueue] failed to read queue: %s", exc)
        return []


def _mark(tg_id: str, status: str) -> None:
    try:
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_closed_market_queue SET status=? WHERE tg_message_id=?",
                (status, tg_id),
            )
    except Exception as exc:
        log.warning("[ClosedMarketQueue] failed to mark tg_id=%s as %s: %s", tg_id, status, exc)


async def flush_queued_limits(
    rs: dict,
    place_fn: Callable[[dict, str, str, str], Awaitable[dict]],
) -> int:
    """Replays every queued limit signal now that the market is open.
    Returns how many were handed to `place_fn`.

    `place_fn(parsed, tg_id, channel_name, source_label)` is the caller's
    bound handle_limit_order_signal -- it needs the balance/lot-sizing
    collaborators and the bridge, which only the engine has.

    A row is marked terminal (placed/expired/failed) before or immediately
    after its placement attempt, never left 'queued' on an exception: a row
    that stayed queued through a failure would be retried every cycle for
    the rest of the week."""
    if is_weekly_market_closed():
        return 0
    rows = get_queued_limits()
    if not rows:
        return 0

    now = time.time()
    flushed = 0
    for row in rows:
        tg_id = row["tg_message_id"]
        if now - float(row["queued_at"]) > _MAX_QUEUE_AGE_SEC:
            _mark(tg_id, "expired")
            log.info("[ClosedMarketQueue] tg_id=%s expired unqueued (older than %dh)",
                     tg_id, _MAX_QUEUE_AGE_SEC // 3600)
            continue
        try:
            parsed = json.loads(row["parsed_json"])
        except Exception as exc:
            _mark(tg_id, "failed")
            log.warning("[ClosedMarketQueue] tg_id=%s has unreadable parsed_json: %s", tg_id, exc)
            continue

        _mark(tg_id, "placed")
        try:
            result = await place_fn(parsed, tg_id, row["channel_name"], row["source_label"])
            flushed += 1
            log.info("[ClosedMarketQueue] market reopened — replayed tg_id=%s (%s): %s",
                     tg_id, row["channel_name"], (result or {}).get("skip_reason", "placed"))
        except Exception as exc:
            _mark(tg_id, "failed")
            log.warning("[ClosedMarketQueue] replay failed for tg_id=%s: %s", tg_id, exc)
    return flushed


def should_queue(rs: dict) -> bool:
    """True when a limit signal arriving right now should be held rather
    than dropped."""
    return bool(rs.get("lk_queue_closed_market_limits", 0)) and is_weekly_market_closed()

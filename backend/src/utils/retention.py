"""Retention — split from core/database.py.
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

from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402
from backend.src.services.risk.app_config_repo import get_app_config, set_app_config  # noqa: E402

# ── Data retention ──────────────────────────────────────────────────────────
# Per-node preference (app_config, not synced) for how long historical data
# is kept before being deleted. 0 (the default) means indefinite — nothing is
# ever deleted. This was added 2026-07 after confirming there was never any
# retention/pruning logic in this codebase at all; the account simply hadn't
# been running very long yet. Keeping "indefinite" as the default preserves
# that existing behaviour exactly — this feature is opt-in, not a new limit.

def get_data_retention_days() -> int:
    try:
        raw = get_app_config("data_retention_days")
        return int(raw) if raw else 0
    except (TypeError, ValueError):
        return 0


def set_data_retention_days(days: int) -> None:
    set_app_config("data_retention_days", str(max(0, int(days))))


def prune_historical_data() -> dict:
    """
    Delete data older than the configured retention window. No-op if
    retention is set to indefinite (0, the default).

    Only ever deletes rows that are already historical/resolved:
      - telegram_messages: pure log, age-only.
      - vantage_simulated_trades: status='closed' only — open trades are
        never touched regardless of age.
      - vantage_signals: status IN ('closed','expired') only — pending/
        active signals are never touched.
      - vantage_tg_signals, channel_unrecognised_messages,
        ai_recovered_signals: parse/review logs, age-only (the live trade
        state they may have produced lives in the two tables above, which
        have their own explicit status guards).
    """
    days = get_data_retention_days()
    if days <= 0:
        return {"pruned": False, "reason": "indefinite", "deleted": {}}

    cutoff_epoch = time.time() - days * 86400
    cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()
    deleted: dict[str, int] = {}
    try:
        with db() as conn:
            for table, clause, param in (
                ("telegram_messages", "received_at < ?", cutoff_iso),
                ("vantage_simulated_trades", "status='closed' AND close_time < ?", cutoff_epoch),
                ("vantage_signals", "status IN ('closed','expired') AND created_at < ?", cutoff_epoch),
                ("vantage_tg_signals", "parsed_at < ?", cutoff_epoch),
                ("channel_unrecognised_messages", "received_at < ?", cutoff_epoch),
                ("ai_recovered_signals", "created_at < ?", cutoff_epoch),
            ):
                cur = conn.execute(f"DELETE FROM {table} WHERE {clause}", (param,))
                deleted[table] = cur.rowcount
    except Exception as e:
        log.warning("prune_historical_data failed: %s", e)
        return {"pruned": False, "reason": str(e), "deleted": deleted}

    return {"pruned": True, "retention_days": days, "deleted": deleted}

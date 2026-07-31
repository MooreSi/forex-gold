"""Circuit Breaker — split from core/database.py.
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
from backend.src.services.risk.risk_settings_repo import get_risk_settings, update_risk_settings  # noqa: E402

# ── Global Circuit Breaker ────────────────────────────────────────────────────
# State is persisted in vantage_risk_settings so it survives restarts.
# Fields (auto-migrated by _ensure_settings_cols):
#   circuit_breaker_enabled        INTEGER  0/1
#   circuit_breaker_losses         INTEGER  consecutive live losses to trigger
#   circuit_breaker_cooldown_mins  INTEGER  minutes the CB stays active
#   circuit_breaker_active_until   REAL     unix timestamp when CB expires (0 = off)
#   circuit_breaker_consec_losses  INTEGER  running count of consecutive losses


def get_circuit_breaker_state() -> dict:
    """Return current CB state. is_active is authoritative."""
    rs  = get_risk_settings()
    now = time.time()
    enabled      = bool(rs.get("circuit_breaker_enabled", 0))
    active_until = float(rs.get("circuit_breaker_active_until", 0) or 0)
    is_active    = enabled and active_until > now
    return {
        "enabled":         enabled,
        "losses_threshold": int(rs.get("circuit_breaker_losses", 3) or 3),
        "cooldown_mins":   int(rs.get("circuit_breaker_cooldown_mins", 60) or 60),
        "active_until":    active_until,
        "consec_losses":   int(rs.get("circuit_breaker_consec_losses", 0) or 0),
        "is_active":       is_active,
        "remaining_secs":  max(0.0, active_until - now) if is_active else 0.0,
    }


def record_live_trade_outcome(won: bool) -> dict:
    """Update consecutive-loss counter after a live MT5 trade closes.

    On a win/BE, resets the counter.  On a loss, increments it and triggers
    the cooldown when the threshold is reached.  Returns the new CB state.
    """
    rs        = get_risk_settings()
    enabled   = bool(rs.get("circuit_breaker_enabled", 0))
    if not enabled:
        return get_circuit_breaker_state()

    consec    = int(rs.get("circuit_breaker_consec_losses", 0) or 0)
    threshold = int(rs.get("circuit_breaker_losses", 3) or 3)
    cooldown  = int(rs.get("circuit_breaker_cooldown_mins", 60) or 60)

    just_triggered = False
    if won:
        update_risk_settings({"circuit_breaker_consec_losses": 0})
    else:
        consec += 1
        updates: dict = {"circuit_breaker_consec_losses": consec}
        if threshold > 0 and consec >= threshold:
            updates["circuit_breaker_active_until"]  = time.time() + cooldown * 60
            updates["circuit_breaker_consec_losses"] = 0  # reset after trigger
            just_triggered = True
        update_risk_settings(updates)

    state = get_circuit_breaker_state()
    # Distinguishes "this call is the one that tripped it" from "it was
    # already active from an earlier trigger and this loss just closed
    # while the cooldown was still running" — callers alerting on trigger
    # need this or they'd re-notify on every loss that happens to close
    # during an already-active cooldown, not just the one that caused it.
    state["just_triggered"] = just_triggered
    return state


def reset_circuit_breaker() -> None:
    """Manually clear an active circuit breaker and reset the loss counter."""
    update_risk_settings({
        "circuit_breaker_active_until":  0.0,
        "circuit_breaker_consec_losses": 0,
    })

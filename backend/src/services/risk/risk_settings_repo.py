"""Risk Settings — split from core/database.py.
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

from forex_trader.core import database as _database_module  # noqa: E402
from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402


_RS_CACHE_TTL = 10.0  # seconds — risk settings change only on user edit


def get_risk_settings() -> dict:
    # Cache state lives on the database module itself (_database_module._rs_cache),
    # not as a local global here -- see the comment at database.py's re-export site.
    now = time.time()
    if _database_module._rs_cache is not None and (now - _database_module._rs_cache_ts) < _RS_CACHE_TTL:
        return _database_module._rs_cache
    with db() as conn:
        result = row_to_dict(conn.execute("SELECT * FROM vantage_risk_settings WHERE id=1").fetchone())
    _database_module._rs_cache    = result
    _database_module._rs_cache_ts = now
    return result


_applying_sync_settings = False  # re-entrancy guard — see update_risk_settings


def update_risk_settings(updates: dict, _from_sync: bool = False) -> dict:
    """Update risk settings and, for a normal (non-sync-originated) edit,
    forward the change over the Local/Remote sync channel if one is active.

    _from_sync=True is used only by the two call sites that are themselves
    APPLYING a value that arrived over the sync channel (the Mac's
    settings-mirror on receipt of MSG_SETTINGS_STATE, and the VPS applying a
    Mac's MSG_SETTINGS_PROPOSE) — without this guard, applying an incoming
    sync value would immediately re-forward it back out, an infinite
    propose/confirm ping-pong between the two nodes.
    """
    global _applying_sync_settings
    if not updates:
        return get_risk_settings()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with db() as conn:
        conn.execute(f"UPDATE vantage_risk_settings SET {set_clause} WHERE id=1",
                     list(updates.values()))
    _database_module._rs_cache    = None   # invalidate so next read hits the DB
    _database_module._rs_cache_ts = 0.0
    result = get_risk_settings()

    if not _from_sync and not _applying_sync_settings:
        _applying_sync_settings = True
        try:
            _forward_settings_over_sync(updates)
        finally:
            _applying_sync_settings = False
    return result


def _forward_settings_over_sync(updates: dict) -> None:
    """Send a locally-made settings change to the paired node, whichever
    role this process has. No-op (and near-zero cost) if sync isn't
    configured — both get_instance() calls return None until sync.server
    .init()/sync.client.get_instance() have actually been used."""
    try:
        from backend.src.controllers.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None:
            # propose_settings() queues into cli._pending_settings and only
            # attempts to send if currently connected — calling it here even
            # while disconnected is what makes the change durable across the
            # frequent reconnects this link sees, instead of silently
            # dropping it when conn_state isn't "connected" at this instant.
            _schedule_coro(cli.propose_settings(updates))
            return
    except Exception as e:
        log.debug("[Sync] settings forward (client) failed: %s", e)

    try:
        from backend.src.controllers.sync import server as _sync_srv_mod
        srv = _sync_srv_mod.get_instance()
        if srv is not None:
            _schedule_coro(srv.broadcast_settings())
    except Exception as e:
        log.debug("[Sync] settings forward (server) failed: %s", e)


def is_session_allowed(rs: Optional[dict] = None) -> tuple[bool, str]:
    """Return (allowed, session_name) based on the user's Trading Markets selection.

    Session mapping:
      "asian"   → Asia button (21:00–07:00 UTC)
      "london"  → London button (07:00–12:00 UTC, pre-overlap)
      "overlap" → London OR New York button (12:00–16:00 UTC)
      "ny"      → New York button (16:00–21:00 UTC, post-overlap)
    """
    from backend.src.services.dpm.engine import detect_session, is_weekly_market_closed  # local to avoid circular import
    if is_weekly_market_closed():
        return False, "closed"
    if rs is None:
        rs = get_risk_settings()
    enabled: set[str] = set()
    if rs.get("session_asia_enabled", 1):
        enabled.add("asian")
    if rs.get("session_london_enabled", 1):
        enabled.add("london")
        enabled.add("overlap")
    if rs.get("session_ny_enabled", 1):
        enabled.add("ny")
        enabled.add("overlap")
    session = detect_session()
    return session in enabled, session


def get_fee_settings() -> dict:
    with db() as conn:
        return row_to_dict(conn.execute("SELECT * FROM vantage_fee_settings WHERE id=1").fetchone())


def update_fee_settings(updates: dict) -> dict:
    if not updates:
        return get_fee_settings()
    set_clause = ", ".join(f"{k}=?" for k in updates)
    with db() as conn:
        conn.execute(f"UPDATE vantage_fee_settings SET {set_clause} WHERE id=1",
                     list(updates.values()))
    return get_fee_settings()


def get_effective_strategy(rs: dict) -> tuple[str, bool]:
    """
    Return (effective_strategy_key, is_ooh_active).

    When Out of Hours is enabled and the current UTC time/date falls inside the
    configured window, the OOH strategy is returned and is_ooh_active=True.
    Otherwise the base trade_strategy is returned with is_ooh_active=False.

    The time window can span midnight (e.g. 22:00-07:00).
    The optional date range (ooh_date_from / ooh_date_to, ISO format YYYY-MM-DD)
    activates OOH all day on every date within the range — useful for holidays.
    """
    from datetime import datetime, date as _date, timezone as _tz

    base = rs.get("trade_strategy", "scale_out") or "scale_out"

    if not bool(rs.get("ooh_enabled", 0)):
        return base, False

    start_str     = rs.get("ooh_start_time", "22:00") or "22:00"
    end_str       = rs.get("ooh_end_time",   "07:00") or "07:00"
    ooh_strat     = rs.get("ooh_strategy",   "conservative") or "conservative"
    date_from_str = rs.get("ooh_date_from",  "") or ""
    date_to_str   = rs.get("ooh_date_to",    "") or ""

    try:
        now = datetime.now(_tz.utc)

        # Date-range filter: when active, OOH only applies on dates within the range.
        # If today is outside the range, OOH is inactive regardless of time.
        # If the date range is not active, the time window applies every day.
        if bool(rs.get("ooh_date_active", 0)) and date_from_str and date_to_str:
            try:
                d_from = _date.fromisoformat(date_from_str)
                d_to   = _date.fromisoformat(date_to_str)
                if not (d_from <= now.date() <= d_to):
                    return base, False
            except ValueError:
                return base, False

        # Daily time-window check
        cur = now.hour * 60 + now.minute
        sh, sm = (int(x) for x in start_str.split(":"))
        eh, em = (int(x) for x in end_str.split(":"))
        s = sh * 60 + sm
        e = eh * 60 + em
        in_window = (cur >= s or cur < e) if s >= e else (s <= cur < e)
        if in_window:
            return ooh_strat, True
    except Exception:
        pass
    return base, False

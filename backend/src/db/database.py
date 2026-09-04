"""
Unified SQLite database layer.
Single DB file combines trading engine schema and Telegram reader schema.
Schema is column-compatible with the source services for data migration.
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


_DB_PATH: str = ""


# Every db() call in this module runs synchronously and, historically, always
# on the single asyncio event-loop thread — fine when the underlying disk I/O
# is fast, but with no application-level bound when it isn't (VPS disk stalls
# were the likely cause of the multi-minute event-loop freezes seen in
# production). A single dedicated worker thread (not asyncio.to_thread's
# default shared pool, which would hand different calls to different threads
# non-deterministically) lets hot-path callers move DB work off the event
# loop while preserving today's fully-serialized, one-thread-touches-the-file
# access pattern — no new concurrent-connection/lock-contention risk is
# introduced, since it's always this same one thread issuing every call.
_db_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="db-worker")


# The main asyncio event loop, captured once at startup (see set_main_event_loop
# below) so code running on the DB worker thread — or any other non-main
# thread — can still schedule a coroutine onto it. Needed because
# update_risk_settings() -> _forward_settings_over_sync() tries to schedule
# an asyncio task (propose_settings/broadcast_settings); asyncio.ensure_future()
# only works on the thread that's actually running the target loop, so once
# to_db_thread() started moving more DB-writing calls off the event-loop
# thread, any of them that touch update_risk_settings() (e.g.
# record_live_trade_outcome() after a circuit-breaker update) silently failed
# to forward the change to the paired node — "RuntimeWarning: coroutine ...
# was never awaited", not a raised exception, so nothing surfaced this until
# settings visibly diverged between the two nodes.
_main_loop: Optional[asyncio.AbstractEventLoop] = None


def set_main_event_loop(loop: asyncio.AbstractEventLoop) -> None:
    global _main_loop
    _main_loop = loop


async def to_db_thread(fn, *args, **kwargs):
    """Run a synchronous database.py call on the dedicated DB worker thread
    instead of blocking the caller's own event-loop thread. Use this at
    call sites that run frequently (tight loops, hot paths) where a slow
    disk I/O moment would otherwise stall the entire application."""
    loop = asyncio.get_running_loop()
    if kwargs:
        import functools
        fn = functools.partial(fn, *args, **kwargs)
        return await loop.run_in_executor(_db_executor, fn)
    return await loop.run_in_executor(_db_executor, fn, *args)


# One connection per thread, reused for the thread's lifetime, instead of a
# fresh sqlite3.connect()/close() on every single db() call. This codebase
# calls db() extremely frequently — often several times per open trade per
# UI refresh timer tick — and opening/closing a connection each time is real,
# measurable synchronous work on the single asyncio event-loop thread (worse
# on Windows, where antivirus/Defender real-time scanning commonly intercepts
# each file-handle open). That was directly implicated in event-loop stalls
# of 400-600ms attributed to nicegui timer callbacks on the VPS. Safe to
# reuse across calls from the same thread: check_same_thread=False was
# already set, and every caller goes through this context manager, which
# always commits or rolls back before yielding control back.
_thread_local = threading.local()


def _close_thread_local_conn() -> None:
    """Close and drop this thread's cached db() connection, if any, so the
    next db() call on this thread opens a fresh connection against whatever
    _DB_PATH currently is -- see init()'s comment for why this matters."""
    conn = getattr(_thread_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        del _thread_local.conn
    if hasattr(_thread_local, "depth"):
        del _thread_local.depth


# Caches derived from the database's contents, which must be dropped whenever
# the active database changes. Modules register their own clear function at
# import time, so this file does not need to import them back and create a
# cycle -- core_strategy_params already imports this module.
#
# This exists because the same defect was found twice. get_risk_settings()
# memoised for 10s keyed on nothing but time, so a demo/live switch kept
# answering with the other environment's risk settings; core_strategy_params
# has a module-level dict with an identical 10s TTL and the same problem. A
# registry makes the next one a one-line registration instead of a third bug.
_cache_invalidators: list = []


def register_cache_invalidator(fn):
    """Register a callable to be run whenever init() re-points the database.

    Usable as a decorator or a plain call. Any cache whose contents come from
    the database belongs here.
    """
    _cache_invalidators.append(fn)
    return fn


def _invalidate_registered_caches() -> None:
    for fn in _cache_invalidators:
        try:
            fn()
        except Exception:  # a broken invalidator must not block the switch
            log.exception("[db] cache invalidator failed")


def init(db_path: str) -> None:
    global _DB_PATH
    # db() caches one connection per thread for the life of that thread, so
    # simply reassigning _DB_PATH here is not enough: any thread that already
    # opened a db() connection against the OLD path (the calling thread doing
    # this account-env switch, and the single to_db_thread() worker -- the
    # only two threads that ever call db()) would keep silently reading and
    # writing the OLD database file forever, since db() only opens a fresh
    # connection when a thread has none cached yet. Close both caches here so
    # every subsequent db() call, on either thread, opens fresh against the
    # new path. Found 2026-07-21: switching demo/live in the running app left
    # _apply_schema() below reusing the calling thread's stale connection, so
    # it silently re-applied the schema to the OLD file instead of the new
    # one -- the new file stayed schema-less until the next full process
    # restart, and background loops that read fresh (not cached) connections
    # against the new path (e.g. reversal_engine_correlate.py's VIP fetch)
    # broke immediately with "no such table".
    _close_thread_local_conn()
    _db_executor.submit(_close_thread_local_conn).result(timeout=5)
    # Same failure as the connection caches above, one layer up:
    # get_risk_settings() memoises its result for _RS_CACHE_TTL (10s) keyed on
    # nothing but time, so for ten seconds after a demo/live switch it would
    # keep answering with the OTHER environment's risk settings -- including
    # the session gates and the Max Risk per trade % ceiling. Pointing at a new
    # database has to invalidate it, exactly as it closes stale connections.
    global _rs_cache, _rs_cache_ts
    _rs_cache = None
    _rs_cache_ts = 0.0
    _invalidate_registered_caches()
    _DB_PATH = db_path
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    _apply_schema()


@contextmanager
def db():
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        # Two threads write this DB (the caller and the to_db_thread worker). With
        # the default busy_timeout of 0, a write while the other holds the lock
        # fails instantly with "database is locked"; 5s lets SQLite wait for the
        # lock to clear instead (review data H4 / backend H8).
        conn.execute("PRAGMA busy_timeout=5000")
        _thread_local.conn = conn
    # Nesting depth so a re-entrant db() call (a function that itself calls
    # db() while already inside another db() block, on the same thread)
    # doesn't commit/rollback the shared connection out from under the
    # outer block before it's done — only the outermost caller actually
    # finalizes the transaction; inner ones just pass the same connection
    # through untouched.
    depth = getattr(_thread_local, "depth", 0) + 1
    _thread_local.depth = depth
    try:
        yield conn
        if depth == 1:
            conn.commit()
    except Exception:
        if depth == 1:
            conn.rollback()
        raise
    finally:
        _thread_local.depth = depth - 1


def row_to_dict(row) -> dict:
    if row is None:
        return {}
    return dict(row)


# ── Schema ────────────────────────────────────────────────────────────────────
# The base DDL, the numbered migration registry and the data backfills all
# live in backend/migrations/ (schema evolution, kept apart from this
# runtime data-access layer — see that package's docstring).
from backend.migrations.schema_sql import SCHEMA as _SCHEMA  # noqa: E402,F401


# Migration mechanics (the ordered numbered registry, fail-closed handling,
# version stamp, pre-flight check) re-exported under the names tests/callers use.
from backend.migrations.registry import SCHEMA_VERSION, apply_migration as _apply_migration, run as _run_migrations, stamp_schema_version as _stamp_schema_version, verify_critical_schema as _verify_critical_schema, get_schema_version  # noqa: E402,F401
from backend.migrations.backfills import run as _run_backfills  # noqa: E402


def _apply_schema() -> None:
    with db() as conn:
        conn.executescript(_SCHEMA)
        # Migrations for existing databases: the ordered, numbered registry in
        # db/migrations.py (idempotent steps, fail-closed, per-step version
        # stamp), then the named every-boot data backfills in db/backfills.py
        # (missing table/column benign, everything else aborts).
        _run_migrations(conn)
        _run_backfills(conn)
        # singleton rows
        from backend.src.config import get as cfg_get
        starting_balance = cfg_get("starting_balance", 1000.0)
        mt5_login = cfg_get("mt5_login", 0)
        mt5_server = cfg_get("mt5_server", "")
        conn.execute(
            "INSERT OR IGNORE INTO mt5_credentials(id,login,server) VALUES(1,?,?)",
            (mt5_login, mt5_server),
        )
        conn.execute(
            "INSERT OR IGNORE INTO vantage_simulation_account(id,balance,reset_at) VALUES(1,?,?)",
            (starting_balance, time.time()),
        )
        conn.execute("INSERT OR IGNORE INTO telegram_config(id) VALUES(1)")
        conn.execute("INSERT OR IGNORE INTO vantage_risk_settings(id) VALUES(1)")
        conn.execute("INSERT OR IGNORE INTO vantage_fee_settings(id) VALUES(1)")
        conn.execute("INSERT OR IGNORE INTO email_config(id) VALUES(1)")

        _stamp_schema_version(conn)    # record the schema generation, then fail
        _verify_critical_schema(conn)  # closed if the money-critical shape is incomplete


def _schedule_coro(coro) -> None:
    """Schedule a coroutine regardless of which thread calls this — the
    main event-loop thread (asyncio.ensure_future works directly) or the
    dedicated DB worker thread / any other thread (needs
    run_coroutine_threadsafe targeting the captured main loop instead,
    since ensure_future only works on the thread actually running that
    loop). See set_main_event_loop's docstring above for why this exists."""
    try:
        asyncio.get_running_loop()
        asyncio.ensure_future(coro)
    except RuntimeError:
        if _main_loop is not None:
            asyncio.run_coroutine_threadsafe(coro, _main_loop)
        else:
            coro.close()  # avoid a dangling "never awaited" coroutine


# ── Re-exports: split into core_db_*.py, see docs/todo/refactor/core-database-migration/ ──
# Every name below is a verbatim extraction (function bodies AST-verified byte-identical
# to the pre-split original). Imported here so every existing `db_module.<name>` call site
# app-wide continues to work completely unchanged.
from backend.src.services.risk.app_config_repo import (  # noqa: E402,F401
    get_app_config,
    set_app_config,
)
# _rs_cache/_rs_cache_ts live here (not in core_db_risk_settings.py) because
# tests reset them via `db_module._rs_cache = None` -- a plain re-export
# would only rebind database.py's own name, leaving the cache actually used
# by get_risk_settings()/update_risk_settings() (in core_db_risk_settings.py)
# untouched. Those functions read/write these two names via the database
# module object directly, so this is the one true copy.
_rs_cache:    Optional[dict] = None
_rs_cache_ts: float = 0.0

from backend.src.services.risk.risk_settings_repo import (  # noqa: E402,F401
    _RS_CACHE_TTL,
    get_risk_settings,
    _applying_sync_settings,
    update_risk_settings,
    _forward_settings_over_sync,
    is_session_allowed,
    get_fee_settings,
    update_fee_settings,
    get_effective_strategy,
)
from backend.src.services.risk.circuit_breaker_repo import (  # noqa: E402,F401
    get_circuit_breaker_state,
    record_live_trade_outcome,
    reset_circuit_breaker,
)
from backend.src.db.retention import (  # noqa: E402,F401
    get_data_retention_days,
    set_data_retention_days,
    prune_historical_data,
)
from backend.src.services.cluster.sync_repo import (  # noqa: E402,F401
    _ensure_sync_tables,
    get_or_create_node_id,
    record_consolidated_trade,
    get_consolidated_ticket_maps,
    get_consolidated_extra_maps,
    get_consolidated_trades,
    get_active_trader,
    set_active_trader,
    is_remote_node,
    should_generate_signals_here,
    get_stood_down_engines,
    set_stood_down_engines,
    generate_sync_token,
    get_sync_token,
)
# Analytics names resolve lazily via __getattr__ below rather than an eager
# from-import. read_repo lives in backend/ now and legitimately gets imported
# on its own; an eager import here made "which module was imported first"
# decide whether the process bootstraps -- read_repo imported first meant this
# line ran while read_repo was half-initialised and raised ImportError.
_ANALYTICS_LAZY = {
    "_session_for_hour", "_trade_pts", "get_hourly_pnl_grid",
    "get_equity_drawdown_pct", "get_regime_score",
    "get_strategy_ladder_reach",
}


def __getattr__(name):
    if name in _ANALYTICS_LAZY:
        from backend.src.services.analytics import read_repo as _rr
        return getattr(_rr, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from backend.src.services.channels.scorecard_repo import (  # noqa: F401
    get_channel_performance_map,
)
from backend.src.services.channels.repo import (  # noqa: E402,F401
    _TG_GROUP_ID_MAP,
    _normalise_tg_source,
    sync_channel_rename,
    get_channel_scorecard,
    get_channel_strategy_breakdown,
    _CHANNEL_MIN_SAMPLE,
    _CHANNEL_PAUSE_PF,
    _CHANNEL_NO_AUTO_PAUSE,
    _channel_profit_factor,
    recompute_channel_performance,
    get_channel_lot_mult,
    _CHANNEL_TRUST_MIN_SAMPLES,
    _CHANNEL_TRUST_MIN_WR,
    get_channel_trust,
    CANONICAL_CHANNELS,
    _canonical,
    get_channel_strategy_override,
    _applying_sync_channel_strategy,
    set_channel_strategy_override,
    get_all_channel_strategy_overrides,
    _forward_channel_strategy_over_sync,
    get_channel_strategy_rec,
    set_channel_strategy_rec,
    get_open_trade_count,
    get_open_trade_count_for_channel,
    get_all_channel_strategy_settings,
    set_channel_paused,
)
from backend.src.services.risk.custom_strategies_repo import (  # noqa: E402,F401
    get_custom_strategies,
    save_custom_strategy,
    delete_custom_strategy,
)
from backend.src.services.telegram.repo import (  # noqa: E402,F401
    get_telegram_config,
    save_telegram_config,
    log_telegram_event,
    save_telegram_reader_event,
    store_telegram_message,
    get_stored_messages,
    get_messages_for_research,
)
from backend.src.services.ai.commentary_repo import (  # noqa: E402,F401
    save_commentary,
)
from backend.src.services.notifications.repo import (  # noqa: E402,F401
    get_email_config,
    save_email_config,
)
from backend.src.services.positions.ladder_repo import (  # noqa: E402,F401
    create_ladder_leg,
    get_ladder_legs,
    close_ladder_leg,
)
from backend.src.services.positions.max_tp_repo import (  # noqa: E402,F401
    save_max_tp_hit,
    get_trades_with_max_tp_set,
    get_max_tp_map_by_ticket,
    get_rr_map_by_ticket,
    get_trades_pending_max_tp,
)
from backend.src.services.positions.spread_cache_repo import (  # noqa: E402,F401
    get_cached_spreads,
    cache_spread,
)
from backend.src.services.broker.credentials_repo import (  # noqa: E402,F401
    _master_creds_path,
    _CRED_SECRET_COLS,
    get_mt5_credentials,
    save_mt5_credentials,
    _bridge_creds_path,
    sync_bridge_credentials_file,
)
from backend.src.services.channels.parser_repo import (  # noqa: E402,F401
    get_channel_parser_config,
    get_all_channel_parser_configs,
    save_channel_parser_config,
)
from backend.src.services.channels.unrecognised_repo import (  # noqa: E402,F401
    save_unrecognised_message,
    update_unrecognised_message,
    get_pending_unrecognised_messages,
    get_all_unrecognised_messages,
)
from backend.src.services.channels.learned_rules_repo import (  # noqa: E402,F401
    get_channel_learned_rules,
    save_channel_learned_rule,
    save_synced_learned_rule,
    get_learned_parser_rules,
    get_learned_rules_by_type,
    delete_channel_learned_rule,
)
from backend.src.services.ai.recovered_repo import (  # noqa: E402,F401
    save_ai_recovered_signal,
    save_ai_recovered_sl_adjustment,
    try_claim_sl_adjustment,
    _text_hash,
    has_ai_fallback_check,
    record_ai_fallback_check,
    get_ai_recovered_signals,
    has_unreviewed_ai_recovered_signals,
    mark_ai_recovered_signal_approved,
    mark_ai_recovered_signal_rule_result,
    discard_ai_recovered_signal,
    get_unresolved_ai_recovered_signals,
    mark_ai_recovered_signal_approved_by_tg_id,
    mark_ai_recovered_signal_rule_result_by_tg_id,
    discard_ai_recovered_signal_by_tg_id,
)
from backend.src.services.cluster.signal_bus_repo import (  # noqa: E402,F401
    _ensure_signal_bus,
    write_signal_bus,
    close_bus_entry,
    get_concurrent_signals,
    get_concurrent_agreement,
    prune_signal_bus,
    has_conflict_on_bus,
)

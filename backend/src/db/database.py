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
# The full DDL lives in db/schema_sql.py (moved out to keep this file shrinking).
from backend.src.db.schema_sql import SCHEMA as _SCHEMA  # noqa: E402,F401


# Schema migration mechanics live in db/migrations.py (fail-closed handling,
# version stamp, pre-flight check), re-exported under the names tests/callers use.
from backend.src.db.migrations import SCHEMA_VERSION, apply_migration as _apply_migration, stamp_schema_version as _stamp_schema_version, verify_critical_schema as _verify_critical_schema, get_schema_version  # noqa: E402,F401


def _apply_schema() -> None:
    with db() as conn:
        conn.executescript(_SCHEMA)
        # Rename-in-place for pre-existing databases whose column still carries
        # the old "gdc_" prefix (2026-07-23 rebrand: GD Copy Engine -> Reversal
        # Engine) -- must run BEFORE the ADD COLUMN loop below, which now
        # creates the new name directly on a fresh install and would otherwise
        # leave an existing install's real toggle state stranded on the old
        # column while reading back a fresh, always-off one.
        try:
            conn.execute(
                "ALTER TABLE vantage_risk_settings RENAME COLUMN gdc_live_execution TO re_live_execution"
            )
        except Exception:
            pass  # already renamed, or fresh DB never had the old column
        # Migrations for existing databases (idempotent)
        for stmt in [
            "ALTER TABLE vantage_tg_signals ADD COLUMN group_name TEXT",
            "ALTER TABLE vantage_risk_settings ADD COLUMN profit_close_usd REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE mt5_credentials ADD COLUMN live_login INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE mt5_credentials ADD COLUMN live_password_enc TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mt5_credentials ADD COLUMN live_server TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE mt5_credentials ADD COLUMN live_terminal_path TEXT",
            "ALTER TABLE email_config ADD COLUMN mailjet_api_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE email_config ADD COLUMN mailjet_secret_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE email_config ADD COLUMN resend_api_key TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE vantage_signals ADD COLUMN tp6 REAL",
            "ALTER TABLE vantage_signals ADD COLUMN tp7 REAL",
            "ALTER TABLE vantage_signals ADD COLUMN tp8 REAL",
            "ALTER TABLE vantage_simulated_trades ADD COLUMN tp6 REAL",
            "ALTER TABLE vantage_simulated_trades ADD COLUMN tp7 REAL",
            "ALTER TABLE vantage_simulated_trades ADD COLUMN tp8 REAL",
            "ALTER TABLE vantage_tg_signals ADD COLUMN tp6 REAL",
            "ALTER TABLE vantage_tg_signals ADD COLUMN tp7 REAL",
            "ALTER TABLE vantage_tg_signals ADD COLUMN tp8 REAL",
            "ALTER TABLE vantage_risk_settings ADD COLUMN display_strategy_id TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE vantage_risk_settings ADD COLUMN dpm_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN dpm_be_trigger_usd REAL NOT NULL DEFAULT 5.0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN dpm_trail_distance REAL NOT NULL DEFAULT 8.0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN dpm_tp1_partial_pct REAL NOT NULL DEFAULT 50.0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_start_time TEXT NOT NULL DEFAULT '22:00'",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_end_time TEXT NOT NULL DEFAULT '07:00'",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_strategy TEXT NOT NULL DEFAULT 'conservative'",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_date_from TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_date_to TEXT NOT NULL DEFAULT ''",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ooh_date_active INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN immediate_market_entry INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN exclude_high_risk INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE dpm_trade_performance ADD COLUMN tg_source TEXT",
            "ALTER TABLE email_config ADD COLUMN send_provider TEXT NOT NULL DEFAULT 'resend'",
            "ALTER TABLE vantage_risk_settings ADD COLUMN risk_governor_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_simulated_trades ADD COLUMN max_tp_hit TEXT",
            "ALTER TABLE vantage_risk_settings ADD COLUMN accept_tg_signals INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN sg_live_execution INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN bo_live_execution INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN unattended_mode INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN session_asia_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN session_london_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN session_ny_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN circuit_breaker_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN circuit_breaker_losses INTEGER NOT NULL DEFAULT 3",
            "ALTER TABLE vantage_risk_settings ADD COLUMN circuit_breaker_cooldown_mins INTEGER NOT NULL DEFAULT 60",
            "ALTER TABLE vantage_risk_settings ADD COLUMN circuit_breaker_active_until REAL NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN circuit_breaker_consec_losses INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN trail_stop_sl_pts REAL NOT NULL DEFAULT 5.0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN sg_claude_eval_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN bo_claude_eval_enabled INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN atr_collapse_threshold REAL NOT NULL DEFAULT 0.65",
            "ALTER TABLE vantage_risk_settings ADD COLUMN kelly_sizing_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN re_live_execution INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE channel_performance ADD COLUMN strategy_override TEXT",
            "ALTER TABLE channel_performance ADD COLUMN auto_strategy INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS channel_strategy_rec (
                source     TEXT PRIMARY KEY,
                strategy   TEXT NOT NULL DEFAULT 'conservative',
                reasoning  TEXT NOT NULL DEFAULT '',
                confidence REAL NOT NULL DEFAULT 0.0,
                updated_at REAL NOT NULL DEFAULT 0
            )""",
            # 'python' (default, existing behaviour) or 'ea' — set when open_trade()
            # hands a trade's SL/TP/partial-close management off to the local MQL5
            # EA over the ea_bridge socket instead of managing it in _monitor_loop.
            # Flipped back to 'python' by the EA-heartbeat-timeout fallback if the
            # EA goes silent, so the trade is never left with no manager at all.
            "ALTER TABLE vantage_simulated_trades ADD COLUMN managed_by TEXT NOT NULL DEFAULT 'python'",
            "ALTER TABLE vantage_risk_settings ADD COLUMN ea_bridge_enabled INTEGER NOT NULL DEFAULT 0",
            # Shared by both Bounce and Breakout generators — off by default (a
            # measured toxic-hour block was previously always-on per engine via
            # each one's own adaptive_params "hour_filter_enabled", with no user
            # facing toggle). When on, a blocked hour no longer suppresses signal
            # generation at all (the model still needs fresh data from those
            # hours to keep validating the measurement) — it only blocks
            # _execute_live() from placing the real MT5 order, exactly like the
            # existing daily-loss circuit breaker already does.
            "ALTER TABLE vantage_risk_settings ADD COLUMN hour_blocklist_enabled INTEGER NOT NULL DEFAULT 0",
            # 'signal' (default, a new entry) or 'sl_adjustment' — a follow-up
            # instruction to move an existing trade's SL, e.g. "Adjust SL to
            # 4060", recognised by the same AI fallback but reviewed/approved
            # and turned into a rule separately since it drives a different
            # live action (modify an open trade, not open a new one).
            "ALTER TABLE ai_recovered_signals ADD COLUMN message_type TEXT NOT NULL DEFAULT 'signal'",
            "ALTER TABLE ai_recovered_signals ADD COLUMN new_stop_loss REAL",
            # Morning ORB (opening-range breakout) report — a fixed 08:15
            # Europe/London send, independent of the daily/weekly summary's
            # configurable send_time, since it's tied to the London session
            # open rather than an arbitrary time of day. Defaults on: this is
            # an explicit user request, not an opt-in feature like the other
            # email reports above.
            "ALTER TABLE email_config ADD COLUMN orb_report_enabled INTEGER NOT NULL DEFAULT 1",
            # ORB/IVB Report tab's auto-execute toggle — places a real trade
            # every morning off the London-open reload-zone setup with no
            # further management (STRATEGY_ORB_FIXED). Defaults OFF unlike the
            # informational email above — this one moves real money/demo
            # positions unattended, so it must be an explicit opt-in.
            "ALTER TABLE vantage_risk_settings ADD COLUMN orb_auto_execute_enabled INTEGER NOT NULL DEFAULT 0",
            # ORB/IVB Report's lot size — shared by the manual Execute button
            # and the auto-execute scheduler, so a fixed size set once applies
            # to both. 0 = auto-size from Risk % and stop distance, matching
            # the Market Order tab's convention.
            "ALTER TABLE vantage_risk_settings ADD COLUMN orb_lot_size REAL NOT NULL DEFAULT 0",
            # Centralized signal generation — when on and this VPS is the
            # active trader, the VPS stops running its own Breakout/TestSignal
            # /REopy/GD2-GD-VIP analysis entirely and only executes trades
            # forwarded from the Mac (see should_generate_signals_here()).
            # Defaults off: changes which node's signals actually trade and
            # removes the VPS's ability to self-generate if the Mac drops.
            "ALTER TABLE vantage_risk_settings ADD COLUMN centralized_signal_gen_enabled INTEGER NOT NULL DEFAULT 0",
            # Limit Runner (STRATEGY_LIMIT_RUNNER): True once a resting pending
            # order fills if the originating signal contained a literal "TP OPEN"
            # line — the portion left after the last numeric TP has no fixed
            # target and rides as a runner (run_tp_ladder's close_full_on_last=
            # False path) instead of closing everything on the last TP like
            # every other ladder strategy. See core_limit_order_signal.py.
            "ALTER TABLE vantage_simulated_trades ADD COLUMN tp_open INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS vantage_pending_orders (
                trade_id       TEXT PRIMARY KEY,
                signal_id      TEXT NOT NULL,
                tg_message_id  TEXT,
                channel_name   TEXT NOT NULL,
                direction      TEXT NOT NULL,
                price          REAL NOT NULL,
                stop_loss      REAL NOT NULL,
                tps_json       TEXT NOT NULL,
                pcts_json      TEXT NOT NULL,
                be_at_pos      INTEGER NOT NULL,
                tp_open        INTEGER NOT NULL DEFAULT 0,
                lot_size       REAL NOT NULL,
                ea_ticket      INTEGER,
                status         TEXT NOT NULL DEFAULT 'working',
                created_at     REAL NOT NULL,
                resolved_at    REAL
            )""",
            # Which strategy the resulting trade gets registered under once this
            # pending order fills (_on_pending_order_filled reads this instead of
            # assuming "limit_runner" — the only strategy this table originally
            # tracked). Default preserves existing rows' actual behaviour.
            "ALTER TABLE vantage_pending_orders ADD COLUMN strategy TEXT NOT NULL DEFAULT 'limit_runner'",
            # Logic Keywords (Parsing page, 2026-07-22) -- editable phrase
            # lexicons for CLOSE ALL / RISK FREE-BE / TP HIT triggers, symbol
            # tokens, and exclusion filtering. See core_logic_keywords.py.
            """CREATE TABLE IF NOT EXISTS logic_keyword_lexicons (
                category     TEXT PRIMARY KEY,
                phrases_json TEXT NOT NULL
            )""",
            # Dedup guard for the CLOSE ALL / RISK FREE-BE / TP HIT triggers --
            # same problem/shape as sl_adjustment_applied above (the buffered
            # message keeps getting re-scanned every cycle without this).
            """CREATE TABLE IF NOT EXISTS logic_keyword_triggers_applied (
                tg_message_id TEXT NOT NULL,
                trigger_type  TEXT NOT NULL,
                applied_at    REAL NOT NULL,
                PRIMARY KEY (tg_message_id, trigger_type)
            )""",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_tp_parsing INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_sl_parsing INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_close_all_parsing INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_risk_free_be_parsing INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_tp_hit_parsing INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_ignore_media_messages INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_ignore_forwarded_messages INTEGER NOT NULL DEFAULT 0",
            # Reversal Engine page's "Active Positions" LIMIT ORDER toggle
            # (2026-07-23) -- off by default, same market-fill flow as today.
            "ALTER TABLE vantage_risk_settings ADD COLUMN re_use_limit_order INTEGER NOT NULL DEFAULT 0",
            # Order Type / pending-duration tracking (2026-07-23) -- Trade
            # Analysis had no way to tell a Limit Runner/EA Template grid fill
            # apart from an immediate market open, or to see how long a
            # resting order sat before it filled. order_type defaults to
            # 'market' (every immediate open_trade() caller); the two
            # EA-bridge fill-promotion paths (_on_pending_order_filled,
            # _promote_grid_leg_fill) explicitly set 'limit' and
            # pending_placed_at (copied from vantage_pending_orders.created_at
            # / the grid placeholder row's own open_time) so Trade Analysis
            # can show open_time - pending_placed_at as "time pending".
            "ALTER TABLE vantage_simulated_trades ADD COLUMN order_type TEXT NOT NULL DEFAULT 'market'",
            "ALTER TABLE vantage_simulated_trades ADD COLUMN pending_placed_at REAL",
            # Entry Realignment (2026-07-23) -- off by default. When a Limit
            # Runner signal's zone has already been breached by the time the
            # EA would place the resting order (root-caused live 2026-07-23:
            # a BuyLimit above/at current ask is broker-rejected as "Invalid
            # price"), this lets handle_limit_order_signal enter at market
            # instead and shift SL/TPs by the breach delta rather than losing
            # the signal outright.
            "ALTER TABLE vantage_risk_settings ADD COLUMN lk_entry_realignment INTEGER NOT NULL DEFAULT 0",
            # Trading > Global Parameters (2026-07-24) -- moved out of the
            # per-template EA Templates form and the Risk Settings tab so
            # they read as one shared set of account-wide numbers instead of
            # being scattered/duplicated across tabs. See core_ea_templates.py
            # and core_fees_sizing.suggest_lot_size for how they're consumed.
            "ALTER TABLE vantage_risk_settings ADD COLUMN strategy_lot_size_grid REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN global_harvest_enabled INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE vantage_risk_settings ADD COLUMN global_harvest_threshold_usd REAL NOT NULL DEFAULT 50.0",
        ] + [
            # EA Templates > Anchor TP (2026-07-24) -- a per-template pip/pct
            # ladder. tp{n}_pips is used only as a FALLBACK when the raw
            # Telegram signal itself didn't supply that TP level (entry ±
            # N pips); tp{n}_pct always wins over whatever the signal
            # implies, since a signal states TP prices but never states how
            # much to close at each one -- see core_open_trade.py's EA-
            # handoff block and core_ea_templates.py's DEFAULTS. 0 for
            # either field means "this level is unused" (matches the
            # existing tp6-8/pct-table convention everywhere else).
            f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pips REAL NOT NULL DEFAULT 0.0"
            for n in range(1, 9)
        ] + [
            f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pct REAL NOT NULL DEFAULT 0.0"
            for n in range(1, 9)
        ]:
            _apply_migration(conn, stmt)  # skips already-applied, aborts on a real error
        # One-off backfill (2026-07-23): every trade that filled before the
        # order_type column existed defaulted to 'market' regardless of how
        # it actually opened. Correct the two strategies whose identity
        # alone already proves they were a genuine resting pending order
        # (Limit Runner, ORB/IVB) -- their pending_placed_at can't be
        # recovered (never captured pre-migration), so "Pending For" stays
        # blank for these, but Order Type is now correct. Idempotent: the
        # WHERE clause only ever matches a row once.
        try:
            conn.execute(
                "UPDATE vantage_simulated_trades SET order_type='limit' "
                "WHERE strategy IN ('limit_runner','orb_fixed') AND order_type='market'"
            )
        except Exception:
            pass  # column doesn't exist on this schema version yet
        # 2026-07-23 rebrand: any strategy value stored under the old
        # "gd_vip_runner" identifier (channel overrides, open/closed trades,
        # pending orders, strategy-param templates) must keep pointing at the
        # same management logic under its new name, or those rows silently
        # fall back to whatever the global default strategy is on next read.
        for _tbl, _col in (
            ("vantage_risk_settings", "trade_strategy"),
            ("vantage_signals", "strategy"),
            ("vantage_simulated_trades", "strategy"),
            ("vantage_pending_orders", "strategy"),
            ("channel_performance", "strategy_override"),
            ("channel_strategy_rec", "strategy"),
            ("strategy_param_templates", "strategy"),
        ):
            try:
                conn.execute(
                    f"UPDATE {_tbl} SET {_col}='reversal_runner' WHERE {_col}='gd_vip_runner'"
                )
            except Exception:
                pass  # table/column doesn't exist on this schema version
        # Same 2026-07-23 rebrand, for the signal-source/channel-name string
        # itself ("GD Copy Engine" -> "Reversal Engine") -- without this,
        # historical rows keep the old name forever and the Channel Strategy
        # tab shows it as a second, orphaned row with none of the new row's
        # override/stats history.
        for _tbl, _col in (
            ("channel_parser_config", "channel_name"),
            ("channel_performance", "source"),
            ("channel_strategy_rec", "source"),
            ("vantage_simulated_trades", "tg_source"),
            ("vantage_signals", "source_name"),
            ("vantage_pending_orders", "channel_name"),
            ("consolidated_trades", "tg_source"),
        ):
            try:
                conn.execute(
                    f"UPDATE {_tbl} SET {_col}='Reversal Engine' WHERE {_col}='GD Copy Engine'"
                )
            except Exception:
                pass  # table/column doesn't exist on this schema version
        # Enable instant_entry for any GD2 channel configs that were bootstrapped
        # before the GD2 IME support was added (they defaulted to 0).
        conn.execute(
            "UPDATE channel_parser_config "
            "SET instant_entry_enabled=1 "
            "WHERE parser_format='gd2' AND instant_entry_enabled=0"
        )
        # Strip legacy "instant:" prefix from tg_source in all trade tables
        conn.execute(
            "UPDATE vantage_simulated_trades "
            "SET tg_source = SUBSTR(tg_source, 9) "
            "WHERE tg_source LIKE 'instant:%'"
        )
        conn.execute(
            "UPDATE vantage_signals "
            "SET source_name = SUBSTR(source_name, 9) "
            "WHERE source_name LIKE 'instant:%'"
        )
        # Backfill tg_source into dpm_trade_performance from the trade record
        conn.execute(
            "UPDATE dpm_trade_performance "
            "SET tg_source = ("
            "    SELECT tg_source FROM vantage_simulated_trades t "
            "    WHERE t.trade_id = dpm_trade_performance.trade_id"
            ") WHERE tg_source IS NULL OR tg_source LIKE 'instant:%'"
        )
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
}


def __getattr__(name):
    if name in _ANALYTICS_LAZY:
        from backend.src.services.analytics import read_repo as _rr
        return getattr(_rr, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


from backend.src.services.channels.repo import (  # noqa: E402,F401
    _TG_GROUP_ID_MAP,
    _normalise_tg_source,
    sync_channel_rename,
    get_channel_scorecard,
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
    get_channel_performance_map,
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

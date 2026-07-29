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

_SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS mt5_credentials (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    login         INTEGER NOT NULL,
    password_enc  TEXT    NOT NULL DEFAULT '',
    server        TEXT    NOT NULL DEFAULT '',
    terminal_path TEXT,
    account_type  TEXT    NOT NULL DEFAULT 'demo',
    updated_at    REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS mt5_connection_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    ts         REAL    NOT NULL,
    event_type TEXT    NOT NULL,
    detail     TEXT
);

CREATE TABLE IF NOT EXISTS vantage_signals (
    signal_id     TEXT PRIMARY KEY,
    source_name   TEXT NOT NULL DEFAULT '',
    direction     TEXT NOT NULL,
    entry_low     REAL NOT NULL,
    entry_high    REAL NOT NULL,
    stop_loss     REAL NOT NULL,
    tp1           REAL,
    tp2           REAL,
    tp3           REAL,
    tp4           REAL,
    tp5           REAL,
    tp6           REAL,
    tp7           REAL,
    tp8           REAL,
    lot_size      REAL,
    risk_pct      REAL,
    notes         TEXT DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',
    created_at    REAL NOT NULL,
    activated_at  REAL,
    cancelled_at  REAL,
    claude_commentary TEXT
);

CREATE TABLE IF NOT EXISTS vantage_simulated_trades (
    trade_id          TEXT PRIMARY KEY,
    signal_id         TEXT NOT NULL,
    mt5_ticket        INTEGER,
    direction         TEXT NOT NULL,
    entry_low         REAL NOT NULL,
    entry_high        REAL NOT NULL,
    entry_price       REAL NOT NULL,
    lot_size          REAL NOT NULL,
    remaining_lots    REAL NOT NULL,
    stop_loss         REAL NOT NULL,
    tp1               REAL,
    tp2               REAL,
    tp3               REAL,
    tp4               REAL,
    tp5               REAL,
    tp6               REAL,
    tp7               REAL,
    tp8               REAL,
    status            TEXT NOT NULL DEFAULT 'open',
    open_time         REAL NOT NULL,
    close_time        REAL,
    close_price       REAL,
    exit_reason       TEXT,
    gross_pnl         REAL NOT NULL DEFAULT 0,
    realised_pnl      REAL NOT NULL DEFAULT 0,
    spread_cost       REAL NOT NULL DEFAULT 0,
    commission        REAL NOT NULL DEFAULT 0,
    swap_est          REAL NOT NULL DEFAULT 0,
    slippage_cost     REAL NOT NULL DEFAULT 0,
    net_pnl           REAL NOT NULL DEFAULT 0,
    claude_open       TEXT,
    claude_close      TEXT,
    telegram_status   TEXT,
    sl_moved_to_be    INTEGER NOT NULL DEFAULT 0,
    strategy          TEXT NOT NULL DEFAULT 'scale_out',
    mt5_profit        REAL,
    tg_source         TEXT,
    FOREIGN KEY (signal_id) REFERENCES vantage_signals(signal_id)
);

CREATE TABLE IF NOT EXISTS vantage_partial_closes (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      TEXT NOT NULL,
    ts            REAL NOT NULL,
    lots_closed   REAL NOT NULL,
    close_price   REAL NOT NULL,
    pnl           REAL NOT NULL,
    reason        TEXT NOT NULL DEFAULT 'TP',
    FOREIGN KEY (trade_id) REFERENCES vantage_simulated_trades(trade_id)
);

-- Adaptive Runner's TP ladder as genuine resting broker-side orders: each
-- tier is its own separate MT5 position (hedging-mode account, confirmed
-- 2026-07-17) with its own native TP, so profit banks atomically at the
-- broker the instant price touches it -- not via Python noticing a crossed
-- tick, which a fast multi-point spike-and-reverse can miss entirely (see
-- project_adaptive_runner_ladder memory for the root-cause analysis).
-- One vantage_simulated_trades row (the "parent") still represents the whole
-- logical position for History/reporting; its own mt5_ticket is the anchor
-- (tier 1) leg for backward-compatible ticket-keyed lookups.
CREATE TABLE IF NOT EXISTS vantage_ladder_legs (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id      TEXT NOT NULL,
    tier          INTEGER NOT NULL,
    tp_num        INTEGER NOT NULL,
    tp_price      REAL NOT NULL,
    lots          REAL NOT NULL,
    entry_price   REAL,
    mt5_ticket    INTEGER,
    status        TEXT NOT NULL DEFAULT 'open',
    close_price   REAL,
    close_time    REAL,
    pnl           REAL,
    FOREIGN KEY (trade_id) REFERENCES vantage_simulated_trades(trade_id)
);
CREATE INDEX IF NOT EXISTS idx_ladder_legs_trade ON vantage_ladder_legs(trade_id);

CREATE TABLE IF NOT EXISTS vantage_simulation_account (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    balance       REAL NOT NULL,
    currency      TEXT NOT NULL DEFAULT 'USD',
    reset_at      REAL NOT NULL
);

CREATE TABLE IF NOT EXISTS telegram_config (
    id            INTEGER PRIMARY KEY CHECK (id = 1),
    bot_token_enc TEXT NOT NULL DEFAULT '',
    chat_id       TEXT NOT NULL DEFAULT '',
    enabled       INTEGER NOT NULL DEFAULT 0,
    updated_at    REAL    NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vantage_risk_settings (
    id                            INTEGER PRIMARY KEY CHECK (id = 1),
    risk_per_trade_pct            REAL    NOT NULL DEFAULT 0.5,
    max_risk_per_trade_pct        REAL    NOT NULL DEFAULT 1.0,
    max_daily_loss_pct            REAL    NOT NULL DEFAULT 3.0,
    max_total_drawdown_pct        REAL    NOT NULL DEFAULT 8.0,
    max_open_trades               INTEGER NOT NULL DEFAULT 1,
    max_pending_signals           INTEGER NOT NULL DEFAULT 10,
    default_lot_size              REAL    NOT NULL DEFAULT 0.01,
    max_lot_size                  REAL    NOT NULL DEFAULT 0.10,
    require_sl_and_tp             INTEGER NOT NULL DEFAULT 1,
    require_at_least_tp1          INTEGER NOT NULL DEFAULT 1,
    allow_no_sl                   INTEGER NOT NULL DEFAULT 0,
    move_sl_to_be_after_tp1       INTEGER NOT NULL DEFAULT 1,
    pause_after_losses            INTEGER NOT NULL DEFAULT 3,
    cooldown_after_loss_min       INTEGER NOT NULL DEFAULT 15,
    auto_execute_signals          INTEGER NOT NULL DEFAULT 0,
    trade_strategy                TEXT    NOT NULL DEFAULT 'scale_out',
    trailing_stop_distance        REAL    NOT NULL DEFAULT 5.0,
    strategy_lot_size             REAL    NOT NULL DEFAULT 0.0,
    immediate_market_entry        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS vantage_fee_settings (
    id                            INTEGER PRIMARY KEY CHECK (id = 1),
    account_type                  TEXT    NOT NULL DEFAULT 'custom',
    commission_per_lot_per_side   REAL    NOT NULL DEFAULT 0.0,
    commission_round_turn_per_lot REAL    NOT NULL DEFAULT 0.0,
    include_spread_cost           INTEGER NOT NULL DEFAULT 1,
    include_swap_cost             INTEGER NOT NULL DEFAULT 1,
    estimated_slippage_points     REAL    NOT NULL DEFAULT 5.0,
    max_allowed_spread_points     REAL    NOT NULL DEFAULT 50.0,
    swap_per_lot_per_night        REAL    NOT NULL DEFAULT -6.5
);

CREATE TABLE IF NOT EXISTS vantage_claude_commentary (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id     TEXT,
    signal_id    TEXT,
    ts           REAL NOT NULL,
    event_type   TEXT NOT NULL,
    payload      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS vantage_telegram_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           REAL    NOT NULL,
    event_type   TEXT    NOT NULL,
    trade_id     TEXT,
    status       TEXT    NOT NULL,
    detail       TEXT
);

CREATE TABLE IF NOT EXISTS vantage_tg_signals (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_message_id  TEXT NOT NULL UNIQUE,
    group_id       TEXT NOT NULL,
    group_name     TEXT,
    sender_name    TEXT,
    message_ts     TEXT,
    raw_text       TEXT NOT NULL,
    parsed_at      REAL NOT NULL,
    direction      TEXT,
    entry_low      REAL,
    entry_high     REAL,
    stop_loss      REAL,
    tp1            REAL,
    tp2            REAL,
    tp3            REAL,
    tp4            REAL,
    tp5            REAL,
    tp6            REAL,
    tp7            REAL,
    tp8            REAL,
    signal_id      TEXT,
    status         TEXT NOT NULL DEFAULT 'new'
);

CREATE TABLE IF NOT EXISTS vantage_bot_updates (
    update_id      INTEGER PRIMARY KEY,
    processed_at   REAL    NOT NULL,
    action         TEXT,
    result         TEXT
);

-- Telegram reader tables (merged from telegram-reader service)
CREATE TABLE IF NOT EXISTS telegram_messages (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    telegram_message_id   TEXT NOT NULL,
    group_id              TEXT NOT NULL,
    group_name            TEXT,
    sender_id             TEXT,
    sender_name           TEXT,
    timestamp             TEXT,
    received_at           TEXT,
    text                  TEXT,
    raw_text              TEXT,
    has_media             INTEGER,
    media_type            TEXT,
    reply_to_message_id   TEXT,
    forwarded             INTEGER,
    raw_json              TEXT,
    UNIQUE(telegram_message_id, group_id)
);

CREATE TABLE IF NOT EXISTS telegram_reader_events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp    TEXT,
    event_type   TEXT,
    status       TEXT,
    message      TEXT,
    details_json TEXT
);

CREATE TABLE IF NOT EXISTS custom_strategies (
    id            TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    description   TEXT NOT NULL DEFAULT '',
    base_strategy TEXT NOT NULL DEFAULT 'scale_out',
    rules_json    TEXT NOT NULL DEFAULT '{}',
    created_at    REAL NOT NULL
);

-- App config key/value store
CREATE TABLE IF NOT EXISTS app_config (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Named parameter presets for the fixed-point-SL strategies (Conservative,
-- Scalp Runner, Reversal Runner, Adaptive Runner, Adaptive Runner 2) -- see
-- core_strategy_params.py. The LIVE value for each strategy lives in
-- app_config (key f"strategy_params_{strategy}"); this table is only the
-- saved/named library a user can apply from later.
CREATE TABLE IF NOT EXISTS strategy_param_templates (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy    TEXT NOT NULL,
    name        TEXT NOT NULL,
    params_json TEXT NOT NULL,
    created_at  REAL NOT NULL
);

-- EA-native trade-management templates (2026-07-23) -- see
-- core_ea_templates.py. Unlike strategy_param_templates above (a named
-- preset of ONE existing Python strategy's numeric knobs), a row here is a
-- complete, self-contained, EA-managed trade-management definition -- Grid
-- vs Single entry, TP/SL visibility, trailing method, breakeven rule,
-- cancel-pending-siblings, and profit harvesting -- selectable per channel
-- (Trading > Channel Strategy) in place of a built-in strategy, not
-- alongside one. The EA reads every field fresh off the open_trade/
-- place_pending_order wire message, so changing a template never needs a
-- recompile.
CREATE TABLE IF NOT EXISTS ea_trade_templates (
    name              TEXT PRIMARY KEY,
    tg_cmd_enabled    INTEGER NOT NULL DEFAULT 1,
    harvest_enabled   INTEGER NOT NULL DEFAULT 0,
    harvest_threshold REAL NOT NULL DEFAULT 50.0,
    mode              TEXT NOT NULL DEFAULT 'single',
    grid_step_pts     REAL NOT NULL DEFAULT 10.0,
    grid_legs         INTEGER NOT NULL DEFAULT 3,
    tpsl_mode         TEXT NOT NULL DEFAULT 'on',
    anchor            TEXT NOT NULL DEFAULT 'unified',
    trail_mode        TEXT NOT NULL DEFAULT 'off',
    be_mode           TEXT NOT NULL DEFAULT 'entry',
    be_buffer_pts     REAL NOT NULL DEFAULT 1.0,
    be_trigger        INTEGER NOT NULL DEFAULT 1,
    cancel_pending    INTEGER NOT NULL DEFAULT 0,
    sig_guard         INTEGER NOT NULL DEFAULT 0,
    created_at        REAL NOT NULL,
    updated_at        REAL NOT NULL
);

-- Per-position spread cache — computed once from historical MT5 ticks at
-- trade-open time, then reused forever (spread at a past moment never
-- changes). Keyed by position_id since Closed Trades reads directly from
-- MT5 deal history, not any one engine's own trade table.
CREATE TABLE IF NOT EXISTS trade_spread_cache (
    position_id      INTEGER PRIMARY KEY,
    spread_price     REAL NOT NULL,
    spread_points    REAL NOT NULL,
    spread_cost_usd  REAL NOT NULL,
    computed_at      REAL NOT NULL
);

-- DPM per-trade performance log — used for self-calibration and analysis
CREATE TABLE IF NOT EXISTS dpm_trade_performance (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    trade_id              TEXT UNIQUE NOT NULL,
    direction             TEXT,
    entry_price           REAL,
    close_price           REAL,
    lot_size              REAL,
    original_sl           REAL,
    exit_type             TEXT,       -- 'SL', 'BE', 'trail', 'TP', 'manual'
    final_pnl             REAL,
    r_multiple            REAL,
    hold_minutes          REAL,
    -- Market state at entry (first DPM cycle)
    atr_at_entry          REAL,
    session_at_entry      TEXT,
    momentum_at_entry     REAL,
    momentum_label        TEXT,       -- weak/moderate/strong
    regime_at_entry       TEXT,       -- trending/ranging/spike
    -- Parameters in effect at entry
    be_multiplier_used    REAL,
    trail_multiplier_used REAL,
    be_trigger_used       REAL,
    trail_dist_used       REAL,
    tp1_pct_used          REAL,
    -- Trade milestones
    reached_be            INTEGER DEFAULT 0,
    reached_tp1           INTEGER DEFAULT 0,
    reached_tp2           INTEGER DEFAULT 0,
    peak_pnl              REAL DEFAULT 0.0,
    -- Meta
    used_calibrated       INTEGER DEFAULT 0,   -- 1 if calibrated params were available
    adx_at_entry          REAL,
    tg_source             TEXT,
    opened_at             REAL,
    closed_at             REAL
);

-- DPM calibration history — each run produces one row per (session, momentum_bucket)
CREATE TABLE IF NOT EXISTS dpm_calibration (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    calibrated_at   REAL NOT NULL,
    session         TEXT NOT NULL,
    momentum_bucket TEXT NOT NULL,
    be_multiplier   REAL NOT NULL,
    trail_multiplier REAL NOT NULL,
    tp1_partial_pct REAL NOT NULL,
    sample_size     INTEGER NOT NULL,
    profit_factor   REAL,
    win_rate        REAL,
    avg_r_multiple  REAL,
    notes           TEXT
);

CREATE TABLE IF NOT EXISTS email_config (
    id                 INTEGER PRIMARY KEY CHECK (id = 1),
    smtp_host          TEXT    NOT NULL DEFAULT '',
    smtp_port          INTEGER NOT NULL DEFAULT 587,
    smtp_user          TEXT    NOT NULL DEFAULT '',
    smtp_password      TEXT    NOT NULL DEFAULT '',
    from_addr          TEXT    NOT NULL DEFAULT '',
    to_addr            TEXT    NOT NULL DEFAULT '',
    use_tls            INTEGER NOT NULL DEFAULT 1,
    daily_enabled      INTEGER NOT NULL DEFAULT 0,
    weekly_enabled     INTEGER NOT NULL DEFAULT 0,
    send_time          TEXT    NOT NULL DEFAULT '18:00',
    mailjet_api_key    TEXT    NOT NULL DEFAULT '',
    mailjet_secret_key TEXT    NOT NULL DEFAULT '',
    resend_api_key     TEXT    NOT NULL DEFAULT '',
    send_provider      TEXT    NOT NULL DEFAULT 'resend',
    updated_at         REAL    NOT NULL DEFAULT 0
);

-- Per-channel signal parser configuration
CREATE TABLE IF NOT EXISTS channel_parser_config (
    channel_name            TEXT PRIMARY KEY,
    parser_format           TEXT NOT NULL DEFAULT 'auto',
    signal_prefix           TEXT NOT NULL DEFAULT '',
    instant_entry_enabled   INTEGER NOT NULL DEFAULT 0,
    enabled                 INTEGER NOT NULL DEFAULT 1,
    notes                   TEXT NOT NULL DEFAULT '',
    created_at              REAL NOT NULL DEFAULT 0,
    updated_at              REAL NOT NULL DEFAULT 0
);

-- Messages that did not match any configured parser
CREATE TABLE IF NOT EXISTS channel_unrecognised_messages (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name    TEXT NOT NULL,
    tg_message_id   TEXT NOT NULL UNIQUE,
    raw_text        TEXT NOT NULL,
    received_at     REAL NOT NULL,
    claude_analysis TEXT,
    resolution      TEXT,
    resolved_at     REAL,
    status          TEXT NOT NULL DEFAULT 'pending'
);

-- AI fallback extractions (deterministic parser missed the message) pending
-- human review in Telegram > Reader Logic > AI tab. Approving one triggers
-- automatic regex-rule generation (see ai_rule_generator.py) so future
-- messages of the same shape are parsed deterministically, no AI call.
CREATE TABLE IF NOT EXISTS ai_recovered_signals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_message_id   TEXT NOT NULL UNIQUE,
    channel_name    TEXT NOT NULL,
    raw_text        TEXT NOT NULL,
    direction       TEXT,
    entry_low       REAL,
    entry_high      REAL,
    stop_loss       REAL,
    tp1 REAL, tp2 REAL, tp3 REAL, tp4 REAL, tp5 REAL, tp6 REAL, tp7 REAL, tp8 REAL,
    confidence      REAL NOT NULL DEFAULT 0,
    reasoning       TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    approved        INTEGER NOT NULL DEFAULT 0,
    approved_at     REAL,
    rule_generated  INTEGER NOT NULL DEFAULT 0,
    rule_id         INTEGER,
    rule_gen_note   TEXT NOT NULL DEFAULT '',
    message_type    TEXT NOT NULL DEFAULT 'signal',
    new_stop_loss   REAL
);

-- One row per tg_message_id ever actioned by an SL-adjustment (either the
-- AI-fallback first pass or a matched ai_derived_sl_adjust learned rule) —
-- the dedup guard for SimulationEngine._apply_sl_adjustment(). Without this,
-- a message stays in the Telegram reader's buffer (get_buffer_messages) for
-- many scan cycles after being handled, and a broad learned-rule gate would
-- keep re-matching and re-firing on it every ~1s cycle indefinitely (found
-- live 2026-07-08: a single approved "adjust sl" rule with an unanchored
-- gate re-fired on the same message roughly once a minute for over half an
-- hour, spamming a Telegram alert each time even though the target SL never
-- changed). Entry signals don't need this separately since vantage_tg_signals
-- already dedupes them; SL-adjustments have no equivalent table of their own.
CREATE TABLE IF NOT EXISTS sl_adjustment_applied (
    tg_message_id TEXT PRIMARY KEY,
    channel_name  TEXT NOT NULL,
    new_stop_loss REAL,
    applied_at    REAL NOT NULL
);

-- One row per (tg_message_id, text) already put through the AI signal
-- fallback (SimulationEngine._try_ai_signal_fallback) — the dedup guard that
-- was missing entirely until 2026-07-08. Same root problem as
-- sl_adjustment_applied above: the Telegram reader's message buffer
-- (get_buffer_messages) holds recent messages regardless of processing
-- status, and this fallback is reached on every ~1s scan cycle for any
-- message still failing deterministic parsing. Without a claim here, a
-- single piece of channel chatter that was neither a signal nor an
-- SL-adjustment got reclassified by a live paid AI call every cycle,
-- indefinitely — confirmed live via a temporary caller-debug patch showing
-- ai_signal_extractor._classify firing continuously every ~2s with zero
-- corresponding Telegram activity. Keyed on a hash of the text (not just
-- tg_message_id) so a genuine edit still gets a fresh classification.
CREATE TABLE IF NOT EXISTS ai_fallback_checked (
    tg_message_id TEXT NOT NULL,
    text_hash     TEXT NOT NULL,
    checked_at    REAL NOT NULL,
    PRIMARY KEY (tg_message_id, text_hash)
);

-- Learned rules stored from user resolutions
CREATE TABLE IF NOT EXISTS channel_learned_rules (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    channel_name    TEXT NOT NULL,
    rule_type       TEXT NOT NULL DEFAULT 'ignore_pattern',
    pattern         TEXT NOT NULL DEFAULT '',
    action          TEXT NOT NULL DEFAULT 'ignore',
    notes           TEXT NOT NULL DEFAULT '',
    created_at      REAL NOT NULL,
    source_msg_id   TEXT
);

CREATE TABLE IF NOT EXISTS channel_performance (
    source          TEXT PRIMARY KEY,
    lot_mult        REAL NOT NULL DEFAULT 1.0,
    win_rate        REAL NOT NULL DEFAULT 0.0,
    sample_n        INTEGER NOT NULL DEFAULT 0,
    net_pnl         REAL NOT NULL DEFAULT 0.0,
    paused          INTEGER NOT NULL DEFAULT 0,
    manual_override INTEGER NOT NULL DEFAULT 0,
    updated_at      REAL NOT NULL DEFAULT 0
);
"""


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
        ] + [
            # Trading > Strategy > Internal Engine Exposure (2026-07-28) --
            # applies ONLY to the internal signal generators (Reversal,
            # Breakout, Bounce), never to Telegram-channel trades. 'off'
            # (default) is the long-standing behaviour: no restriction on
            # opposing positions. See core_internal_exposure_guard.py for
            # the modes and for why the default is deliberately off.
            "ALTER TABLE vantage_risk_settings ADD COLUMN internal_hedge_mode TEXT NOT NULL DEFAULT 'off'",
            "ALTER TABLE vantage_risk_settings ADD COLUMN internal_net_exposure_max_lots REAL NOT NULL DEFAULT 0.30",
        ] + [
            # EA Templates > Group TP Action (2026-07-28) -- grid mode only:
            # the first TP any leg of the group clears cancels every other
            # still-resting sibling and moves every other already-live
            # sibling's SL to its own breakeven. See core_ea_templates.py's
            # DEFAULTS and ForexTraderBridge.mq5's ApplyGroupTpAction.
            "ALTER TABLE ea_trade_templates ADD COLUMN group_tp_action INTEGER NOT NULL DEFAULT 0",
        ] + [
            # EA Templates: full copier parity (2026-07-29). Mirrors the
            # per-channel input block of the GoldSnipers copier EA
            # (goldbotea.set's InpC{n}_* group) so a template can express
            # the same behaviour. The big structural change is splitting
            # the old single `grid_legs` into an ANCHOR leg (enters at
            # market, near the zone) and PENDING legs (rest inside it),
            # each with their own count and lot -- observed live on signal
            # 25202, where the copier opened "_ANC" at 4026 and "_PEN" at
            # 4025. grid_legs is left in place so existing rows keep
            # working unchanged.
            "ALTER TABLE ea_trade_templates ADD COLUMN anchors INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE ea_trade_templates ADD COLUMN pendings INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE ea_trade_templates ADD COLUMN lot_anchor REAL NOT NULL DEFAULT 0.01",
            "ALTER TABLE ea_trade_templates ADD COLUMN lot_pending REAL NOT NULL DEFAULT 0.01",
            "ALTER TABLE ea_trade_templates ADD COLUMN sl_pips REAL NOT NULL DEFAULT 50.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN risk_pct REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN equity_protect REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN late_guard_pips REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN anc_shave INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE ea_trade_templates ADD COLUMN auto_sl INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE ea_trade_templates ADD COLUMN partials INTEGER NOT NULL DEFAULT 1",
            "ALTER TABLE ea_trade_templates ADD COLUMN cancel_pending_level INTEGER NOT NULL DEFAULT 0",
            "ALTER TABLE ea_trade_templates ADD COLUMN trail_distance REAL NOT NULL DEFAULT 50.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN trail_step REAL NOT NULL DEFAULT 10.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN trail_activation REAL NOT NULL DEFAULT 100.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN trail_padding REAL NOT NULL DEFAULT 0.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN max_spread_pips REAL NOT NULL DEFAULT 6.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN slippage INTEGER NOT NULL DEFAULT 20",
            "ALTER TABLE ea_trade_templates ADD COLUMN harvest_pips REAL NOT NULL DEFAULT 1.0",
            "ALTER TABLE ea_trade_templates ADD COLUMN signal_max_age_sec INTEGER NOT NULL DEFAULT 10",
        ] + [
            # TP ladder widened 8 -> 10 to match the copier's own depth.
            f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pips REAL NOT NULL DEFAULT 0.0"
            for n in (9, 10)
        ] + [
            f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pct REAL NOT NULL DEFAULT 0.0"
            for n in (9, 10)
        ] + [
            # Separate PENDING-leg ladder. The copier ships WIDER defaults
            # here than for the anchor (40/70/110/150/250 vs
            # 30/50/80/100/130): a leg filled deeper in the zone has more
            # room to the same structural target. Confirmed live on signal
            # 25204, where its pending leg entered 1pt better and so
            # carried 14pt of reward against the anchor's 13pt. Columns
            # default to 0 ("level unused") like every other TP column;
            # the copier's own defaults are offered in the UI instead of
            # being forced on existing templates.
            f"ALTER TABLE ea_trade_templates ADD COLUMN tp_pen{n}_pips REAL NOT NULL DEFAULT 0.0"
            for n in range(1, 11)
        ] + [
            f"ALTER TABLE ea_trade_templates ADD COLUMN tp_pen{n}_pct REAL NOT NULL DEFAULT 0.0"
            for n in range(1, 11)
        ]:
            try:
                conn.execute(stmt)
            except Exception:
                pass  # column already exists
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
        # One-off heal (2026-07-27): "Gold Diggers 2.0"'s channel_performance /
        # channel_strategy_rec rows predate both the GOLD DIGGERS INSTITUTIONAL
        # rename cascade and the PK-collision fix in core_db_channel's
        # sync_channel_rename -- the plain UPDATE above has the exact same bug
        # (silently no-ops via the bare except when the canonical row already
        # exists), so these two rows were never folded in and every lookup by
        # the live channel name missed whatever was set on the old one --
        # confirmed live: a user-set EA Template override sat here invisibly,
        # so the channel silently traded under the global default strategy
        # instead. sync_channel_rename won't re-fire for this pair on its own
        # (the Telegram-side title mismatch that triggers it is long gone),
        # so heal it directly here, once, with the same merge-safe helper.
        try:
            from forex_trader.core.core_db_channel import (
                _fold_renamed_row, _CHANNEL_UNIQUE_TABLES,
            )
            for _tbl, _col in (
                ("channel_parser_config", "channel_name"),
                ("channel_performance", "source"),
                ("channel_strategy_rec", "source"),
            ):
                _fold_renamed_row(
                    conn, _tbl, _col, "Gold Diggers 2.0", "GOLD DIGGERS INSTITUTIONAL",
                    _CHANNEL_UNIQUE_TABLES.get(_tbl, ()),
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
        from forex_trader.config import get as cfg_get
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
from forex_trader.core.core_db_app_config import (  # noqa: E402,F401
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

from forex_trader.core.core_db_risk_settings import (  # noqa: E402,F401
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
from forex_trader.core.core_db_circuit_breaker import (  # noqa: E402,F401
    get_circuit_breaker_state,
    record_live_trade_outcome,
    reset_circuit_breaker,
)
from forex_trader.core.core_db_retention import (  # noqa: E402,F401
    get_data_retention_days,
    set_data_retention_days,
    prune_historical_data,
)
from forex_trader.core.core_db_sync import (  # noqa: E402,F401
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
from forex_trader.core.core_db_analytics import (  # noqa: E402,F401
    _session_for_hour,
    _trade_pts,
    get_hourly_pnl_grid,
    get_equity_drawdown_pct,
    get_regime_score,
)
from forex_trader.core.core_db_channel import (  # noqa: E402,F401
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
from forex_trader.core.core_db_custom_strategies import (  # noqa: E402,F401
    get_custom_strategies,
    save_custom_strategy,
    delete_custom_strategy,
)
from forex_trader.core.core_db_telegram import (  # noqa: E402,F401
    get_telegram_config,
    save_telegram_config,
    log_telegram_event,
    save_telegram_reader_event,
    store_telegram_message,
    get_stored_messages,
    get_messages_for_research,
)
from forex_trader.core.core_db_commentary import (  # noqa: E402,F401
    save_commentary,
)
from forex_trader.core.core_db_email import (  # noqa: E402,F401
    get_email_config,
    save_email_config,
)
from forex_trader.core.core_db_ladder import (  # noqa: E402,F401
    create_ladder_leg,
    get_ladder_legs,
    close_ladder_leg,
)
from forex_trader.core.core_db_max_tp import (  # noqa: E402,F401
    save_max_tp_hit,
    get_trades_with_max_tp_set,
    get_max_tp_map_by_ticket,
    get_rr_map_by_ticket,
    get_trades_pending_max_tp,
)
from forex_trader.core.core_db_spread_cache import (  # noqa: E402,F401
    get_cached_spreads,
    cache_spread,
)
from forex_trader.core.core_db_credentials import (  # noqa: E402,F401
    _master_creds_path,
    _CRED_SECRET_COLS,
    get_mt5_credentials,
    save_mt5_credentials,
    _bridge_creds_path,
    sync_bridge_credentials_file,
)
from forex_trader.core.core_db_channel_parser import (  # noqa: E402,F401
    get_channel_parser_config,
    get_all_channel_parser_configs,
    save_channel_parser_config,
)
from forex_trader.core.core_db_unrecognised import (  # noqa: E402,F401
    save_unrecognised_message,
    update_unrecognised_message,
    get_pending_unrecognised_messages,
    get_all_unrecognised_messages,
)
from forex_trader.core.core_db_learned_rules import (  # noqa: E402,F401
    get_channel_learned_rules,
    save_channel_learned_rule,
    save_synced_learned_rule,
    get_learned_parser_rules,
    get_learned_rules_by_type,
    delete_channel_learned_rule,
)
from forex_trader.core.core_db_ai_recovered import (  # noqa: E402,F401
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
from forex_trader.core.core_db_signal_bus import (  # noqa: E402,F401
    _ensure_signal_bus,
    write_signal_bus,
    close_bus_entry,
    get_concurrent_signals,
    get_concurrent_agreement,
    prune_signal_bus,
    has_conflict_on_bus,
)

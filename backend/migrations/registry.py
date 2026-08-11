"""Schema migrations — an ordered, numbered registry, applied fail-closed.

History: the boot-time migrations were one flat list of ~90 idempotent
ALTER/CREATE statements inside database._apply_schema, originally wrapped in
a single `except Exception: pass` (so a genuinely failed migration was
indistinguishable from an already-applied one), later fail-closed via
apply_migration, and now numbered here so a database records *which*
migrations it has (`schema_version.version` = the last applied step).

Rules for changing this file:
- NEVER renumber, reorder, or edit an existing step — append a new one.
- Every SQL step must stay idempotent (ADD COLUMN / CREATE TABLE IF NOT
  EXISTS); apply_migration skips the benign already-applied errors and
  aborts on anything else.
- The statements below are the verbatim transcription of the old flat loop
  (2026-08-11); the grouping follows the feature waves the comments named.

These functions take a connection so they carry no import dependency on
database.py; database.py calls run() from _apply_schema after the base
CREATE TABLE pass.
"""
from __future__ import annotations

import sqlite3
import time

# Tables/columns the money path cannot run without. Verified after migration so
# a silently incomplete schema aborts startup instead of trading on it.
CRITICAL_SCHEMA = {
    "vantage_simulated_trades": {"trade_id", "managed_by", "order_type"},
    "vantage_risk_settings":    {"circuit_breaker_enabled", "re_live_execution"},
    "vantage_signals":          {"signal_id", "status"},
}


def apply_migration(conn, stmt: str) -> None:
    """Run one idempotent schema migration, failing closed on a real error.

    An already-applied migration raises 'duplicate column name' / 'already
    exists' — benign, skip it. ANY OTHER failure aborts startup: a genuinely
    failed migration must never be mistaken for an applied one, or the app
    trades on an unknown schema (review 2026-08-08, data #2). This is safe
    because _apply_schema runs CREATE TABLE IF NOT EXISTS for every table BEFORE
    this ADD COLUMN pass, so the only expected failure here is a duplicate
    column on an already-migrated database.
    """
    try:
        conn.execute(stmt)
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        if "duplicate column name" in msg or "already exists" in msg:
            return
        first = stmt.strip().splitlines()[0][:120]
        raise SystemExit(
            "FATAL: a database schema migration failed and was NOT a benign "
            "already-applied case. Refusing to start so the app never trades on "
            f"an unknown schema.\n  statement: {first}\n  error: {e}"
        )


def _rename_gdc_column(conn) -> None:
    """2026-07-23 rebrand: GD Copy Engine -> Reversal Engine. Must run BEFORE
    the ADD COLUMN steps, which create the new name directly on a fresh
    install and would otherwise leave an existing install's real toggle state
    stranded on the old column while reading back a fresh, always-off one."""
    try:
        conn.execute(
            "ALTER TABLE vantage_risk_settings RENAME COLUMN gdc_live_execution TO re_live_execution"
        )
    except sqlite3.OperationalError as e:
        msg = str(e).lower()
        # Benign: already renamed (duplicate/new name exists) or the old
        # column never existed (fresh DB).
        if ("duplicate column name" in msg or "already exists" in msg
                or "no such column" in msg):
            return
        raise SystemExit(
            "FATAL: the gdc_live_execution rename migration failed and was not "
            f"a benign already-applied case: {e}"
        )


# The registry. (number, title, step) where step is a list of idempotent SQL
# statements applied via apply_migration, or a callable(conn).
MIGRATIONS: list[tuple[int, str, object]] = [
    (1, "rename gdc_live_execution -> re_live_execution (2026-07-23 rebrand)",
     _rename_gdc_column),

    (2, "telegram group names, profit-close target, live MT5 credentials, email providers", [
        "ALTER TABLE vantage_tg_signals ADD COLUMN group_name TEXT",
        "ALTER TABLE vantage_risk_settings ADD COLUMN profit_close_usd REAL NOT NULL DEFAULT 0.0",
        "ALTER TABLE mt5_credentials ADD COLUMN live_login INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE mt5_credentials ADD COLUMN live_password_enc TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mt5_credentials ADD COLUMN live_server TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE mt5_credentials ADD COLUMN live_terminal_path TEXT",
        "ALTER TABLE email_config ADD COLUMN mailjet_api_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE email_config ADD COLUMN mailjet_secret_key TEXT NOT NULL DEFAULT ''",
        "ALTER TABLE email_config ADD COLUMN resend_api_key TEXT NOT NULL DEFAULT ''",
    ]),

    (3, "TP6-TP8 levels on signals, trades and telegram signals", [
        "ALTER TABLE vantage_signals ADD COLUMN tp6 REAL",
        "ALTER TABLE vantage_signals ADD COLUMN tp7 REAL",
        "ALTER TABLE vantage_signals ADD COLUMN tp8 REAL",
        "ALTER TABLE vantage_simulated_trades ADD COLUMN tp6 REAL",
        "ALTER TABLE vantage_simulated_trades ADD COLUMN tp7 REAL",
        "ALTER TABLE vantage_simulated_trades ADD COLUMN tp8 REAL",
        "ALTER TABLE vantage_tg_signals ADD COLUMN tp6 REAL",
        "ALTER TABLE vantage_tg_signals ADD COLUMN tp7 REAL",
        "ALTER TABLE vantage_tg_signals ADD COLUMN tp8 REAL",
    ]),

    (4, "DPM, out-of-hours windows, IME and high-risk exclusion settings", [
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
    ]),

    (5, "per-engine execution flags, circuit breaker, sessions, sizing and eval toggles", [
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
    ]),

    (6, "per-channel strategy overrides and recommendations", [
        "ALTER TABLE channel_performance ADD COLUMN strategy_override TEXT",
        "ALTER TABLE channel_performance ADD COLUMN auto_strategy INTEGER NOT NULL DEFAULT 0",
        """CREATE TABLE IF NOT EXISTS channel_strategy_rec (
            source     TEXT PRIMARY KEY,
            strategy   TEXT NOT NULL DEFAULT 'conservative',
            reasoning  TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            updated_at REAL NOT NULL DEFAULT 0
        )""",
    ]),

    (7, "EA management handoff and the hour blocklist", [
        # 'python' (default, existing behaviour) or 'ea' — set when open_trade()
        # hands a trade's SL/TP/partial-close management to the local MQL5 EA;
        # flipped back by the EA-heartbeat-timeout fallback.
        "ALTER TABLE vantage_simulated_trades ADD COLUMN managed_by TEXT NOT NULL DEFAULT 'python'",
        "ALTER TABLE vantage_risk_settings ADD COLUMN ea_bridge_enabled INTEGER NOT NULL DEFAULT 0",
        # Shared Bounce/Breakout toxic-hour block — when on it only blocks
        # _execute_live(), never signal generation.
        "ALTER TABLE vantage_risk_settings ADD COLUMN hour_blocklist_enabled INTEGER NOT NULL DEFAULT 0",
    ]),

    (8, "AI-recovered SL adjustments and the morning ORB report/auto-execute", [
        # 'signal' (default) or 'sl_adjustment' — a follow-up instruction to
        # move an existing trade's SL, reviewed and applied separately.
        "ALTER TABLE ai_recovered_signals ADD COLUMN message_type TEXT NOT NULL DEFAULT 'signal'",
        "ALTER TABLE ai_recovered_signals ADD COLUMN new_stop_loss REAL",
        # Fixed 08:15 Europe/London send; defaults on by explicit user request.
        "ALTER TABLE email_config ADD COLUMN orb_report_enabled INTEGER NOT NULL DEFAULT 1",
        # Moves real money/demo positions unattended — explicit opt-in, defaults OFF.
        "ALTER TABLE vantage_risk_settings ADD COLUMN orb_auto_execute_enabled INTEGER NOT NULL DEFAULT 0",
        # 0 = auto-size from Risk % and stop distance.
        "ALTER TABLE vantage_risk_settings ADD COLUMN orb_lot_size REAL NOT NULL DEFAULT 0",
    ]),

    (9, "centralized signal generation, TP-OPEN runners and pending orders", [
        "ALTER TABLE vantage_risk_settings ADD COLUMN centralized_signal_gen_enabled INTEGER NOT NULL DEFAULT 0",
        # Limit Runner: portion after the last numeric TP rides with no fixed target.
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
        # Which strategy the trade registers under when the pending order fills.
        "ALTER TABLE vantage_pending_orders ADD COLUMN strategy TEXT NOT NULL DEFAULT 'limit_runner'",
    ]),

    (10, "logic-keyword lexicons, trigger dedup and parsing toggles", [
        """CREATE TABLE IF NOT EXISTS logic_keyword_lexicons (
            category     TEXT PRIMARY KEY,
            phrases_json TEXT NOT NULL
        )""",
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
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_use_limit_order INTEGER NOT NULL DEFAULT 0",
    ]),

    (11, "order-type tracking, entry realignment and global parameters", [
        # 'market' (every immediate open) vs 'limit' (EA fill promotions).
        "ALTER TABLE vantage_simulated_trades ADD COLUMN order_type TEXT NOT NULL DEFAULT 'market'",
        "ALTER TABLE vantage_simulated_trades ADD COLUMN pending_placed_at REAL",
        "ALTER TABLE vantage_risk_settings ADD COLUMN lk_entry_realignment INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN strategy_lot_size_grid REAL NOT NULL DEFAULT 0.0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN global_harvest_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN global_harvest_threshold_usd REAL NOT NULL DEFAULT 50.0",
    ]),

    (12, "EA template Anchor TP pip/pct ladders (tp1-8)", [
        f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pips REAL NOT NULL DEFAULT 0.0"
        for n in range(1, 9)
    ] + [
        f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pct REAL NOT NULL DEFAULT 0.0"
        for n in range(1, 9)
    ]),
]

# The schema generation a fully migrated database carries = the last step.
SCHEMA_VERSION = MIGRATIONS[-1][0]


def _ensure_version_table(conn) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS schema_version("
        "id INTEGER PRIMARY KEY CHECK(id=1), version INTEGER NOT NULL, applied_at REAL NOT NULL)"
    )


def _set_version(conn, version: int) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO schema_version(id, version, applied_at) VALUES(1, ?, ?)",
        (version, time.time()),
    )


def run(conn) -> None:
    """Apply every step after the DB's recorded version, in order, advancing
    the stamp per step. Fail-closed: a failing step aborts with the stamp
    still pointing at the last good step."""
    _ensure_version_table(conn)
    row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
    current = int(row["version"]) if row else 0
    for number, _title, step in MIGRATIONS:
        if number <= current:
            continue
        if callable(step):
            step(conn)
        else:
            for stmt in step:
                apply_migration(conn, stmt)
        _set_version(conn, number)


def stamp_schema_version(conn) -> None:
    """Record the head schema generation (kept for existing callers; run()
    normally stamps per step)."""
    _ensure_version_table(conn)
    _set_version(conn, SCHEMA_VERSION)


def verify_critical_schema(conn) -> None:
    """Abort if a money-critical table or column is missing after migration."""
    for table, cols in CRITICAL_SCHEMA.items():
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            raise SystemExit(
                f"FATAL: required table '{table}' is missing after schema "
                "migration — refusing to start on an incomplete schema."
            )
        missing = cols - present
        if missing:
            raise SystemExit(
                f"FATAL: table '{table}' is missing column(s) {sorted(missing)} "
                "after schema migration — refusing to start on an incomplete schema."
            )


def get_schema_version() -> int:
    """The recorded schema generation, or 0 if never stamped."""
    from backend.src.db.database import db  # lazy: avoid an import cycle
    with db() as conn:
        row = conn.execute("SELECT version FROM schema_version WHERE id=1").fetchone()
        return int(row["version"]) if row else 0

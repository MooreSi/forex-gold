"""Full SQLite DDL for the trading database.

Moved verbatim out of database.py (which is oversized) — data only, no
logic. database.py imports SCHEMA and runs it via executescript in
_apply_schema; the ADD COLUMN migrations that follow live in migrations.py.
"""

SCHEMA = """
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
    max_total_drawdown_pct        REAL    NOT NULL DEFAULT 10.0,
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
-- automatic regex-rule generation (removed 2026-08-26, Q002 #1 -- the module
-- was never wired in either repo; rows here are written by hand-approved rules)
-- so future
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


-- Market snapshot at the instant a reference-channel signal arrived
-- (2026-08-04). One row per signal EVENT, not per signal: Gold Diggers VIP
-- fires a bare market call and then sends the zone/SL/TPs ~40s later, and
-- the difference between those two moments is likely where their
-- market-vs-limit decision actually lives, so each stage is captured
-- separately (see `stage`).
--
-- Purpose is to learn their entry logic from evidence rather than
-- assumption, and ultimately to feed the Reversal Engine's ML features.
-- Deliberately a wide, flat, append-only table: this is a research log, so
-- it favours "record everything at capture time" over normalisation, and
-- nothing reads it on the trading path.
CREATE TABLE IF NOT EXISTS tg_signal_snapshots (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    tg_message_id  TEXT NOT NULL,
    stage          TEXT NOT NULL,          -- market_call | levels | complete
    group_name     TEXT,
    direction      TEXT,
    signal_ts      REAL,                   -- when the message was parsed
    captured_at    REAL NOT NULL,          -- when this snapshot was taken
    capture_lag_s  REAL,                   -- captured_at - signal_ts, kept so
                                           -- staleness is auditable, not hidden
    -- stated levels (absent on a bare market call)
    entry_low      REAL,
    entry_high     REAL,
    stop_loss      REAL,
    tp1            REAL,
    -- market at capture
    bid            REAL,
    ask            REAL,
    spread_points  REAL,
    price          REAL,
    -- where price sat relative to what they asked for
    dist_to_entry_mid   REAL,
    price_inside_zone   INTEGER,
    session        TEXT,
    regime_score   REAL,
    -- per-timeframe indicators, JSON {"M1": {...}, "M5": {...}, "M15": {...}}
    -- JSON rather than 3x N columns: the set of indicators will change as
    -- this research develops, and a schema migration per idea would stall it.
    indicators_json TEXT,
    fvg_json        TEXT,
    raw_text        TEXT
);
CREATE INDEX IF NOT EXISTS idx_tg_snap_msg ON tg_signal_snapshots(tg_message_id, stage);
CREATE INDEX IF NOT EXISTS idx_tg_snap_ts  ON tg_signal_snapshots(captured_at);
"""

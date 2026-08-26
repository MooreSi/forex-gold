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

    # ── Upstream merge 2026-08-25 ─────────────────────────────────────────
    # Steps 13+ carry the schema evolution MooreSi/forex made between the
    # 2026-07-24 fork point and 2026-08-24, transcribed from that repo's
    # core/database.py migration list in its original order and grouping.
    # Append-only, like every step above: never renumber, never edit.
    (13, "Trading > Strategy > Internal Engine Exposure (2026-07-28)", [
        # Trading > Strategy > Internal Engine Exposure (2026-07-28) --
        # applies ONLY to the internal signal generators (Reversal,
        # Breakout, Bounce), never to Telegram-channel trades. 'off'
        # (default) is the long-standing behaviour: no restriction on
        # opposing positions. See core_internal_exposure_guard.py for
        # the modes and for why the default is deliberately off.
        "ALTER TABLE vantage_risk_settings ADD COLUMN internal_hedge_mode TEXT NOT NULL DEFAULT 'off'",
        "ALTER TABLE vantage_risk_settings ADD COLUMN internal_net_exposure_max_lots REAL NOT NULL DEFAULT 0.30",
    ]),
    (14, "Intraday give-back guard (2026-08-18)", [
        # Intraday give-back guard (2026-08-18). The existing daily-loss
        # limit measures from the day's OPENING balance, so it cannot see a
        # day that goes +$350 and then bleeds back to -$190 -- realised P&L
        # never breaches a from-open threshold on the way down, and that is
        # exactly the shape of 2026-08-17 (peak +$348.76 at 09:06, closed
        # -$88.48) and 08-18. This measures from the day's PEAK instead.
        #
        # Off by default: it stops trading for the rest of the broker day,
        # which is not a behaviour to switch on behind anyone's back.
        "ALTER TABLE vantage_risk_settings ADD COLUMN giveback_guard_enabled INTEGER NOT NULL DEFAULT 0",
        # Arms only once the day is genuinely up, so normal churn around
        # break-even can never lock the day out.
        "ALTER TABLE vantage_risk_settings ADD COLUMN giveback_arm_usd REAL NOT NULL DEFAULT 50.0",
        # How much of that peak may be surrendered before stopping.
        "ALTER TABLE vantage_risk_settings ADD COLUMN giveback_pct REAL NOT NULL DEFAULT 40.0",
    ]),
    (15, "Parsing Settings > TP/SL in Second Message (2026-07-31)", [
        # Parsing Settings > TP/SL in Second Message (2026-07-31). Holds a
        # bare "direction + entry, no levels yet" signal while its
        # follow-up is awaited. Deliberately NOT vantage_tg_signals: a row
        # there marks the message as seen and stops _scan_messages
        # re-reading it, and re-reading each cycle is exactly how the hold
        # gets re-evaluated (see core_second_message_merge.py).
        """CREATE TABLE IF NOT EXISTS vantage_second_message_holds (
            tg_message_id TEXT PRIMARY KEY,
            channel_name  TEXT NOT NULL,
            partial_json  TEXT NOT NULL,
            levels_json   TEXT,
            first_seen_at REAL NOT NULL,
            status        TEXT NOT NULL DEFAULT 'waiting'
        )""",
        # Parsing Settings > Queue Closed Market Limits (2026-07-31).
        # A LIMIT signal arriving over the weekend is currently discarded
        # outright (handle_limit_order_signal returns on !sess_ok); this
        # holds the parsed dict verbatim so it can be replayed once the
        # market reopens. tg_message_id is the PK so the same buffered
        # message re-scanned on a later cycle can't queue twice.
        """CREATE TABLE IF NOT EXISTS vantage_closed_market_queue (
            tg_message_id TEXT PRIMARY KEY,
            channel_name  TEXT NOT NULL,
            source_label  TEXT NOT NULL,
            parsed_json   TEXT NOT NULL,
            queued_at     REAL NOT NULL,
            status        TEXT NOT NULL DEFAULT 'queued'
        )""",
        # Parsing Settings (2026-07-31) -- all three default OFF: each one
        # changes what actually gets executed, so an existing install must
        # keep behaving exactly as it did until the user opts in.
        "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_mirror_copy INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN lk_queue_closed_market_limits INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN lk_enable_second_message_tp_sl INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN lk_second_message_match_window_sec INTEGER NOT NULL DEFAULT 120",
        # Reversal Engine REF confirmation gate (2026-07-31) -- off by
        # default. Only live-executes a signal when the professional
        # channels posted a matching entry within the window. See
        # reversal_engine/ref_confirmation.py for the measured decay curve
        # behind the 60-minute default and for why it isn't on by default.
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_require_ref_confirmation INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_ref_confirmation_window_min INTEGER NOT NULL DEFAULT 60",
        # Learn From Pro Signals (2026-08-06) -- off by default. When on,
        # every captured reference-channel signal refits pro_model.py and
        # its verdict enters the Reversal Engine's feature vector as
        # `pro_likeness`. Off leaves that feature at its neutral, so the
        # model behaves exactly as it did before this existed.
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_learn_from_ref_signals INTEGER NOT NULL DEFAULT 0",
    ]),
    (16, "EA Templates > Group TP Action (2026-07-28)", [
        # EA Templates > Group TP Action (2026-07-28) -- grid mode only:
        # the first TP any leg of the group clears cancels every other
        # still-resting sibling and moves every other already-live
        # sibling's SL to its own breakeven. See core_ea_templates.py's
        # DEFAULTS and ForexTraderBridge.mq5's ApplyGroupTpAction.
        "ALTER TABLE ea_trade_templates ADD COLUMN group_tp_action INTEGER NOT NULL DEFAULT 0",
    ]),
    (17, "EA Templates: full copier parity (2026-07-29)", [
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
    ]),
    (18, "TP ladder widened 8 -> 10 to match the copier's own depth.", [
        # TP ladder widened 8 -> 10 to match the copier's own depth.
        f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pips REAL NOT NULL DEFAULT 0.0"
        for n in (9, 10)
    ] + [
        f"ALTER TABLE ea_trade_templates ADD COLUMN tp{n}_pct REAL NOT NULL DEFAULT 0.0"
        for n in (9, 10)
    ]),
    (19, "Separate PENDING-leg ladder", [
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
    ]),
    (20, "Remaining goldbotea.set behaviour parity", [
        # Remaining goldbotea.set behaviour parity. The copier holds
        # these as EA globals; kept per-template here so two channels
        # can differ, which the copier itself cannot express.
        "ALTER TABLE ea_trade_templates ADD COLUMN use_dynamic_atr INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ea_trade_templates ADD COLUMN atr_period INTEGER NOT NULL DEFAULT 14",
        "ALTER TABLE ea_trade_templates ADD COLUMN atr_sl_mult REAL NOT NULL DEFAULT 1.5",
        "ALTER TABLE ea_trade_templates ADD COLUMN atr_tp1_mult REAL NOT NULL DEFAULT 1.5",
        "ALTER TABLE ea_trade_templates ADD COLUMN guard_pips REAL NOT NULL DEFAULT 10.0",
        "ALTER TABLE ea_trade_templates ADD COLUMN safety_cap_pips REAL NOT NULL DEFAULT 10.0",
        "ALTER TABLE ea_trade_templates ADD COLUMN use_emergency_sl INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ea_trade_templates ADD COLUMN emergency_sl_mult REAL NOT NULL DEFAULT 2.0",
        "ALTER TABLE ea_trade_templates ADD COLUMN signal_rr_ratio REAL NOT NULL DEFAULT 0.0",
        "ALTER TABLE ea_trade_templates ADD COLUMN tp1_trigger_level INTEGER NOT NULL DEFAULT 1",
        "ALTER TABLE ea_trade_templates ADD COLUMN manual_sl_push_pips REAL NOT NULL DEFAULT 10.0",
        "ALTER TABLE ea_trade_templates ADD COLUMN gold_half_pip_anchor INTEGER NOT NULL DEFAULT 0",
    ]),
    (21, "'Use TP Levels from Telegram' (2026-07-30), one flag per ladder", [
        # "Use TP Levels from Telegram" (2026-07-30), one flag per
        # ladder. When set, that ladder ignores its tp{n}_pips column and
        # takes its TP levels from the triggering Telegram message's own
        # stated prices. Defaults to 0 so every existing template keeps
        # its current, pips-driven behaviour untouched. See
        # core_ea_templates.DEFAULTS and core_open_trade's
        # _resolve_template_tps for the resolution order.
        "ALTER TABLE ea_trade_templates ADD COLUMN tp_from_telegram INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE ea_trade_templates ADD COLUMN tp_pen_from_telegram INTEGER NOT NULL DEFAULT 0",
    ]),
    (22, "close_full_on_last (2026-08-03): whether the last CONFIGURED Anchor TP level closes the whole re", [
        # close_full_on_last (2026-08-03): whether the last CONFIGURED
        # Anchor TP level closes the whole remaining position outright
        # instead of just its own tp{n}_pct. Defaults to 1 so every
        # existing template keeps its current (only) behaviour; a
        # template can now set this to 0 to leave a genuine runner past
        # its last defined TP level, managed by Trail/BE from there. See
        # core_ea_templates.DEFAULTS and ForexTraderBridge.mq5's
        # ManageTemplate.
        "ALTER TABLE ea_trade_templates ADD COLUMN close_full_on_last INTEGER NOT NULL DEFAULT 1",
    ]),
    (23, "Grid pending-leg placement model (2026-08-04)", [
        # Grid pending-leg placement model (2026-08-04). 'zone' is the
        # behaviour every existing template already has (span the
        # signal's own entry zone), so it is the default and nothing
        # changes on upgrade. 'step' steps grid_step_pts away from the
        # anchor's base price instead, matching the reference copier's
        # LADDER STEP -- and, unlike zone mode, can never place a leg on
        # the wrong side of the market and have it silently skipped.
        # See core_ea_templates.PENDING_MODE_CHOICES.
        "ALTER TABLE ea_trade_templates ADD COLUMN pending_mode TEXT NOT NULL DEFAULT 'zone'",
        # Sig Guard pip distance (the copier shows this as "SIG GUARD:
        # 20p"). 0 keeps the existing all-or-nothing guard.
        "ALTER TABLE ea_trade_templates ADD COLUMN sig_guard_pips REAL NOT NULL DEFAULT 0.0",
    ]),
    (24, "Grid-leg fill accounting (2026-08-03)", [
        # Grid-leg fill accounting (2026-08-03) -- an EA Template grid
        # trade's placeholder row (mt5_ticket=0, entry_price=0 -- see
        # core_template_placeholder_repair.py) previously had no record
        # of how many legs HandleOpenTemplateGrid actually placed, so
        # ea_bridge._on_grid_leg_cancelled could never tell "one sibling
        # leg cancelled, others may still fill" apart from "every leg
        # this grid ever had has now cancelled with none filled" -- it
        # always assumed the former and left the row open at $0 forever.
        # Confirmed live 2026-08-03: two single-pending-leg grids (no
        # anchor -- price was outside the zone at signal time) each had
        # their one resting leg expire unfilled, and both placeholder
        # rows sat in Active Trades for 5+ hours showing a fabricated
        # ~$16,132 unrealised P&L (the (current - 0) * lots arithmetic
        # every $0-entry row produces). grid_legs_total is set once, from
        # the EA's own trade_opened ack (HandleOpenTemplateGrid's
        # legs_placed field); grid_legs_cancelled increments on every
        # confirmed cancellation, and _on_grid_leg_cancelled closes the
        # row via record_close (its existing zero-entry guard keeps this
        # from fabricating P&L) once cancelled reaches total. NULL/0 for
        # every non-grid trade, which never touches either column.
        "ALTER TABLE vantage_simulated_trades ADD COLUMN grid_legs_total INTEGER",
        "ALTER TABLE vantage_simulated_trades ADD COLUMN grid_legs_cancelled INTEGER NOT NULL DEFAULT 0",
    ]),
    (25, "Fallback SL Distance (2026-08-05)", [
        # Fallback SL Distance (2026-08-05) -- the stop substituted for a
        # signal's own when Enable SL Parsing is OFF. Only ever consulted
        # by that path (core_logic_keyword_triggers.apply_sl_parsing_
        # override); a channel with an EA Template uses the template's own
        # sl_pips ahead of it. 50 pips matches ea_trade_templates.sl_pips'
        # own default.
        "ALTER TABLE vantage_risk_settings ADD COLUMN lk_fallback_sl_pips REAL NOT NULL DEFAULT 50.0",
    ]),
    (26, "Realized-R inputs, captured at open (2026-08-07)", [
        # Realized-R inputs, captured at open (2026-08-07). R:R on Closed
        # Trades is net_pnl / initial risk (core_db_max_tp.
        # get_rr_map_by_ticket), and BOTH stop columns it used to
        # reconstruct that risk from are unusable:
        #
        #   * vantage_simulated_trades.stop_loss is overwritten in place
        #     by every breakeven/trailing path, so a winner's stored stop
        #     is no longer what it risked (often exactly entry_price).
        #   * vantage_signals.stop_loss -- preferred instead to dodge
        #     that -- is not what got placed for an EA Template channel:
        #     core_signal_resolution.py makes the template's own sl_pips
        #     authoritative and replaces the signal's stop outright.
        #
        # Measured live 2026-08-07 on "Grid - Zone Mode" (sl_pips=40, so
        # every stop 4.00 from entry) against signal stops ranging 0.90
        # to 8.78: full stop-outs reported anywhere from -0.71R to
        # -2.20R instead of -1.00R.
        #
        # initial_sl is the stop price actually sent to the broker,
        # written once and never touched again. initial_risk is that
        # distance in account currency across EVERY leg -- an EA Template
        # grid is N broker positions behind ONE row whose lot_size is
        # only the promoting leg's, while core_profit_sync sums all N
        # legs into net_pnl, so a 2-leg grid's R was also ~2x too large
        # in magnitude on top of the wrong distance. Seeded at open from
        # the legs the EA's ack says it placed, then replaced with the
        # exact figure (each filled leg's own opening price and volume)
        # by core_profit_sync once the legs settle -- a resting leg that
        # never fills carries no risk and must not count.
        "ALTER TABLE vantage_simulated_trades ADD COLUMN initial_sl REAL",
        "ALTER TABLE vantage_simulated_trades ADD COLUMN initial_risk REAL",
    ]),
    (27, "Staged SL ratchet (2026-08-10)", [
        # Staged SL ratchet (2026-08-10) -- trail_mode="staged". Each
        # rung fires once, moving SL to target_pips (signed: negative
        # still risks a loss, 0 = breakeven, positive locks profit) when
        # floating profit crosses trigger_pips; the last rung can also
        # strip the take-profit. See core_ea_templates.DEFAULTS and
        # ForexTraderBridge.mq5's ManageTemplate.
        f"ALTER TABLE ea_trade_templates ADD COLUMN sl_stage{n}_trigger_pips REAL NOT NULL DEFAULT 0.0"
        for n in (1, 2, 3)
    ] + [
        f"ALTER TABLE ea_trade_templates ADD COLUMN sl_stage{n}_target_pips REAL NOT NULL DEFAULT 0.0"
        for n in (1, 2, 3)
    ] + [
        f"ALTER TABLE ea_trade_templates ADD COLUMN sl_stage{n}_remove_tp INTEGER NOT NULL DEFAULT 0"
        for n in (1, 2, 3)
    ]),
    (28, "Basket harvest (2026-08-12)", [
        # Basket harvest (2026-08-12) -- mirror image of equity_protect:
        # close every open position on this (channel, template) group
        # once their COMBINED floating profit reaches this many
        # account-currency units. See core_equity_protect.py.
        "ALTER TABLE ea_trade_templates ADD COLUMN basket_harvest_threshold REAL NOT NULL DEFAULT 0.0",
    ]),
    (29, "harvest_pips was on for everyone (2026-08-26)", [
        # Migration 17 added harvest_pips as DEFAULT 1.0, and DEFAULTS in
        # ea_templates.py carried 1.0 too. The EA then implemented the field
        # (2026-08-04) as a second harvest trigger ORed with the dollar
        # threshold, assuming "0 = off, matches every template saved before
        # this existed" -- but no template had ever held 0. Every
        # harvest-enabled template therefore closed at the first favourable
        # pip, making harvest_threshold unreachable: live on 2026-08-26, a
        # template set to $30 harvested two trades at $1.40 each.
        #
        # Only 1.0 is cleared -- exactly the value the old column default
        # produced. A template someone deliberately set to another number
        # keeps it. (Nothing can have been set deliberately today: the field
        # is not exposed in the UI. This is written to survive that changing.)
        "UPDATE ea_trade_templates SET harvest_pips = 0.0 WHERE harvest_pips = 1.0",
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

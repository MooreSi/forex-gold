"""Schema migrations 39 onward.

A continuation of `steps.py`, not a second registry. That file reached the
800-line structure ceiling at migration 38, and its first rule is that a
step is only ever APPENDED -- never renumbered, reordered or edited. A split
by number keeps that rule literally true: nothing moved except where the
text lives, and `steps.py` concatenates this onto the end so `MIGRATIONS`
still reads 1..N in order.

Same rules apply here. Append only, every statement idempotent.
"""
from __future__ import annotations

_RECENT: list[tuple[int, str, object]] = [
    # An ATR-sized LADDER, not just an ATR-sized TP1. use_dynamic_atr sizes
    # the stop and TP1 from ATR and stops there, so on a volatile day the
    # stop and first target move out and TP2 upward do not -- R constant at
    # the first target and drifting above it. Off, so every existing
    # template is byte-identical. docs/todo/reversal-engine/200 section 2.
    (39, "ATR-scaled anchor ladder (2026-09-11), off by default", [
        "ALTER TABLE ea_trade_templates ADD COLUMN atr_ladder_scale INTEGER NOT NULL DEFAULT 0",
    ]),

    # Measured execution cost, per fill. Nothing in the app has ever
    # compared the price asked for with the price received:
    # fees_sizing.calculate_fees charges a CONSTANT estimated_slippage_
    # points to every trade. reversal-engine/020 is trying to explain 0.53
    # points of leakage per loss with no measurement of this at all.
    # Written by services/broker/tca.py; read-only everywhere else.
    (40, "Execution quality: measured slippage and spread per fill", [
        """CREATE TABLE IF NOT EXISTS execution_quality (
            trade_id         TEXT PRIMARY KEY,
            mt5_ticket       INTEGER,
            measured_at      REAL NOT NULL,
            open_time        REAL,
            direction        TEXT,
            strategy         TEXT,
            bucket           TEXT,
            requested_price  REAL,
            fill_price       REAL,
            slippage_pts     REAL,
            spread_open_pts  REAL,
            spread_close_pts REAL,
            spread_cost_pts  REAL,
            cost_pts         REAL,
            sl_dist          REAL,
            cost_r           REAL,
            fill_delay_s     REAL,
            measured         INTEGER NOT NULL DEFAULT 0
        )""",
        "CREATE INDEX IF NOT EXISTS idx_execquality_open ON execution_quality(open_time)",
    ]),

    # Every new capability from docs/todo/reversal-engine/200, each one OFF
    # and each default byte-identical to today's behaviour. Nothing in this
    # step changes what the app trades; they are the switches a demo session
    # turns on one at a time, which is the only way any of them can be
    # attributed afterwards.
    (41, "Reversal-engine capability switches, all off (2026-09-11)", [
        # Section 1.1: barriers fitted to our own excursion data instead of
        # the reference channel's fixed point offsets.
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_atr_barriers_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_atr_stop_mult REAL NOT NULL DEFAULT 1.2",
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_atr_tp1_mult REAL NOT NULL DEFAULT 1.2",
        # Section 4.2: confirmation at the level, not just arrival at it.
        "ALTER TABLE vantage_risk_settings ADD COLUMN entry_trigger_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN entry_trigger_rejection INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN entry_trigger_deceleration INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN entry_trigger_max_range_ratio REAL NOT NULL DEFAULT 0.5",
        # Section 5.2: the meta-labeller, which replaces the R regression.
        "ALTER TABLE vantage_risk_settings ADD COLUMN meta_label_gate_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN meta_label_threshold REAL NOT NULL DEFAULT 0.5",
        # Section 5.6: illiquidity that arrives on a clock, and per-tier
        # event windows.
        "ALTER TABLE vantage_risk_settings ADD COLUMN session_liquidity_gate_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN event_tier_gate_enabled INTEGER NOT NULL DEFAULT 0",
        # Section 5.4. The cap is in lots and 0 means OFF, matching every
        # other cap in this table.
        "ALTER TABLE vantage_risk_settings ADD COLUMN vol_target_sizing_enabled INTEGER NOT NULL DEFAULT 0",
        "ALTER TABLE vantage_risk_settings ADD COLUMN correlated_exposure_cap_lots REAL NOT NULL DEFAULT 0",
        # Section 4.1: the previous-day/week, VWAP and initial-balance
        # levels joining the candidate list.
        "ALTER TABLE vantage_risk_settings ADD COLUMN liquidity_map_levels_enabled INTEGER NOT NULL DEFAULT 0",
    ]),

    # The measured 0.372R round trip (2026-09-11) was one number covering
    # two causes with different fixes: the fill against what the broker was
    # QUOTING, and that quote against the price the decision was made at.
    # The second is not the broker's doing -- it is the signal chasing or
    # arriving late. They sum to slippage_pts, so nothing already recorded
    # changes meaning. See services/broker/tca.py.
    (42, "Split measured slippage into broker slippage and entry drift", [
        "ALTER TABLE execution_quality ADD COLUMN broker_slippage_pts REAL",
        "ALTER TABLE execution_quality ADD COLUMN entry_drift_pts REAL",
    ]),

    # Refuse a named level type. The first thing the live study measured
    # that nothing in the app could act on: score_level rates round_5
    # highest of all at 0.78 and it is the worst cohort on the book (210
    # trades, -0.157R, -$1,323), because those weights were fitted against
    # a Telegram channel's behaviour rather than against outcome. Empty,
    # so nothing is refused until somebody names it.
    (43, "Per-level-type refusal list, empty by default", [
        "ALTER TABLE vantage_risk_settings ADD COLUMN re_blocked_level_types TEXT NOT NULL DEFAULT ''",
    ]),
]

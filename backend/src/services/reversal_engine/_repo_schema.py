"""The reversal engine's own tables -- the schema half of its repo.

Named `_repo_schema` rather than `_schema` deliberately: the structure gate
identifies the data layer partly by filename (`*_repo.py`, `backend/src/db/`,
`backend/migrations/`), and SQL is allowed there and nowhere else. Splitting
DDL out under a name the gate does not recognise as data layer would have
reported it as SQL leaking into a service, which is exactly what that gate is
for. It is repo code, so it says so.

Lifted out of reversal_engine_repo.py, which sat 42 lines over the 800-line
ceiling with 127 of them here. Pure DDL: one blob, one caller.

Takes the db accessor as a parameter rather than importing it. The repo
imports this module, so reaching back for `get_db` would be a circular import
-- the same trap the frontend page splits hit twice.
"""
from __future__ import annotations

from typing import Any, Callable


def create_schema(get_db: Callable[[], Any]) -> None:
    get_db().exec("""
    CREATE TABLE IF NOT EXISTS re_signals (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at      REAL    NOT NULL,
        signal_ref      TEXT    UNIQUE,
        direction       TEXT,
        entry_low       REAL,
        entry_high      REAL,
        stop_loss       REAL,
        sl_dist         REAL,
        tp1             REAL,
        tp2             REAL,
        tp3             REAL,
        tp4             REAL,
        tp5             REAL,
        tp6             REAL,
        tp7             REAL,
        tp8             REAL,
        rr_tp1          REAL,
        level_price     REAL,
        level_type      TEXT,
        level_score     REAL,
        session         TEXT,
        htf_bias        TEXT,
        h1_bias         TEXT,
        atr             REAL,
        adx             REAL,
        price_at_signal REAL,
        status          TEXT    NOT NULL DEFAULT 'pending',
        outcome         TEXT    NOT NULL DEFAULT 'open',
        trigger_price   REAL,
        trigger_time    REAL,
        close_price     REAL,
        close_time      REAL,
        pnl_pts         REAL,
        pnl_dollars     REAL,
        net_pnl_dollars REAL,
        balance_after   REAL,
        sl_moved_to_be  INTEGER NOT NULL DEFAULT 0,
        ml_features_json TEXT,
        ml_prob         REAL,
        mt5_ticket      INTEGER,
        vantage_signal_id TEXT,
        live_exec_status TEXT,
        strategy        TEXT    DEFAULT 'signal_climber',
        correlated_ref_signal_id TEXT,
        correlation_time_delta_s REAL,
        correlation_distance_pts REAL,
        correlation_confirmed INTEGER NOT NULL DEFAULT 0
    );

    CREATE TABLE IF NOT EXISTS re_levels (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        updated_at  REAL,
        level_type  TEXT,
        price       REAL,
        direction   TEXT,
        strength    INTEGER DEFAULT 1,
        active      INTEGER DEFAULT 1,
        source      TEXT DEFAULT 'engine',
        notes       TEXT
    );

    CREATE TABLE IF NOT EXISTS re_correlation (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        date            TEXT UNIQUE,
        re_signals_sent INTEGER DEFAULT 0,
        re_correlated  INTEGER DEFAULT 0,
        ref_signals_sent INTEGER DEFAULT 0,
        ref_predicted   INTEGER DEFAULT 0,
        avg_lead_time_s REAL,
        correlation_rate REAL
    );

    CREATE TABLE IF NOT EXISTS re_analysis_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL,
        session     TEXT,
        htf_bias    TEXT,
        price       REAL,
        atr         REAL,
        adx         REAL,
        levels_json TEXT,
        result      TEXT,
        reason      TEXT
    );

    CREATE TABLE IF NOT EXISTS re_config (
        key     TEXT PRIMARY KEY,
        value   TEXT NOT NULL DEFAULT ''
    );

    CREATE TABLE IF NOT EXISTS re_balance_log (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        ts          REAL,
        balance     REAL,
        change_amt  REAL,
        reason      TEXT,
        signal_id   INTEGER
    );

    CREATE TABLE IF NOT EXISTS re_near_miss (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        ts              REAL,
        re_signal_id   INTEGER,
        ref_signal_id   TEXT,
        direction       TEXT,
        time_delta_s    REAL,
        distance_pts    REAL,
        reason          TEXT
    );

    CREATE TABLE IF NOT EXISTS re_daily_research (
        date                TEXT PRIMARY KEY,
        discipline_score    REAL,
        aggression_score    REAL,
        summary             TEXT,
        notable_trades      TEXT,
        entry_logic_notes   TEXT,
        risk_mgmt_notes     TEXT,
        n_messages          INTEGER,
        n_images_analyzed   INTEGER,
        raw_json            TEXT,
        created_at          REAL
    );
    """)

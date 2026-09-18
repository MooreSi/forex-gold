"""Storage for the Telegram decision log (stage 0 + stage 1).

Every function here is CRUD on two tables and nothing else -- the structure
gate identifies the data layer partly by filename, and SQL in anything not
called `*_repo` is reported as leaking into a service.

WHY THIS LIVES IN reversal_engine.db AND NOT THE CORE DB
--------------------------------------------------------
Same reason `pro_corpus_repo` gives, and it is not a preference. The core
database is per-environment -- `forex_trader_demo.db` on the demo account,
`forex_trader_live.db` on live -- so a corpus kept there splits in half on
the day the account switches, and the model silently trains on an empty
table. The RE database is one file regardless of environment, so
`account_env` is a COLUMN here: demo and live are separated when the
question is asked, not by accident.

WHAT A ROW IS
-------------
`tg_decisions`     one Telegram signal reaching one execution decision.
                   Executed or blocked -- the blocked half is free evidence
                   and the whole reason this is cheaper than a backtest.
`tg_shadow_decisions`
                   what one variant would have done with that same decision.

UNIQUE(tg_message_id, path) rather than a pre-check: the full-signal path
and the IME path are genuinely different decisions about one message and
both are worth keeping, while a rescan of the same one is not. The
constraint is what makes that safe against a scan loop racing itself.

Outcome columns stay NULL until `decision_outcomes` fills them, and NULL
means "not known yet", never "no result" -- `report()` must be able to tell
those apart or it will read an unresolved trade as a flat one.
"""
from __future__ import annotations

import logging
import sqlite3
import time
from typing import Any, Optional

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

log = logging.getLogger(__name__)

# How long a decision stays worth asking the ledger about. A trade that has
# not closed in fourteen days is one the sweep should stop retrying: the
# query is ORDER BY decided_at LIMIT n, so permanent residents accumulate at
# its head and eventually starve the rows behind them. Wide enough for a
# position held across a weekend and then some -- the longest real hold on
# record is hours, not days.
#
# Giving up leaves `outcome` NULL. That is the point: unknown stays unknown,
# and the report goes on excluding it rather than counting it as flat.
MAX_RESOLVE_AGE_S = 14 * 86400

# Column order used by insert_decision(). Append-only, like the corpus's.
_COLS = (
    "tg_message_id", "path", "channel_name", "direction", "decided_at",
    "executed", "skip_reason", "strategy", "entry_low", "entry_high",
    "stop_loss", "tp1", "bid", "ask", "spread_points", "price", "session",
    "trade_id", "account_env", "liquidity_blocked", "event_blocked",
    "source",
)


def create_schema() -> None:
    """Idempotent -- called on every startup after the RE db is opened."""
    re_db.get_db().exec("""
    CREATE TABLE IF NOT EXISTS tg_decisions (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        tg_message_id    TEXT NOT NULL,
        path             TEXT NOT NULL,          -- auto | ime
        channel_name     TEXT,
        direction        TEXT,
        decided_at       REAL NOT NULL,
        executed         INTEGER NOT NULL DEFAULT 0,
        skip_reason      TEXT,
        strategy         TEXT,
        entry_low        REAL,
        entry_high       REAL,
        stop_loss        REAL,
        tp1              REAL,
        bid              REAL,
        ask              REAL,
        spread_points    REAL,
        price            REAL,
        session          TEXT,
        trade_id         TEXT,
        account_env      TEXT,
        -- live | backfill. A reconstruction and an observation must never
        -- be indistinguishable: decision_backfill can recover the clock
        -- facts and the candle replay exactly, and nothing else. A table
        -- that hides which is which is one nobody can trust twice.
        source           TEXT NOT NULL DEFAULT 'live',
        -- Shadow facts computed inline, before any gate returned. See
        -- decision_log.inline_facts for why they are gathered up front.
        liquidity_blocked INTEGER,
        event_blocked     INTEGER,
        -- Filled by decision_outcomes from the core trade ledger. NULL
        -- means not known yet.
        outcome          TEXT,
        net_usd          REAL,
        realised_r       REAL,
        max_tp_hit       TEXT,
        exit_reason      TEXT,
        resolved_at      REAL,
        -- Filled by decision_shadow once every variant has an answer.
        shadow_evaluated_at REAL,
        UNIQUE(tg_message_id, path)
    );

    CREATE INDEX IF NOT EXISTS idx_tg_dec_at      ON tg_decisions(decided_at);
    CREATE INDEX IF NOT EXISTS idx_tg_dec_open    ON tg_decisions(outcome, executed);
    CREATE INDEX IF NOT EXISTS idx_tg_dec_shadow  ON tg_decisions(shadow_evaluated_at);

    CREATE TABLE IF NOT EXISTS tg_shadow_decisions (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        decision_id  INTEGER NOT NULL,
        variant      TEXT NOT NULL,
        -- NULL is an ABSTENTION, not a refusal: a variant whose fact was
        -- unavailable has no opinion. Storing it as 0 would report a
        -- refusal rate that says nothing about the variant.
        would_take   INTEGER,
        reason       TEXT,
        evaluated_at REAL NOT NULL,
        UNIQUE(decision_id, variant)
    );

    CREATE INDEX IF NOT EXISTS idx_tg_shadow_dec ON tg_shadow_decisions(decision_id);
    """)
    # Idempotent, and here rather than in the CREATE alone so a database
    # that already has the table from an earlier build gains the column
    # instead of silently failing every insert that names it.
    try:
        re_db.get_db().run(
            "ALTER TABLE tg_decisions ADD COLUMN source TEXT NOT NULL DEFAULT 'live'")
    except sqlite3.OperationalError:
        pass  # already there


def insert_decision(row: dict) -> Optional[int]:
    """Write one decision, returning its id -- the existing row's id when
    that (message, path) is already recorded, so a caller can attach shadow
    rows either way without caring which happened."""
    marks = ",".join("?" for _ in _COLS)
    # `source` defaults here rather than only in the schema: a caller that
    # does not say is recording something it watched happen, and an explicit
    # NULL would fail the NOT NULL constraint instead of taking the default.
    values = [row.get(c) for c in _COLS]
    values[_COLS.index("source")] = row.get("source") or "live"
    try:
        re_db.get_db().run(
            f"INSERT OR IGNORE INTO tg_decisions ({','.join(_COLS)}) VALUES ({marks})",
            *values,
        )
        got = re_db.get_db().get(
            "SELECT id FROM tg_decisions WHERE tg_message_id=? AND path=?",
            str(row.get("tg_message_id") or ""), str(row.get("path") or ""),
        )
    except sqlite3.OperationalError as exc:
        log.debug("[DecisionLog] insert failed: %s", exc)
        return None
    return int(got["id"]) if got else None


def rows(limit: int = 5000) -> list[dict]:
    out = re_db.get_db().all(
        "SELECT * FROM tg_decisions ORDER BY decided_at DESC LIMIT ?", limit)
    return [dict(r) for r in out]


def unresolved_outcomes(limit: int = 200,
                        now: Optional[float] = None) -> list[dict]:
    """Executed decisions with a trade, no outcome yet, and recent enough to
    still be worth asking about.

    Two exclusions, for the same reason: a queue this sweep can never empty
    is a queue that stops working. A decision with no trade has nothing to
    resolve and never enters it; a decision older than MAX_RESOLVE_AGE_S
    leaves it unresolved rather than being retried forever.
    """
    cutoff = (time.time() if now is None else now) - MAX_RESOLVE_AGE_S
    out = re_db.get_db().all(
        "SELECT * FROM tg_decisions WHERE executed=1 AND trade_id IS NOT NULL "
        "AND outcome IS NULL AND decided_at >= ? ORDER BY decided_at LIMIT ?",
        cutoff, limit)
    return [dict(r) for r in out]


def set_outcome(decision_id: int, *, outcome: str, net_usd: Optional[float],
                realised_r: Optional[float], max_tp_hit: Optional[str],
                exit_reason: Optional[str], now: Optional[float] = None) -> None:
    re_db.get_db().run(
        "UPDATE tg_decisions SET outcome=?, net_usd=?, realised_r=?, "
        "max_tp_hit=?, exit_reason=?, resolved_at=? WHERE id=?",
        outcome, net_usd, realised_r, max_tp_hit, exit_reason,
        time.time() if now is None else now, int(decision_id),
    )


def unevaluated_shadow(limit: int = 200) -> list[dict]:
    out = re_db.get_db().all(
        "SELECT * FROM tg_decisions WHERE shadow_evaluated_at IS NULL "
        "ORDER BY decided_at LIMIT ?", limit)
    return [dict(r) for r in out]


def mark_shadow_evaluated(decision_id: int, now: Optional[float] = None) -> None:
    re_db.get_db().run(
        "UPDATE tg_decisions SET shadow_evaluated_at=? WHERE id=?",
        time.time() if now is None else now, int(decision_id))


def insert_shadow(decision_id: int, variant: str, would_take: Optional[bool],
                  reason: str, now: Optional[float] = None) -> None:
    """INSERT OR IGNORE, so a retried evaluation cannot double-count.
    `would_take=None` is stored as NULL and means the variant abstained."""
    value: Any = None if would_take is None else int(bool(would_take))
    try:
        re_db.get_db().run(
            "INSERT OR IGNORE INTO tg_shadow_decisions "
            "(decision_id, variant, would_take, reason, evaluated_at) "
            "VALUES (?,?,?,?,?)",
            int(decision_id), variant, value, reason or "",
            time.time() if now is None else now,
        )
    except sqlite3.OperationalError as exc:
        log.debug("[DecisionLog] shadow insert failed: %s", exc)


def shadow_rows_for(decision_id: int) -> list[dict]:
    out = re_db.get_db().all(
        "SELECT * FROM tg_shadow_decisions WHERE decision_id=? ORDER BY variant",
        int(decision_id))
    return [dict(r) for r in out]


def decision_counts() -> dict:
    """Totals in one round trip: how many decisions, how many executed, how
    many already resolved, and how many are reconstructions."""
    got = re_db.get_db().get(
        "SELECT COUNT(*) AS total, "
        "       SUM(executed) AS executed, "
        "       SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS resolved, "
        "       SUM(CASE WHEN source='backfill' THEN 1 ELSE 0 END) AS reconstructed "
        "FROM tg_decisions")
    return dict(got) if got else {}


def counts_by_path() -> list[dict]:
    out = re_db.get_db().all(
        "SELECT path, "
        "       SUM(executed) AS executed, "
        "       SUM(CASE WHEN executed=0 THEN 1 ELSE 0 END) AS blocked "
        "FROM tg_decisions GROUP BY path ORDER BY path")
    return [dict(r) for r in out]


def counts_by_reason(limit: int = 10) -> list[dict]:
    """Why decisions were declined, commonest first. Executed rows carry an
    empty reason and are excluded -- an unlabelled bar at the top of this
    list would be the most misleading thing on the page."""
    out = re_db.get_db().all(
        "SELECT skip_reason AS reason, COUNT(*) AS n FROM tg_decisions "
        "WHERE executed=0 AND skip_reason IS NOT NULL AND skip_reason <> '' "
        "GROUP BY skip_reason ORDER BY n DESC, reason LIMIT ?", limit)
    return [dict(r) for r in out]


def closed_shadow_decisions() -> list[dict]:
    """Every shadow row whose decision has a resolved outcome, with that
    outcome attached. The join is here rather than in the service because
    the service may not hold SQL."""
    out = re_db.get_db().all(
        "SELECT s.variant, s.would_take, d.executed, d.outcome, d.net_usd, "
        "       d.realised_r "
        "FROM tg_shadow_decisions s JOIN tg_decisions d ON d.id = s.decision_id "
        "WHERE d.outcome IS NOT NULL")
    return [dict(r) for r in out]

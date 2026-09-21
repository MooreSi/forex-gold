"""Storage for the contradiction study.

CRUD on two tables and nothing else -- the structure gate identifies the
data layer partly by filename, and SQL in anything not called `*_repo` is
reported as leaking into a service.

It lives in `reversal_engine.db` for the reason `decision_log_repo` and
`pro_corpus_repo` both give, and it is not a preference: the core database
is per-environment (`forex_trader_demo.db` / `forex_trader_live.db`), so a
corpus kept there splits in half the day the account switches, and the
study silently restarts from zero. One file across both, with `account_env`
as a COLUMN, so demo and live separate when the question is asked rather
than by accident.

WHAT A ROW IS
-------------
`signal_contradictions`
    one signal, at the moment it was recorded, together with whatever was
    already active against it. A signal with NOTHING against it is still a
    row -- it is the denominator, and without it every policy looks like it
    fires constantly.
`signal_contradiction_verdicts`
    what one policy would have done with that signal.

`action` NULL is an ABSTENTION, not an allow. A policy whose fact was
unavailable has no opinion; storing it as "allow" would report an approval
rate that says nothing about the policy. Same rule, and the same reason, as
`tg_shadow_decisions.would_take`.

UNIQUE(candidate_ref, source_name) rather than a pre-check: the scan loop
re-sees the same message about once a second, and the constraint is what
makes that safe without a read before every write.
"""
from __future__ import annotations

import json
import logging
import sqlite3
from typing import Any, Optional

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

log = logging.getLogger(__name__)


def create_schema() -> None:
    """Idempotent -- called on every startup after the RE db is opened."""
    re_db.get_db().exec("""
    CREATE TABLE IF NOT EXISTS signal_contradictions (
        id             INTEGER PRIMARY KEY AUTOINCREMENT,
        observed_at    REAL NOT NULL,
        account_env    TEXT,
        candidate_ref  TEXT NOT NULL,
        source_kind    TEXT,
        source_name    TEXT,
        direction      TEXT,
        symbol         TEXT,
        opposing_count INTEGER NOT NULL DEFAULT 0,
        -- The opposing set as it stood, not a join to rows that expire.
        -- signal_bus is pruned every 30 minutes; a study that read it back
        -- later would find its own evidence deleted.
        opposing_json  TEXT,
        UNIQUE(candidate_ref, source_name)
    );

    CREATE INDEX IF NOT EXISTS idx_sig_contra_at
        ON signal_contradictions(observed_at);

    CREATE TABLE IF NOT EXISTS signal_contradiction_verdicts (
        id               INTEGER PRIMARY KEY AUTOINCREMENT,
        contradiction_id INTEGER NOT NULL,
        policy           TEXT NOT NULL,
        -- NULL is an abstention. See the module docstring.
        action           TEXT,
        reason           TEXT,
        lot_mult         REAL,
        evaluated_at     REAL NOT NULL,
        UNIQUE(contradiction_id, policy)
    );

    CREATE INDEX IF NOT EXISTS idx_sig_contra_verdict
        ON signal_contradiction_verdicts(contradiction_id);
    """)


def insert_observation(row: dict) -> Optional[int]:
    """Returns the new row id, or None when this candidate is already in.

    None is the ordinary case, not an error: the scan loop re-sees a message
    for as long as it stays in the reader's fetch window.
    """
    try:
        result = re_db.get_db().run(
            "INSERT OR IGNORE INTO signal_contradictions"
            "(observed_at,account_env,candidate_ref,source_kind,source_name,"
            " direction,symbol,opposing_count,opposing_json)"
            " VALUES(?,?,?,?,?,?,?,?,?)",
            row.get("observed_at"), row.get("account_env"),
            row.get("candidate_ref"), row.get("source_kind"),
            row.get("source_name"), row.get("direction"), row.get("symbol"),
            int(row.get("opposing_count") or 0),
            json.dumps(row.get("opposing") or []),
        )
    except sqlite3.Error as _e:
        log.debug("[Contradiction] observation write failed: %s", _e)
        return None
    # OR IGNORE leaves lastrowid unset when the candidate was already in --
    # the ordinary case on a rescan, not an error. Nothing more to attach.
    return int(result.lastrowid) if result.lastrowid else None


def insert_verdict(contradiction_id: int, policy: str, action: Optional[str],
                   reason: str, lot_mult: float, evaluated_at: float) -> None:
    """Idempotent per (observation, policy)."""
    try:
        re_db.get_db().run(
            "INSERT OR IGNORE INTO signal_contradiction_verdicts"
            "(contradiction_id,policy,action,reason,lot_mult,evaluated_at)"
            " VALUES(?,?,?,?,?,?)",
            contradiction_id, policy, action, reason, lot_mult, evaluated_at,
        )
    except sqlite3.Error as _e:
        log.debug("[Contradiction] verdict write failed: %s", _e)


def rows(limit: int = 500) -> list[dict]:
    return [dict(r) for r in re_db.get_db().all(
        "SELECT * FROM signal_contradictions ORDER BY observed_at DESC LIMIT ?",
        limit)]


def verdicts(contradiction_id: int) -> list[dict]:
    return [dict(r) for r in re_db.get_db().all(
        "SELECT * FROM signal_contradiction_verdicts"
        " WHERE contradiction_id = ? ORDER BY policy", contradiction_id)]


def report(account_env: Optional[str] = None) -> list[dict]:
    """Per policy: how often each verdict came up.

    `observations` is the denominator and is the same for every policy --
    the count of signals seen, not of contradictions found. Reading a
    `blocked` count without it says nothing.

    `abstained` counts the NULL actions separately for the reason the column
    exists at all: they are not approvals.
    """
    where, params = "", []
    if account_env:
        where = " WHERE c.account_env = ?"
        params.append(account_env)

    total = re_db.get_db().get(
        f"SELECT COUNT(*) AS n FROM signal_contradictions c{where}", *params)
    observations = int(dict(total)["n"]) if total else 0

    got = re_db.get_db().all(
        "SELECT v.policy AS policy, v.action AS action, COUNT(*) AS n"
        " FROM signal_contradiction_verdicts v"
        " JOIN signal_contradictions c ON c.id = v.contradiction_id"
        + where +
        " GROUP BY v.policy, v.action", *params)

    by_policy: dict[str, dict[str, Any]] = {}
    for raw in got:
        r = dict(raw)
        entry = by_policy.setdefault(
            r["policy"], {"policy": r["policy"], "observations": observations,
                          "allow": 0, "block": 0, "shrink": 0,
                          "supersede": 0, "abstained": 0})
        key = r["action"] if r["action"] else "abstained"
        entry[key] = entry.get(key, 0) + int(r["n"])
    return sorted(by_policy.values(), key=lambda e: e["policy"])

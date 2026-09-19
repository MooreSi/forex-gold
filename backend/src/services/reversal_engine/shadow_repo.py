"""The SQL half of `reversal_engine/shadow.py`.

A separate module because SQL belongs in the data layer and the structure
gate enforces that by filename. The table itself is created in
`_repo_schema.py` with everything else the engine owns.
"""
from __future__ import annotations

import time

from backend.src.services.reversal_engine.reversal_engine_repo import get_db


def insert_decision(signal_ref: str, variant: str, would_take: bool,
                    reason: str) -> None:
    """INSERT OR IGNORE, so a retried fill attempt cannot double-count
    whichever variant the retry happened to favour."""
    get_db().run(
        "INSERT OR IGNORE INTO re_shadow_decisions "
        "(ts, signal_ref, variant, would_take, reason) VALUES (?,?,?,?,?)",
        time.time(), signal_ref, variant, 1 if would_take else 0, reason)


def decisions_for(signal_ref: str) -> list[dict]:
    rows = get_db().all(
        "SELECT variant, would_take, reason FROM re_shadow_decisions "
        "WHERE signal_ref=?", signal_ref)
    return [dict(r) for r in rows]


def closed_decisions() -> list[dict]:
    """Every shadow decision whose signal has since closed, with the
    outcome needed to score it."""
    rows = get_db().all(
        "SELECT d.variant AS variant, d.would_take AS would_take, "
        "       s.pnl_pts AS pnl_pts, s.sl_dist AS sl_dist, "
        "       s.net_pnl_dollars AS net "
        "FROM re_shadow_decisions d "
        "JOIN re_signals s ON s.signal_ref = d.signal_ref "
        "WHERE s.status='closed'")
    return [dict(r) for r in rows]


def recent_decisions(limit: int = 100) -> list[dict]:
    """The newest shadow decisions, with what each one would have earned.

    `shadow.report()` aggregates this to one row per variant, which answers
    "which variant is ahead" and cannot answer "what did it do last Tuesday,
    and was it right". The Signal Generator's virtual trade history is the
    second question.

    **A SKIP is a row, not an absence.** A variant that skipped a losing trade
    and one that never saw it both contribute nothing to the P&L, and only one
    of them is evidence. The signal's outcome is carried on a skipped row too,
    because a skip on a loser is the variant being right.

    `r` is NULL for a signal that has not closed, never 0.0 -- zero R and "not
    settled yet" are different statements, and a table rendering both as 0.00
    invites the wrong one to be acted on. Same rule `report()` states about
    `mean_r`.
    """
    rows = get_db().all(
        "SELECT d.ts AS ts, d.signal_ref AS signal_ref, d.variant AS variant, "
        "       d.would_take AS would_take, d.reason AS reason, "
        "       s.direction AS direction, s.status AS status, "
        "       s.outcome AS outcome, s.sl_dist AS sl_dist, "
        "       s.pnl_pts AS pnl_pts, s.net_pnl_dollars AS net, "
        "       CASE WHEN s.status='closed' AND s.sl_dist > 0 "
        "            THEN s.pnl_pts / s.sl_dist END AS r "
        "FROM re_shadow_decisions d "
        "JOIN re_signals s ON s.signal_ref = d.signal_ref "
        "ORDER BY d.ts DESC LIMIT ?", limit)
    return [dict(r) for r in rows]

"""One read: how long since this engine produced a signal, and what refused.

Its own module rather than a function in `test_signal_repo`, because that file
sits at 766 of the 800-line ceiling and is on the shrink-only LOC baseline --
adding to it is a structural regression, whatever the function does.

Named `*_repo` deliberately: SQL is allowed in the data layer and nowhere else
(`tools/refactor_audit/structure_gates.is_repo_file`), and this is SQL.
"""
from __future__ import annotations

import time
from typing import Optional

from backend.src.services.test_signal.test_signal_repo import get_db


def get_silence_report(now: Optional[float] = None) -> dict:
    """How long since this engine produced a signal, and what refused since.

    A signal generator that has found nothing looks exactly like one that is
    refusing everything: both show an empty list and a panel that says
    "running". On 2026-09-12 the difference turned out to be sixteen days and
    424 refused candidates (`docs/simon-handover/034`), and nothing on screen
    distinguished the two.

    Returns `last_signal_at`, `silent_secs` (both None if the engine has never
    produced one), `candidates` -- analysis cycles since then that actually
    found something to judge -- and `refusals`, those cycles grouped by the
    result that stopped them, commonest first.

    **A cycle counts as a candidate when its row carries one**, not by matching
    a list of known gate names. A taxonomy here would go stale the first time
    someone adds a gate, and go stale silently, in the very function whose job
    is to say what is stopping things.

    `no_trigger` rows are therefore excluded and should be: they outnumber
    everything else by an order of magnitude and mean the engine found nothing
    to judge, which is the market, not a gate.
    """
    now = time.time() if now is None else now
    last_row = get_db().get("SELECT MAX(created_at) FROM test_signals")
    last = last_row[0] if last_row else None
    since = last if last is not None else 0.0
    rows = get_db().all(
        "SELECT result, COUNT(*) FROM test_analysis_log "
        "WHERE ts > ? AND candidate_json IS NOT NULL "
        "AND candidate_json NOT IN ('', 'null', '{}') "
        "GROUP BY result ORDER BY COUNT(*) DESC, result ASC",
        since,
    )
    refusals = [(r[0] or "", int(r[1])) for r in rows]
    return {
        "last_signal_at": last,
        "silent_secs": (now - last) if last is not None else None,
        "candidates": sum(n for _, n in refusals),
        "refusals": refusals,
    }

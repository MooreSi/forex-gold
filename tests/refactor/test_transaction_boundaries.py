"""Multi-write repo functions declare their transaction boundary.

`transaction` is a plain alias of `db()` — the outermost block already
commits atomically, so swapping one for the other changes no behaviour
whatsoever. What it changes is legibility: a function that performs
several writes and opens `with db() as conn:` looks exactly like one that
performs a single read. Spelling it `with transaction() as conn:` states
that the writes inside are meant to land together, and lets the structure
gate count the ones that have not said so.

This pins the remaining offenders as a shrink-only set, so the count
cannot quietly grow back the next time a repo function gains a second
write.
"""
from __future__ import annotations

import pytest

from tools.refactor_audit import structure_gates as sg


# What remains, and why. This is a shrink-only pin: converting one of
# these means deleting it from here, and a NEW undeclared multi-write
# function fails the first test below.
ALLOWED = {
    # Builds the database itself, before any repo exists, and cannot import
    # the alias without a cycle. Runs once at startup; its writes are
    # CREATE TABLE statements, not trading data.
    "backend/src/db/database.py": ["_apply_schema"],

    # These live in the per-engine research databases, not the trading DB,
    # and they do not use db() at all -- they use each engine's own _conn(),
    # which opens a FRESH connection per call and does not nest. So the
    # alias cannot simply be swapped in: close_signal genuinely opens three
    # separate connections with a balance update between them, and making
    # it atomic means giving _conn() the depth-counting behaviour db() has.
    # That is a change to an engine's data layer, not a rename, so it is
    # recorded rather than half-done. See OPEN_QUESTIONS.md.
    "backend/src/services/reversal_engine/database.py": [
        "close_signal", "upsert_daily_correlation", "upsert_level",
    ],
    "backend/src/services/test_signal/database.py": ["insert_signal"],
}


def test_no_service_repo_has_an_undeclared_multi_write_function():
    report = sg.transaction_report()
    offenders = {
        path: sorted(set(functions) - set(ALLOWED.get(path, [])))
        for path, functions in report.items()
    }
    offenders = {p: fns for p, fns in offenders.items() if fns}
    assert offenders == {}, (
        "these functions perform several writes without declaring a "
        "transaction boundary; use transaction() so the writes are stated "
        "to land together:\n  "
        + "\n  ".join(f"{p}: {', '.join(fns)}" for p, fns in offenders.items())
    )


def test_the_main_trading_database_has_no_undeclared_multi_write_repos():
    """The one that matters. Everything under services/ that writes to the
    trading DB now declares its boundary -- the engine research DBs above
    are a separate, larger job."""
    report = sg.transaction_report()
    assert "backend/src/services/ai/recovered_repo.py" not in report or not report[
        "backend/src/services/ai/recovered_repo.py"
    ], "recovered_repo was converted in this pass; it must stay converted"


def test_the_detector_still_detects():
    """Negative control: an empty offender list is only meaningful if the
    detector can still find something. It finds the sanctioned exception."""
    report = sg.transaction_report()
    assert any(functions for functions in report.values()), (
        "transaction_report found nothing at all -- the detector is broken, "
        "not the codebase clean"
    )

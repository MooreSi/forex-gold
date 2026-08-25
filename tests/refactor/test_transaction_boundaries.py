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
    # Private helper that never opens a connection: it executes on the one its
    # caller hands it, inside that caller's boundary. Both callers declare one --
    # sync_channel_rename() uses transaction() (changed from db() by the
    # 2026-08-25 merge, so a half-applied rename cascade can no longer strand
    # rows), and backfills.run() executes under _apply_schema's. Arrived with
    # that merge, from upstream's core_db_channel. The gate reads one function
    # at a time and cannot see an inherited boundary, which is what this entry
    # records.
    "backend/src/services/channels/repo.py": ["_fold_renamed_row"],
    # (The per-engine research-database clones that were recorded here were
    # deleted 2026-08-10 — dead code, superseded by their *_repo.py.)
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

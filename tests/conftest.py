"""Canonical shared fixtures for the test suite.

**Why this exists.** `fresh_db` is currently defined 119 times across the suite,
in 17 distinct variants, each reaching directly into `database.py` internals --
`db._thread_local`, `db._db_executor`, `db._rs_cache`. Every one of those is
private state the refactor is going to move. When two dicts were once merged
into a single `TPCache`, twelve test files broke at once; relocating the DB
connection machinery into `src/db/` will break far more, and today that means
119 edits instead of one.

**What this is not, yet.** The 119 local definitions are deliberately left in
place. A fixture defined in a test module shadows the conftest one, so this file
changes nothing about how existing tests run -- it is additive, and was verified
to leave the same tests passing and failing as before it existed. Migration has
to happen file by file with the differences actually read: the 17 variants are
not interchangeable, and rewriting them mechanically against a suite whose
baseline is not currently trustworthy (see
docs/todo/refactor/phase-0-audit/QUESTIONS.md, Q1) is how you introduce a silent
regression while believing you are tidying up.

**How to use it.** New tests should take `fresh_db` and `make_engine` from here
and define nothing locally. When you touch a file that has its own copy, diff it
against this one; if it matches, delete the local copy and its helpers.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from forex_trader.core import database as db


def reset_thread_local_connection() -> None:
    """Drop the calling thread's cached sqlite connection and nesting depth."""
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def reset_db_worker_thread_connection() -> None:
    """Same, but inside the DB executor thread.

    `to_db_thread` dispatches onto a worker with its own thread-local, so
    resetting only the caller's leaves a connection open on the old file and the
    next test reads stale data.
    """
    db._db_executor.submit(reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    """A private, empty database for one test, torn down afterwards.

    This is the 49-file majority variant, reproduced exactly rather than
    improved -- the point is a single definition, and changing behaviour at the
    same time would make any resulting failure ambiguous.
    """
    reset_thread_local_connection()
    reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    reset_thread_local_connection()
    reset_db_worker_thread_connection()
    os.remove(path)


@pytest.fixture
def make_engine():
    """Builds a SimulationEngine without running __init__.

    Every characterization test does `SimulationEngine.__new__(SimulationEngine)`
    then assigns private attributes by hand -- `_bridge`, `_cfg`, `_tp_cache`,
    `_dpm_cache` and so on. Those names are exactly what the refactor relocates
    when the god object becomes a task supervisor, so routing construction
    through one factory makes that relocation one edit rather than a hundred.

        def test_x(make_engine):
            engine = make_engine(_bridge=FakeBridge(), _tp_cache={})
    """
    from forex_trader.core.engine import SimulationEngine

    def _build(**attrs):
        engine = SimulationEngine.__new__(SimulationEngine)
        for name, value in attrs.items():
            setattr(engine, name, value)
        return engine

    return _build

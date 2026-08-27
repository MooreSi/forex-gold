"""close_all() closes every namespace, not just the default.

The adapter cache in backend/src/db/connection.py is keyed by NAMESPACE, and
the three engines each carry a private one (reversal_engine_repo,
breakout_signal_repo, test_signal_repo all call init_db with their own).
init_db already closes the namespace it is replacing -- see its docstring --
but re-pointing the default leaves every engine adapter untouched, still
holding an open connection to whatever file it was last given.

POSIX hides this: the file unlinks anyway. Windows does not, which is how it
surfaced -- CI run 33117372693 reported connections to *previous tests'* temp
databases still alive and accumulating as the run went on.

It matters outside tests too: the demo/live env switch re-inits the default
namespace only, so the engine adapters keep a connection to the old
environment's file until the process restarts.
"""
from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

from backend.src.db import connection as conn_mod


@pytest.fixture
def temp_dbs():
    paths = []
    for _ in range(3):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        paths.append(path)
    yield paths
    conn_mod.close_all()
    for p in paths:
        try:
            os.remove(p)
        except FileNotFoundError:
            pass


def test_it_closes_every_namespace_not_just_the_default(temp_dbs):
    a, b, c = temp_dbs
    conn_mod.init_db(a)                      # the default namespace
    conn_mod.init_db(b, "reversal_engine")
    conn_mod.init_db(c, "breakout_signal")

    conn_mod.close_all()

    for ns in ("_default", "reversal_engine", "breakout_signal"):
        with pytest.raises(RuntimeError):
            conn_mod.get_db(ns)


def test_re_initing_the_default_leaves_engine_adapters_open(temp_dbs):
    """The behaviour that made this necessary, pinned so the reason survives.

    This is not a bug in init_db -- it closes what it replaces. It is that
    'replace the main database' and 'close everything' are different
    operations, and teardown needs the second.
    """
    a, b, c = temp_dbs
    conn_mod.init_db(a)
    conn_mod.init_db(b, "reversal_engine")

    conn_mod.init_db(c)   # re-point the default only

    assert conn_mod.get_db("reversal_engine") is not None, (
        "re-initing the default namespace should not disturb an engine's"
    )


def test_the_underlying_connection_is_really_closed(temp_dbs):
    """Not just dropped from the dict -- the sqlite handle has to be shut, or
    Windows still pins the file."""
    a = temp_dbs[0]
    adapter = conn_mod.init_db(a, "reversal_engine")
    raw = adapter._con
    raw.execute("SELECT 1")          # precondition: it is usable

    conn_mod.close_all()

    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")


def test_calling_it_twice_is_not_an_error(temp_dbs):
    conn_mod.init_db(temp_dbs[0], "reversal_engine")
    conn_mod.close_all()
    conn_mod.close_all()


def test_calling_it_with_nothing_open_is_not_an_error():
    conn_mod.close_all()
    conn_mod.close_all()

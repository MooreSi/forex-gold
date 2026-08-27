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
import time

import pytest

from backend.src.db import database as db
import logging
import logging.handlers
from backend.src.services.dpm import engine as _dpm
from backend.src.utils import news_calendar as _nc


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


@pytest.fixture(autouse=True)
def _isolate_risk_settings_cache():
    """Clear the risk-settings cache around every test.

    `core_db_risk_settings.get_risk_settings()` caches its result for
    `_RS_CACHE_TTL = 10.0` seconds, keyed on nothing but time. Each test builds
    its own temp database, so a test that only *reads* settings within ten
    seconds of a previous one silently receives the previous test's values --
    from a database file that has already been deleted. `update_risk_settings`
    invalidates the cache, so tests that write are safe; tests that read are not.

    That made the suite timing-dependent rather than order-dependent, which is
    why the failure count wandered between 20 and 58 across runs of identical
    code, and why the failures clustered in the slower `test_scan_messages_*`
    files. Bisecting for a single polluting file found nothing, because there
    isn't one.

    Autouse so it covers all 119 test modules regardless of which of the 17
    `fresh_db` variants they define locally. It only clears a cache, so it
    cannot change what any test asserts -- only which database answers it.
    """
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield
    db._rs_cache = None
    db._rs_cache_ts = 0.0


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
    remove_db_file(path)


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
    from backend.src.runtime import TradingRuntime

    def _build(**attrs):
        engine = TradingRuntime.__new__(TradingRuntime)
        for name, value in attrs.items():
            setattr(engine, name, value)
        return engine

    return _build


# ─────────────────────────────────────────────────────────────────────────────
# Merged in from MooreSi/forex on 2026-08-25. Both halves are load-bearing:
# ours supplies fresh_db / make_engine / the risk-settings cache isolation that
# 119 test modules rely on; upstream's pins the news feed and the market week
# so the suite stops depending on the world. See the module docstrings above
# and upstream's own notes reproduced with each fixture below.
# ─────────────────────────────────────────────────────────────────────────────


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "live_news: test deliberately uses the live economic calendar feed "
        "(exempt from the offline stub in tests/conftest.py)",
    )
    config.addinivalue_line(
        "markers",
        "live_market_hours: test deliberately reads the real calendar week "
        "(exempt from the market-open stub in tests/conftest.py)",
    )


@pytest.fixture(autouse=True)
def _offline_news_calendar(request, monkeypatch, tmp_path):
    """Keep the calendar offline and the blackout out of the way by default.

    This neutralises the module's *inputs* rather than replacing _fetch_raw or
    get_blackout_settings themselves, so the tests that exist to exercise those
    two functions still get the real ones. They stub urlopen and config.get on
    their own account, after this fixture, and their setup wins.
    """
    if "live_news" in request.keywords:
        return

    # A primed (empty) cache dated far in the future: the real _fetch_raw
    # returns on its first line and never builds a request.
    monkeypatch.setattr(_nc, "_cache_events", [], raising=False)
    monkeypatch.setattr(_nc, "_next_fetch_ts", float("inf"), raising=False)

    # The rest of the module's cross-request state. These are module globals,
    # so without this a failure in one test would change the backoff another
    # test computes, and the on-disk cache would let the developer's own
    # machine answer a test that is supposed to have no data.
    monkeypatch.setattr(_nc, "_fail_streak", 0, raising=False)
    monkeypatch.setattr(_nc, "_validators", {}, raising=False)
    monkeypatch.setattr(_nc, "_disk_loaded", False, raising=False)
    monkeypatch.setattr(
        _nc, "_disk_cache_file",
        lambda: tmp_path / "news_calendar_cache.json",
    )

    # check_news_blackout() short-circuits on `enabled` before it looks at any
    # event. Turning it off through config leaves get_blackout_settings itself
    # real, including its clamping of the padding values. Every other key is
    # delegated, so this is invisible to tests that read unrelated config.
    import backend.src.config as _cfg
    _real_get = _cfg.get

    def _get(key, default=None):
        if key == "news_blackout_enabled":
            return False
        return _real_get(key, default)

    monkeypatch.setattr(_cfg, "get", _get)


@pytest.fixture(autouse=True)
def _market_week_open(request, monkeypatch):
    """Pin the forex week open so the suite means the same thing on a Sunday.

    Only the weekly close is neutralised, not dpm_engine.detect_session():
    that one maps hour-of-day onto asian/london/overlap/ny, and all three
    session toggles ship enabled (vantage_risk_settings.session_*_enabled
    DEFAULT 1), so whatever hour the suite runs at is allowed. "closed" is the
    single value that fails, and it comes only from here.

    Patched in two places because core_closed_market_queue binds the function
    by name at import time, so patching the dpm_engine attribute alone would
    leave that module holding the real one.
    """
    if "live_market_hours" in request.keywords:
        return

    monkeypatch.setattr(_dpm, "is_weekly_market_closed", lambda now=None: False)

    from backend.src.services.positions import core_closed_market_queue as _cmq
    monkeypatch.setattr(_cmq, "is_weekly_market_closed", lambda now=None: False)


@pytest.fixture(autouse=True)
def _never_write_to_the_apps_log():
    """Detach any file handler aimed at the user data directory.

    Importing `run` used to attach a rotating file handler to the ROOT logger
    at module scope, pointed at the LIVE app's forex_trader.log -- and
    tests/test_claim_port.py imports it, so every pytest session wrote into
    the log of whatever app instance was running at the time. On 2026-08-07
    that put five WARNINGs about an EA outage and a failed terminal restart
    into the production log; none of it happened, the durations came from a
    fixture and the exception was injected. A log that invents outages is
    worse than no log, because it is read precisely when something is wrong.
    Two processes were also sharing one TimedRotatingFileHandler, so both
    would try to perform the midnight rename.

    run.setup_logging() is now called from main() rather than at import, which
    fixes that at the source. This is the guard that stops the next one, and
    it runs per-test rather than per-session so a handler attached midway
    through a run is gone again by the next test. Handlers writing anywhere
    else -- a tmp_path, caplog's own -- are left alone.
    """
    from backend.src.config import USER_DATA_DIR
    target = str(USER_DATA_DIR)

    removed = []
    for name in [None] + list(logging.root.manager.loggerDict):
        logger = logging.getLogger(name) if name else logging.getLogger()
        if not isinstance(logger, logging.Logger):
            continue
        for h in list(logger.handlers):
            path = getattr(h, "baseFilename", None)
            if path and target in str(path):
                logger.removeHandler(h)
                h.close()
                removed.append(str(path))
    if removed:
        # Loud on purpose: something reintroduced the import-time side effect.
        print(f"\n[conftest] detached {len(removed)} handler(s) writing to the "
              f"app's data dir: {sorted(set(removed))}")
    yield


def remove_db_file(path: str) -> None:
    """Delete a temp database file, tolerating Windows' handle semantics.

    POSIX unlinks a file whether or not anything still has it open. Windows
    refuses, with `PermissionError: [WinError 32]`. The first Windows CI run
    this repo ever completed turned that difference into 50 teardown errors,
    and a second run left 16 of them in fixtures that already close both the
    thread-local connection AND the db-worker's -- so the remaining handles
    belong to something those resets do not reach.

    The most likely candidate is a sqlite3.Connection caught in a reference
    cycle. CPython frees most objects the moment their refcount drops, but a
    cycle waits for the collector, so the connection stays open for an
    unpredictable while. On POSIX nobody ever noticed, because the unlink
    succeeded anyway.

    So: collect, then retry. If that clears it, the cause really was a cycle.
    If it does not, raise with the details -- how many connections are still
    alive and which files they point at -- because "the process cannot access
    the file" on its own costs a 25-minute CI round trip to learn nothing.

    Deliberately NOT a bare `except OSError: pass`. A leaked connection is a
    real defect; this makes it legible instead of silent.
    """
    import gc
    import sqlite3

    from backend.src.db import connection as _conn_mod

    # Close EVERY namespace, not just the default. The three engines
    # (reversal_engine, breakout_signal, test_signal) each cache their own
    # adapter under a private namespace, so re-pointing the main database
    # leaves theirs holding an open connection to the previous test's file.
    # That is what Windows CI run 33117372693 caught: connections to earlier
    # tests' temp databases, still alive and accumulating.
    _conn_mod.close_all()

    try:
        os.remove(path)
        return
    except PermissionError:
        pass

    gc.collect()
    for _ in range(10):
        try:
            os.remove(path)
            return
        except PermissionError:
            time.sleep(0.05)

    live = []
    for obj in gc.get_objects():
        if isinstance(obj, sqlite3.Connection):
            try:
                row = obj.execute("PRAGMA database_list").fetchone()
                live.append(row[2] if row else "<unknown>")
            except Exception as exc:
                live.append(f"<unreadable: {type(exc).__name__}>")

    raise AssertionError(
        f"could not delete {path} after gc + retries. "
        f"{len(live)} sqlite connection(s) still alive: {live}"
    )

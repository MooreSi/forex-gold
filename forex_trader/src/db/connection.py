"""Module-level DbAdapter registry -- generalizes the global-_DB_PATH
pattern reversal_engine/database.py already uses, for reuse across engines.
See docs/todo/refactor/backend-foundation/010-*.md.

Keyed by an explicit `namespace` string (default: a single shared slot,
same as the original single-adapter design) so that multiple engines can
each hold their own independent connection in the same process without
clobbering each other. Originally a single bare `_adapter` global -- fine
when only one engine's repo module was ever wired into a running app at a
time, but a real bug once a second engine's repo was wired alongside the
first: each `init_db()` call overwrote the same global, so only the
most-recently-initialized engine had a working connection and every other
engine's queries silently ran against the wrong database file (or raised
"no such table", depending on schema overlap). Confirmed live 2026-07-21
wiring breakout_signal + test_signal alongside the already-wired
reversal_engine. Each engine's own `<engine>_repo.py` wraps `get_db()`/
`init_db()` with its own fixed namespace baked in, so every existing
internal call site (`get_db().run(...)` etc., unchanged in call shape)
transparently gets its own connection with no other code needing to
change.
"""
from __future__ import annotations

import sqlite3
from typing import Optional

from .adapter import DbAdapter
from .sqlite_adapter import SqliteAdapter

_DEFAULT_NAMESPACE = "_default"
_adapters: dict[str, DbAdapter] = {}


def init_db(db_path: str, namespace: str = _DEFAULT_NAMESPACE) -> DbAdapter:
    """Open a real SQLite file and make it the active adapter for `namespace`.

    check_same_thread=False because this one connection is deliberately
    reused from more than one thread over its lifetime (core.database.
    to_db_thread's dedicated worker thread for most UI reads, plus direct
    calls from each engine's own async loop on the main thread) -- see
    SqliteAdapter's own docstring for the locking that makes that safe.
    """
    con = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    adapter = SqliteAdapter(con)
    _adapters[namespace] = adapter
    return adapter


def get_db(namespace: str = _DEFAULT_NAMESPACE) -> DbAdapter:
    adapter = _adapters.get(namespace)
    if adapter is None:
        raise RuntimeError("init_db() (or set_db()) must be called before get_db()")
    return adapter


def set_db(adapter: DbAdapter, namespace: str = _DEFAULT_NAMESPACE) -> None:
    """Swap in a specific adapter -- e.g. a fake/in-memory one for tests
    without going through init_db()'s real file-path connection."""
    _adapters[namespace] = adapter


def close_db(namespace: str = _DEFAULT_NAMESPACE) -> None:
    adapter = _adapters.pop(namespace, None)
    if adapter is not None and hasattr(adapter, "close"):
        adapter.close()

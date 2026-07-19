"""Typed interface for database access. Repos depend on this, never on
sqlite3 directly -- see docs/todo/refactor/backend-foundation/010-*.md.
"""
from __future__ import annotations

from contextlib import AbstractContextManager
from typing import Any, Protocol, runtime_checkable


class RunResult:
    __slots__ = ("lastrowid", "rowcount")

    def __init__(self, lastrowid: int | None, rowcount: int):
        self.lastrowid = lastrowid
        self.rowcount = rowcount


@runtime_checkable
class DbAdapter(Protocol):
    def get(self, sql: str, *params: Any) -> Any | None:
        """Single row, or None if no match."""
        ...

    def all(self, sql: str, *params: Any) -> list[Any]:
        """Every matching row."""
        ...

    def run(self, sql: str, *params: Any) -> RunResult:
        """INSERT/UPDATE/DELETE. Commits immediately unless called inside a
        transaction() block, in which case the enclosing block controls the
        commit/rollback."""
        ...

    def exec(self, sql: str) -> None:
        """DDL or batch SQL with no parameters."""
        ...

    def transaction(self) -> AbstractContextManager["DbAdapter"]:
        """Groups every run() call made inside the block into one atomic
        commit/rollback. Required for any multi-statement write where the
        statements must succeed or fail together."""
        ...

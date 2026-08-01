"""Shared data-access layer: repo/adapter pattern, no raw sqlite3 outside this package."""

from backend.src.db.database import db as transaction  # noqa: F401

# database.db() at depth 1 commits (or rolls back) everything issued inside
# the block, so the outermost db() block IS the transaction boundary. The
# alias exists so multi-write repo functions can say what they rely on --
# and so the transaction gate (tools/refactor_audit/structure_gates.py) can
# verify that they say it. Single-statement reads and writes keep using db().

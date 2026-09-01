"""Column names that go into SQL verbatim must be identifiers, not text.

Nine repos build an UPDATE's SET clause from the keys of a dict a caller hands
them. The values are parameterised and safe; the column names are not — they
are interpolated into the statement.

Nothing exploits that today. The only path that takes a dict from outside the
app is a settings proposal over the sync channel, and that is filtered through
an allowlist first. But the allowlist lives in a different module from the SQL,
so the query's safety rests on every caller remembering — including ones not
written yet.

This is the check in one place. It is about SHAPE rather than a list of known
columns: a valid identifier that is not a real column still fails, with
SQLite's own "no such column", which is a clear error rather than a corrupted
statement. `dpm/repo.py` shows the stronger version where the columns CAN be
enumerated (`column not in MILESTONE_COLUMNS`), and that is better still where
it is possible.
"""
from __future__ import annotations

import re
from typing import Iterable

__all__ = ["is_identifier", "set_clause_for"]

# Deliberately narrow: letters, digits and underscore, not starting with a
# digit. No dots, no spaces, no quotes. Every real column in this schema fits.
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def is_identifier(name: object) -> bool:
    return isinstance(name, str) and bool(_IDENTIFIER.match(name))


def set_clause_for(columns: Iterable) -> str:
    """`"a=?, b=?"` for an iterable of column names (a dict works too).

    Raises ValueError on anything that is not a plain identifier, naming the
    offender. It refuses the whole clause rather than dropping the bad key:
    silently filtering would apply a partial update the caller believes was
    complete.
    """
    names = list(columns)
    if not names:
        raise ValueError("no columns to update — an empty SET clause is a "
                         "syntax error at execute time, far from here")
    bad = [n for n in names if not is_identifier(n)]
    if bad:
        raise ValueError(
            f"not a valid SQL identifier: {bad!r} — column names are "
            f"interpolated into the statement, so they cannot be arbitrary text"
        )
    return ", ".join(f"{n}=?" for n in names)

"""`get_all_signals` must be deterministic when created_at ties.

All three engine repos ordered by `created_at DESC` alone. `created_at`
defaults to `time.time()`, which on Windows resolves to roughly a millisecond
-- three back-to-back calls return an *identical* value 19997 times out of
20000. So consecutive inserts routinely share a timestamp, the ORDER BY is a
total tie, and SQLite is free to return the rows in any order it likes.

That surfaced as `test_get_all_signals_respects_limit_newest_first` failing
intermittently in `tests/test_signal/`, `tests/reversal_engine/` and
`tests/breakout_signal/` across four full-suite runs -- three files, one
shared defect. Each passes when its module runs alone, because a cold database
slows the inserts past a tick boundary; a warm cache puts all three in one tick.

It is not only a test artefact. The signal tables render in this order, so a
user watching three signals fire in the same millisecond can see them listed
newest-last.

These tests pin the timestamp explicitly rather than relying on the machine
being slow or fast, so the tie is guaranteed on every platform.
"""
from __future__ import annotations

import tempfile

import pytest

from backend.src.services.breakout_signal import breakout_signal_repo
from backend.src.services.reversal_engine import reversal_engine_repo
from backend.src.services.test_signal import test_signal_repo

PINNED = 1785835911.5   # one instant; every row shares it


def _from_module(dotted):
    """Reuse a test module's own row factory where it has one."""
    import importlib
    return lambda ref: importlib.import_module(dotted)._sig(signal_ref=ref)


# (repo module, create fn name, row builder). reversal_engine's own
# characterization tests build the dict inline rather than via a factory,
# so it gets the same minimal row here.
ENGINES = [
    pytest.param(breakout_signal_repo, "create_signal",
                 _from_module("tests.breakout_signal.test_database_characterization"),
                 id="breakout"),
    pytest.param(reversal_engine_repo, "create_signal",
                 lambda ref: {"direction": "BUY", "signal_ref": ref},
                 id="reversal_engine"),
    pytest.param(test_signal_repo, "insert_signal",
                 _from_module("tests.test_signal.test_database_characterization"),
                 id="test_signal"),
]


def _make(repo, create_name, build_row, n=3):
    """Insert n rows that all carry the SAME created_at, newest last."""
    repo.init(tempfile.mktemp(suffix=".db"))
    ids = []
    for i in range(n):
        row = build_row(f"s{i}")
        row["created_at"] = PINNED          # force the tie
        result = getattr(repo, create_name)(row)
        # insert_signal returns a tuple, create_signal returns the id
        ids.append(result[0] if isinstance(result, tuple) else result)
    return ids


@pytest.mark.parametrize("repo,create_name,build_row", ENGINES)
def test_tied_timestamps_still_order_newest_first(repo, create_name, build_row):
    ids = _make(repo, create_name, build_row)
    rows = repo.get_all_signals(limit=2)

    assert len(rows) == 2
    assert [r["id"] for r in rows] == [ids[-1], ids[-2]], (
        "rows sharing a created_at came back in an arbitrary order -- "
        "ORDER BY created_at DESC needs a tiebreaker"
    )


@pytest.mark.parametrize("repo,create_name,build_row", ENGINES)
def test_the_limit_keeps_the_newest_rows_not_an_arbitrary_two(repo, create_name, build_row):
    """A LIMIT over a total tie must not drop the newest row."""
    ids = _make(repo, create_name, build_row, n=5)
    rows = repo.get_all_signals(limit=2)

    assert ids[-1] in [r["id"] for r in rows], (
        "the newest row was discarded by LIMIT because the tie ordering "
        "put it last"
    )

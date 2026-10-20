"""The Calendar's day journal, stored (docs/todo/023, #8).

A note, tags and a 1-5 mood per trading day, kept in reversal_engine.db with
the account as a column: a note on a live day must never show on the demo
calendar (provisional, docs/simon-handover/055).
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.analytics import day_journal as dj
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from tests.conftest import remove_db_file


@pytest.fixture
def store(monkeypatch):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_db.init(path)
    monkeypatch.setattr(dj, "_account_env", lambda: "demo")
    yield
    re_db.close_db()
    remove_db_file(path)


def test_a_saved_day_comes_back(store):
    dj.save("2026-09-15", note="Overtraded", tags=["tilt", "news"], mood=2)

    [got] = dj.between("2026-09-01", "2026-09-30")
    assert (got["day"], got["note"], got["tags"], got["mood"]) == \
        ("2026-09-15", "Overtraded", ["tilt", "news"], 2)


def test_saving_again_replaces_the_day(store):
    dj.save("2026-09-15", note="first", tags=[], mood=None)
    dj.save("2026-09-15", note="second", tags=["x"], mood=5)

    [got] = dj.between("2026-09-01", "2026-09-30")
    assert (got["note"], got["tags"], got["mood"]) == ("second", ["x"], 5)


def test_saving_a_blank_day_removes_it(store):
    dj.save("2026-09-15", note="something", tags=[], mood=3)
    dj.save("2026-09-15", note="  ", tags=[], mood=None)

    assert dj.between("2026-09-01", "2026-09-30") == []


def test_the_range_is_inclusive_and_nothing_outside_it(store):
    for day in ("2026-08-31", "2026-09-01", "2026-09-30", "2026-10-01"):
        dj.save(day, note=day, tags=[], mood=None)

    assert [e["day"] for e in dj.between("2026-09-01", "2026-09-30")] == \
        ["2026-09-01", "2026-09-30"]


def test_one_account_never_sees_the_others_notes(store, monkeypatch):
    dj.save("2026-09-15", note="demo note", tags=[], mood=None)
    monkeypatch.setattr(dj, "_account_env", lambda: "live")

    assert dj.between("2026-09-01", "2026-09-30") == []
    dj.save("2026-09-15", note="live note", tags=[], mood=None)
    assert [e["note"] for e in dj.between("2026-09-01", "2026-09-30")] == ["live note"]


@pytest.mark.parametrize("kwargs", [
    {"day": "2026-9-15"}, {"day": "2026-02-30"}, {"mood": 0}, {"mood": 6},
    {"note": "x" * 4001}, {"tags": [f"t{i}" for i in range(11)]}, {"tags": ["x" * 33]},
])
def test_bad_input_is_refused(store, kwargs):
    args = {"day": "2026-09-15", "note": "n", "tags": [], "mood": None, **kwargs}
    with pytest.raises(dj.BadJournalEntry):
        dj.save(args.pop("day"), **args)


def test_tags_are_trimmed_and_deduplicated(store):
    dj.save("2026-09-15", note="", tags=[" a ", "b", "a", ""], mood=None)

    assert dj.between("2026-09-15", "2026-09-15")[0]["tags"] == ["a", "b"]

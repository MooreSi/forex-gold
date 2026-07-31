"""Characterizes get_tg_signals on SimulationEngine (core/engine.py) before
task 020 extracts it -- see docs/todo/refactor/core-tg-signals-migration/010-*.md.

Calls self._tg_reader.get_group_name(...) internally, so it needs a real
SimulationEngine.__new__(SimulationEngine) instance with _tg_reader set
manually to a fake test-double (or None) -- __init__ never runs, so no
live Telegram client is ever constructed.
"""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


class _FakeTgReader:
    def __init__(self, names: dict):
        self._names = names

    def get_group_name(self, group_id: str):
        return self._names.get(group_id)


@pytest.fixture
def engine(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._tg_reader = None
    return e


def _insert_tg_signal(msg_id, group_id="grp-1", group_name=None, status="new",
                      parsed_at=None, raw_text="signal text"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id, group_id, group_name, "
            "raw_text, parsed_at, status) VALUES (?,?,?,?,?,?)",
            (msg_id, group_id, group_name, raw_text, parsed_at or time.time(), status),
        )


def test_excludes_instant_historical_rows(fresh_db, engine):
    _insert_tg_signal("m-1", status="new")
    _insert_tg_signal("m-2", status="instant_historical")

    result = SimulationEngine.get_tg_signals(engine)
    ids = [r["tg_message_id"] for r in result]
    assert ids == ["m-1"]


def test_orders_by_parsed_at_descending(fresh_db, engine):
    _insert_tg_signal("m-1", parsed_at=100.0)
    _insert_tg_signal("m-2", parsed_at=200.0)

    result = SimulationEngine.get_tg_signals(engine)
    ids = [r["tg_message_id"] for r in result]
    assert ids == ["m-2", "m-1"]


def test_respects_limit(fresh_db, engine):
    for i in range(5):
        _insert_tg_signal(f"m-{i}", parsed_at=float(i))

    result = SimulationEngine.get_tg_signals(engine, limit=2)
    assert len(result) == 2


def test_backfills_group_name_when_missing(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._tg_reader = _FakeTgReader({"grp-1": "Gold Diggers VIP"})
    _insert_tg_signal("m-1", group_id="grp-1", group_name=None)

    result = SimulationEngine.get_tg_signals(e)
    assert result[0]["group_name"] == "Gold Diggers VIP"


def test_does_not_overwrite_existing_group_name(fresh_db):
    e = SimulationEngine.__new__(SimulationEngine)
    e._tg_reader = _FakeTgReader({"grp-1": "Gold Diggers VIP"})
    _insert_tg_signal("m-1", group_id="grp-1", group_name="Manual Override")

    result = SimulationEngine.get_tg_signals(e)
    assert result[0]["group_name"] == "Manual Override"


def test_works_with_no_tg_reader(fresh_db, engine):
    _insert_tg_signal("m-1", group_id="grp-1", group_name=None)
    result = SimulationEngine.get_tg_signals(engine)
    assert result[0]["group_name"] is None

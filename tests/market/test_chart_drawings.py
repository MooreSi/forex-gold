"""Drawings on the Broker chart, stored (docs/todo/011).

The owner's complaint (2026-09-28): drawings made on the embedded TradingView
chart are gone after a page load. These are ours, so they must come back.
Anchors are (time, price), never pixels: a pixel position is only true for one
zoom level.
"""
from __future__ import annotations

import os
import tempfile

import pytest

from backend.src.services.market import chart_drawings as cd
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from tests.conftest import remove_db_file

TREND = {"kind": "trend", "points": [{"time": 1_790_600_000, "price": 4150.5},
                                     {"time": 1_790_603_600, "price": 4160.25}]}


@pytest.fixture
def store():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_db.init(path)
    yield
    re_db.close_db()
    remove_db_file(path)


def test_a_drawing_comes_back_after_it_is_saved(store):
    saved = cd.create("XAUUSD", TREND["kind"], TREND["points"])

    [got] = cd.list_for("XAUUSD")
    assert got["id"] == saved["id"]
    assert got["kind"] == "trend"
    assert got["points"] == TREND["points"]
    assert got["symbol"] == "XAUUSD"


def test_drawings_belong_to_their_symbol(store):
    cd.create("XAUUSD", TREND["kind"], TREND["points"])

    assert cd.list_for("EURUSD") == []


def test_moving_a_drawing_replaces_its_points(store):
    saved = cd.create("XAUUSD", TREND["kind"], TREND["points"])
    moved = [{"time": 1_790_700_000, "price": 4100.0},
             {"time": 1_790_703_600, "price": 4110.0}]

    assert cd.update_points(saved["id"], moved)["points"] == moved
    assert cd.list_for("XAUUSD")[0]["points"] == moved


def test_deleting_one_leaves_the_others(store):
    a = cd.create("XAUUSD", TREND["kind"], TREND["points"])
    b = cd.create("XAUUSD", "hline", [{"time": 1_790_600_000, "price": 4200.0}])

    assert cd.delete(a["id"]) is True
    assert [d["id"] for d in cd.list_for("XAUUSD")] == [b["id"]]


def test_an_unknown_id_is_reported_not_invented(store):
    assert cd.update_points(999, TREND["points"]) is None
    assert cd.delete(999) is False


def test_listed_oldest_first_so_newer_shapes_draw_on_top(store):
    first = cd.create("XAUUSD", "hline", [{"time": 1, "price": 1.0}])
    second = cd.create("XAUUSD", "hline", [{"time": 2, "price": 2.0}])

    assert [d["id"] for d in cd.list_for("XAUUSD")] == [first["id"], second["id"]]


def test_a_reader_on_a_fresh_install_gets_an_empty_list(store):
    # No drawing has ever been saved, so no table has been created yet.
    assert cd.list_for("XAUUSD") == []


def test_a_symbol_with_too_many_drawings_is_refused(store, monkeypatch):
    # A cap, not a style limit: every drawing is re-read and redrawn on each pan.
    monkeypatch.setattr(cd, "MAX_PER_SYMBOL", 2)
    cd.create("XAUUSD", "hline", [{"time": 1, "price": 1.0}])
    cd.create("XAUUSD", "hline", [{"time": 2, "price": 2.0}])

    with pytest.raises(cd.TooManyDrawings):
        cd.create("XAUUSD", "hline", [{"time": 3, "price": 3.0}])
    # Another symbol still has room.
    cd.create("EURUSD", "hline", [{"time": 3, "price": 3.0}])

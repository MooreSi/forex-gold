"""EA template import/export (Trading > Strategy > EA Templates).

A template file is how a working, hand-tuned template moves between
installs, so the round trip must preserve every field exactly, and an
import must never silently overwrite a local template of the same name
unless the panel's Overwrite box was ticked.
"""
import json
import os
import tempfile

import pytest

from backend.src.services.broker import ea_templates as et
from backend.src.db import database as db


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _drop_all_templates():
    for t in et.list_ea_templates():
        et.delete_ea_template(t["name"])


def test_round_trip_preserves_every_field(fresh_db):
    et.save_ea_template("Gold Grid", {
        "mode": "grid", "pending_mode": "step", "grid_step_pts": 12.5,
        "anchors": 2, "pendings": 3, "lot_anchor": 0.05, "lot_pending": 0.02,
        "trail_mode": "fractal", "trail_distance": 33.0,
        "sig_guard": True, "sig_guard_pips": 20.0, "partials": False,
        "tp1_pips": 30.0, "tp1_pct": 50.0, "tp_pen1_pips": 40.0,
        "tp_pen1_pct": 25.0, "close_full_on_last": False,
    })
    before = et.get_ea_template("Gold Grid")
    blob = et.export_templates()

    _drop_all_templates()
    assert et.get_ea_template("Gold Grid") is None

    result = et.import_templates(blob)
    assert result == {"added": ["Gold Grid"], "replaced": [], "skipped": []}

    after = et.get_ea_template("Gold Grid")
    for field in et.DEFAULTS:
        assert after[field] == before[field], field


def test_existing_name_is_kept_unless_overwrite(fresh_db):
    _drop_all_templates()
    et.save_ea_template("Shared", {"grid_step_pts": 99.0})
    blob = et.export_templates()
    et.save_ea_template("Shared", {"grid_step_pts": 7.0})  # local edit

    result = et.import_templates(blob)
    assert result["skipped"] == ["Shared"]
    assert result["added"] == []
    assert et.get_ea_template("Shared")["grid_step_pts"] == 7.0

    result = et.import_templates(blob, overwrite=True)
    assert result["replaced"] == ["Shared"]
    assert et.get_ea_template("Shared")["grid_step_pts"] == 99.0


def test_export_subset_only(fresh_db):
    _drop_all_templates()
    et.save_ea_template("A", {})
    et.save_ea_template("B", {})
    names = [t["name"] for t in json.loads(et.export_templates(["B"]))["templates"]]
    assert names == ["B"]


def test_missing_fields_fall_back_to_defaults(fresh_db):
    """A file from an older build lacks newer columns; it must still import."""
    _drop_all_templates()
    blob = json.dumps({
        "format": et.EXPORT_FORMAT, "version": 1,
        "templates": [{"name": "Old", "mode": "grid", "unknown_key": 123}],
    })
    et.import_templates(blob)
    t = et.get_ea_template("Old")
    assert t["mode"] == "grid"
    assert t["trail_distance"] == et.DEFAULTS["trail_distance"]


def test_bad_file_imports_nothing(fresh_db):
    _drop_all_templates()
    with pytest.raises(ValueError):
        et.import_templates(b"not json at all")
    with pytest.raises(ValueError):
        et.import_templates(json.dumps({"format": "something.else",
                                        "templates": []}))
    # A file whose second template is invalid must not leave the first behind.
    blob = json.dumps({"format": et.EXPORT_FORMAT, "templates": [
        {"name": "Good", "mode": "grid"},
        {"name": "Bad", "mode": "sideways"},
    ]})
    with pytest.raises(ValueError):
        et.import_templates(blob)
    assert et.get_ea_template("Good") is None


def test_unnamed_template_is_rejected(fresh_db):
    with pytest.raises(ValueError):
        et.import_templates(json.dumps([{"mode": "grid"}]))

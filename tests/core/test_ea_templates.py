"""core_ea_templates.py -- EA Template CRUD, and the resolve_open_trade_params
routing/Sig Guard behaviour a saved template drives in core_signal_resolution.py."""
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_templates as et


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


def test_save_returns_defaults_merged_with_overrides(fresh_db):
    t = et.save_ea_template("Grid Stealth", {"mode": "grid", "tpsl_mode": "stealth"})
    assert t["mode"] == "grid"
    assert t["tpsl_mode"] == "stealth"
    # untouched fields keep their defaults
    assert t["anchor"] == "unified"
    assert t["trail_mode"] == "off"
    assert t["be_trigger"] == 1


def test_save_coerces_types(fresh_db):
    t = et.save_ea_template("T1", {
        "sig_guard": 1, "cancel_pending": 0, "grid_legs": "5", "be_trigger": "3",
        "harvest_threshold": "75.5",
    })
    assert t["sig_guard"] is True
    assert t["cancel_pending"] is False
    assert t["grid_legs"] == 5
    assert t["be_trigger"] == 3
    assert t["harvest_threshold"] == 75.5


def test_be_trigger_clamped_to_1_8(fresh_db):
    t = et.save_ea_template("T1", {"be_trigger": 0})
    assert t["be_trigger"] == 1
    t = et.save_ea_template("T2", {"be_trigger": 99})
    assert t["be_trigger"] == 8


def test_invalid_enum_raises(fresh_db):
    with pytest.raises(ValueError):
        et.save_ea_template("Bad", {"mode": "triangle"})


def test_empty_name_raises(fresh_db):
    with pytest.raises(ValueError):
        et.save_ea_template("  ", {})


def test_save_is_upsert_and_preserves_created_at(fresh_db):
    t1 = et.save_ea_template("T1", {"mode": "grid"})
    time.sleep(0.01)
    t2 = et.save_ea_template("T1", {"mode": "single"})
    assert t2["mode"] == "single"
    assert t2["created_at"] == t1["created_at"]
    assert t2["updated_at"] >= t1["updated_at"]
    assert len(et.list_ea_templates()) == 1


def test_list_sorted_case_insensitively(fresh_db):
    et.save_ea_template("zebra", {})
    et.save_ea_template("Apple", {})
    names = [t["name"] for t in et.list_ea_templates()]
    assert names == ["Apple", "zebra"]


def test_get_missing_returns_none(fresh_db):
    assert et.get_ea_template("nope") is None


def test_delete(fresh_db):
    et.save_ea_template("T1", {})
    et.delete_ea_template("T1")
    assert et.get_ea_template("T1") is None


# ── Anchor TP (2026-07-24) ───────────────────────────────────────────────

def test_anchor_tp_defaults_to_all_zero(fresh_db):
    t = et.save_ea_template("Plain", {})
    for n in range(1, 9):
        assert t[f"tp{n}_pips"] == 0.0
        assert t[f"tp{n}_pct"] == 0.0


def test_anchor_tp_saves_and_coerces_types(fresh_db):
    t = et.save_ea_template("Anchored", {
        "tp1_pips": "20", "tp1_pct": "25",
        "tp4_pips": 100.0, "tp4_pct": 100.0,
    })
    assert t["tp1_pips"] == 20.0
    assert t["tp1_pct"] == 25.0
    assert t["tp4_pips"] == 100.0
    assert t["tp4_pct"] == 100.0
    assert t["tp2_pips"] == 0.0  # untouched levels keep the default


def test_override_helpers_roundtrip():
    assert et.is_template_override("template:My Grid")
    assert not et.is_template_override("reversal_runner")
    assert not et.is_template_override(None)
    assert not et.is_template_override("")
    assert et.template_name_from_override("template:My Grid") == "My Grid"
    assert et.override_for_template("My Grid") == "template:My Grid"

"""core_ea_templates.py -- EA Template CRUD, and the resolve_open_trade_params
routing/Sig Guard behaviour a saved template drives in core_signal_resolution.py."""
import os
import tempfile
import time

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_ea_templates as et


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


def test_be_trigger_clamped_to_ladder_depth(fresh_db):
    """Upper bound follows MAX_TP_LEVELS (8)."""
    t = et.save_ea_template("T1", {"be_trigger": 0})
    assert t["be_trigger"] == 1
    t = et.save_ea_template("T2", {"be_trigger": 99})
    assert t["be_trigger"] == et.MAX_TP_LEVELS == 8


def test_cancel_pending_level_allows_zero_meaning_never(fresh_db):
    """Unlike be_trigger, 0 is meaningful here -- it means "never cancel
    siblings" -- so the floor is 0, not 1."""
    t = et.save_ea_template("T3", {"cancel_pending_level": 0})
    assert t["cancel_pending_level"] == 0
    t = et.save_ea_template("T4", {"cancel_pending_level": 99})
    assert t["cancel_pending_level"] == 8


def test_anchor_and_pending_ladders_are_independent(fresh_db):
    """The copier ships WIDER pending defaults (40/70/110/150/250) than
    anchor (30/50/80/100/130) because a leg filled deeper in the zone has
    more room to the same target. The two tables must not alias."""
    t = et.save_ea_template("Two", {
        "tp1_pips": 30.0, "tp2_pips": 50.0,
        "tp_pen1_pips": 40.0, "tp_pen2_pips": 70.0,
    })
    assert (t["tp1_pips"], t["tp2_pips"]) == (30.0, 50.0)
    assert (t["tp_pen1_pips"], t["tp_pen2_pips"]) == (40.0, 70.0)


def test_leg_counts_and_lots_are_sanitised(fresh_db):
    """A hand-edited template must not be able to send a negative count or
    lot to the broker."""
    t = et.save_ea_template("Legs", {
        "anchors": 99, "pendings": -5, "lot_anchor": -1.0, "lot_pending": 0.02,
    })
    assert t["anchors"] == 20
    assert t["pendings"] == 0
    assert t["lot_anchor"] == 0.0
    assert t["lot_pending"] == 0.02


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


def test_group_tp_action_defaults_off_and_saves(fresh_db):
    t = et.save_ea_template("Plain", {})
    assert t["group_tp_action"] is False
    t2 = et.save_ea_template("Basket", {"mode": "grid", "group_tp_action": 1})
    assert t2["group_tp_action"] is True


def test_override_helpers_roundtrip():
    assert et.is_template_override("template:My Grid")
    assert not et.is_template_override("reversal_runner")
    assert not et.is_template_override(None)
    assert not et.is_template_override("")
    assert et.template_name_from_override("template:My Grid") == "My Grid"
    assert et.override_for_template("My Grid") == "template:My Grid"


# ── None-valued numeric fields (2026-07-31) ──────────────────────────────────
# NiceGUI's ui.number reports its value as None while its box is empty --
# true for every keystroke that clears a field before typing the next
# number, not just an abandoned edit. save_ea_template previously ran
# float()/int() on every numeric field unguarded, so saving while any pips/
# pct/count box was mid-clear raised "float() argument must be a string or a
# real number, not 'NoneType'" and aborted the whole save. Root-caused live
# 2026-07-31 editing Anchor TP pips.

def test_none_pips_value_falls_back_to_default_instead_of_crashing(fresh_db):
    t = et.save_ea_template("Cleared Field", {"tp1_pips": None, "tp2_pips": 30.0})
    assert t["tp1_pips"] == 0.0
    assert t["tp2_pips"] == 30.0


def test_none_int_field_falls_back_to_default_instead_of_crashing(fresh_db):
    t = et.save_ea_template("Cleared Int", {"anchors": None})
    assert t["anchors"] == et.DEFAULTS["anchors"]


def test_none_pct_and_float_fields_all_fall_back(fresh_db):
    t = et.save_ea_template("Cleared Many", {
        "tp1_pct": None, "sl_pips": None, "guard_pips": None,
    })
    assert t["tp1_pct"] == et.DEFAULTS["tp1_pct"]
    assert t["sl_pips"] == et.DEFAULTS["sl_pips"]
    assert t["guard_pips"] == et.DEFAULTS["guard_pips"]

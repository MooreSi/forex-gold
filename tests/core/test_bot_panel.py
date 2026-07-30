"""core_bot_panel.py -- the button-driven Telegram control panel.

Covers the properties that are invisible until the panel is live in Telegram
and expensive to discover there: callback data that exceeds Telegram's 64-byte
cap (the client silently drops the whole keyboard), channel addressing that
survives the channel set changing, and template edits that must not reset the
~90 fields the user did not touch.
"""
import asyncio
import os
import tempfile

import pytest

from forex_trader.core import core_bot_panel as panel
from forex_trader.core import core_ea_templates as et
from forex_trader.core import database as db


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


@pytest.fixture
def template_channel(fresh_db):
    """A channel bound to a grid template, as Reversal Engine is live."""
    et.save_ea_template("Panel Grid", {
        "mode": "grid", "anchors": 1, "pendings": 3,
        "lot_anchor": 0.03, "lot_pending": 0.03, "sl_pips": 50.0,
        "grid_step_pts": 10.0, "trail_mode": "tp", "be_trigger": 2,
        "tp1_pips": 20.0, "tp2_pips": 40.0, "tp3_pips": 60.0,
        "tp1_pct": 10.0, "tp2_pct": 20.0, "tp3_pct": 40.0,
    })
    db.set_channel_strategy_override(
        "Reversal Engine", et.override_for_template("Panel Grid"))
    return next(c for c in panel.channel_list() if c["name"] == "Reversal Engine")


def _run(coro):
    return asyncio.run(coro)


def _all_buttons(screen):
    return [b for row in (screen.keyboard or []) for b in row]


# ── Callback data limits ──────────────────────────────────────────────────────

def test_every_reachable_button_fits_telegrams_64_byte_callback_cap(template_channel):
    """Telegram rejects an entire inline keyboard if any callback_data exceeds
    64 bytes, so one over-long field name would blank a whole screen rather
    than break one button. Walk every navigational screen and check all of it."""
    nav = {"root", "chlist", "cs", "ct", "tpm", "tpl", "strat",
           "f", "fc", "tlist", "sys", "cur", "noop", "x"}
    seen, queue = set(), ["p|root"]
    checked = 0
    while queue:
        data = queue.pop(0)
        if data in seen:
            continue
        seen.add(data)
        parts = data.split("|")
        if len(parts) < 2 or parts[1] not in nav:
            continue          # never fire a mutating or order-placing callback here
        screen = _run(panel.handle_callback(data, None))
        for btn in _all_buttons(screen):
            cb = btn["callback_data"]
            assert len(cb.encode()) <= 64, f"callback data too long: {cb}"
            checked += 1
            queue.append(cb)
    assert checked > 100, "walk did not reach the panel's screens"


def test_cb_rejects_over_long_data_at_build_time():
    with pytest.raises(ValueError):
        panel._cb("f", "a" * 60, "b" * 60)


# ── Channel addressing ────────────────────────────────────────────────────────

def test_channel_slug_is_derived_not_positional(template_channel):
    """Buttons address channels by a hash of the name, so a channel appearing
    or disappearing between render and tap cannot re-point a Close All at a
    different channel."""
    before = {c["name"]: c["slug"] for c in panel.channel_list()}
    db.set_channel_strategy_override("Gold Diggers VIP", "fixed_rr")
    after = {c["name"]: c["slug"] for c in panel.channel_list()}
    for name, slug in before.items():
        assert after.get(name) == slug


def test_unknown_slug_is_reported_not_applied(template_channel):
    screen = _run(panel.handle_callback("p|fa|deadbeef|lot_anchor|u", None))
    assert screen.mode == "noop"
    assert "no longer exists" in screen.toast


# ── Template edits ────────────────────────────────────────────────────────────

def test_stepper_writes_only_the_touched_field(template_channel):
    """save_ea_template rewrites every column from DEFAULTS, so an edit that
    fails to merge the current row first would silently reset ~90 other
    fields. This is the regression that guards that merge."""
    before = et.get_ea_template("Panel Grid")
    _run(panel.handle_callback(f"p|fa|{template_channel['slug']}|lot_anchor|u", None))
    after = et.get_ea_template("Panel Grid")

    changed = {k for k in before
               if before[k] != after[k] and k not in ("updated_at",)}
    assert changed == {"lot_anchor"}
    assert after["lot_anchor"] == pytest.approx(0.04)


def test_stepper_down_clamps_at_the_field_floor(template_channel):
    slug = template_channel["slug"]
    for _ in range(5):
        _run(panel.handle_callback(f"p|fa|{slug}|anchors|d", None))
    assert et.get_ea_template("Panel Grid")["anchors"] == 0


def test_be_trigger_clamps_to_the_tp_ladder_depth(template_channel):
    slug = template_channel["slug"]
    for _ in range(20):
        _run(panel.handle_callback(f"p|fa|{slug}|be_trigger|u", None))
    assert et.get_ea_template("Panel Grid")["be_trigger"] == et.MAX_TP_LEVELS


def test_choice_field_rejects_a_value_outside_its_choices(template_channel):
    slug = template_channel["slug"]
    screen = _run(panel.handle_callback(f"p|fs|{slug}|trail_mode|bogus", None))
    assert screen.mode == "noop"
    assert et.get_ea_template("Panel Grid")["trail_mode"] == "tp"


def test_bool_field_toggles(template_channel):
    slug = template_channel["slug"]
    assert et.get_ea_template("Panel Grid")["harvest_enabled"] is False
    _run(panel.handle_callback(f"p|fb|{slug}|harvest_enabled", None))
    assert et.get_ea_template("Panel Grid")["harvest_enabled"] is True


# ── Exact-value replies ───────────────────────────────────────────────────────

def test_prompt_token_round_trips_field_and_channel(template_channel):
    slug = template_channel["slug"]
    prompt = panel.prompt_text(slug, "tp3_pips")
    assert panel.parse_prompt(prompt) == ("tp3_pips", slug)


def test_typed_value_is_saved(template_channel):
    prompt = panel.prompt_text(template_channel["slug"], "tp3_pips")
    screen = _run(panel.handle_value_reply(prompt, "65"))
    assert screen.mode == "send"
    assert et.get_ea_template("Panel Grid")["tp3_pips"] == pytest.approx(65.0)


def test_non_numeric_reply_is_refused_without_writing(template_channel):
    prompt = panel.prompt_text(template_channel["slug"], "tp3_pips")
    screen = _run(panel.handle_value_reply(prompt, "abc"))
    assert "not a valid number" in screen.text
    assert et.get_ea_template("Panel Grid")["tp3_pips"] == pytest.approx(60.0)


def test_ordinary_message_is_not_mistaken_for_a_value_reply():
    assert panel.parse_prompt("just a normal message") is None


# ── Screen selection ──────────────────────────────────────────────────────────

def test_template_channel_gets_the_full_grid(template_channel):
    labels = [b["text"] for b in _all_buttons(
        panel.channel_settings_screen(template_channel["slug"]))]
    for expected in ("Anchors", "Layers", "Ladder Step", "TP & Pcts"):
        assert any(expected in l for l in labels), expected


def test_builtin_strategy_channel_gets_the_slim_screen(fresh_db):
    db.set_channel_strategy_override("Bounce Engine", "conservative")
    chan = next(c for c in panel.channel_list() if c["name"] == "Bounce Engine")
    screen = panel.channel_settings_screen(chan["slug"])
    labels = [b["text"] for b in _all_buttons(screen)]
    # No template means no grid fields to show -- and the screen says why
    # rather than showing fields that would not be read.
    assert not any("Anchors" in l or "Ladder Step" in l for l in labels)
    assert any("Strategy" in l for l in labels)
    assert "built-in strategy" in screen.text


def test_binding_a_template_switches_the_channel_to_the_full_grid(fresh_db):
    et.save_ea_template("Panel Grid", {"mode": "grid"})
    db.set_channel_strategy_override("Bounce Engine", "conservative")
    chan = next(c for c in panel.channel_list() if c["name"] == "Bounce Engine")
    _run(panel.handle_callback(f"p|sset|{chan['slug']}|t:Panel Grid", None))
    labels = [b["text"] for b in _all_buttons(
        panel.channel_settings_screen(chan["slug"]))]
    assert any("Anchors" in l for l in labels)


def test_manual_channel_sets_the_global_strategy_not_a_channel_row(fresh_db):
    chan = next(c for c in panel.channel_list() if c["name"] == panel.MANUAL)
    _run(panel.handle_callback(f"p|sset|{chan['slug']}|trail_stop", None))
    assert db.get_risk_settings()["trade_strategy"] == "trail_stop"
    assert db.get_channel_strategy_override(panel.MANUAL) is None


def test_pause_toggle_round_trips(template_channel):
    slug = template_channel["slug"]
    _run(panel.handle_callback(f"p|pause|{slug}", None))
    assert next(c for c in panel.channel_list()
                if c["slug"] == slug)["paused"] is True
    _run(panel.handle_callback(f"p|pause|{slug}", None))
    assert next(c for c in panel.channel_list()
                if c["slug"] == slug)["paused"] is False


# ── Value formatting ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("field,value,expected", [
    ("lot_anchor", 0.03, "0.03"),
    ("anchors", 2, "2"),
    ("be_trigger", 3, "TP3"),
    ("cancel_pending_level", 0, "OFF"),
    ("harvest_enabled", True, "ON"),
    ("harvest_enabled", False, "OFF"),
    ("trail_mode", "step", "STEP"),
    ("tp7_pips", 0.0, "OFF"),
])
def test_value_formatting(field, value, expected):
    assert panel._fmt_value(field, value) == expected


# ── Failure handling ──────────────────────────────────────────────────────────

def test_callback_error_becomes_a_toast_not_an_exception(template_channel):
    """A raising handler must still answer the callback, or Telegram leaves a
    spinner on the button forever."""
    screen = _run(panel.handle_callback("p|tpl", None))   # missing args
    assert screen.mode == "noop"


def test_garbage_callback_is_ignored():
    assert _run(panel.handle_callback("not-ours|x", None)).mode == "noop"
    assert _run(panel.handle_callback("", None)).mode == "noop"

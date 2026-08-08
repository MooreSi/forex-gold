"""core_bot_channel_status.py -- the per-channel blocks the Telegram panel's
Status button prints.

The value of this screen is that it is read from a phone and believed. So the
properties worth guarding are the ones that would make it quietly lie: a
ladder padded out with zeros that were never configured, a channel numbered
by list position when the reader knows its real slot, a guard reported as
OFF when it is on, or a whole block missing because one channel's row is
malformed.

Read-only: nothing here places, closes or modifies an order.
"""
import os
import tempfile
import time
from types import SimpleNamespace

import pytest

from forex_trader.core import core_bot_channel_status as status
from forex_trader.core import core_ea_templates as et
from forex_trader.core import core_trading_schedule as sched
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


def _channel(name: str, at: float = 0.0) -> str:
    with db.db() as conn:
        conn.execute(
            "INSERT INTO channel_parser_config (channel_name, created_at) VALUES (?,?)",
            (name, at or time.time()),
        )
    return name


def _bind(name: str, template: str, fields: dict) -> None:
    et.save_ea_template(template, fields)
    db.set_channel_strategy_override(name, et.override_for_template(template))


def _reader(*slots):
    return SimpleNamespace(get_status=lambda: {
        "auth_state": "connected",
        "slots": [{"slot": n, "group_name": name, "listener_active": active}
                  for n, name, active in slots],
    })


# ── Ladder rendering ──────────────────────────────────────────────────────────

def test_ladder_is_trimmed_to_the_levels_actually_configured(fresh_db):
    """Printing all 8 columns when 4 are set reports four zeros as if they
    were targets sitting at the entry price."""
    name = _channel("C One")
    _bind(name, "Four", {
        "tp1_pips": 20, "tp2_pips": 50, "tp3_pips": 90, "tp4_pips": 200,
        "tp1_pct": 25, "tp2_pct": 25, "tp3_pct": 25, "tp4_pct": 100,
    })
    text = "\n".join(status.channel_status_lines())
    assert "Take Profits Pips (Anchor): 20/50/90/200" in text
    assert "TP Close Pcts (Anchor):     25/25/25/100" in text


def test_a_level_with_a_pct_but_no_pips_still_counts_as_configured(fresh_db):
    """TP1 at 0 pips / 0% is genuinely unused; TP1 at 0 pips with a 14% close
    is a level whose pips are yet to be filled in, and hiding it would make
    the pcts row silently shorter than the ladder the EA runs."""
    name = _channel("C One")
    _bind(name, "Gap", {"tp1_pct": 0, "tp2_pips": 40, "tp2_pct": 14})
    text = "\n".join(status.channel_status_lines())
    assert "Take Profits Pips (Anchor): 0/40" in text
    assert "TP Close Pcts (Anchor):     0/14" in text


def test_unset_ladder_says_so_rather_than_printing_zeros(fresh_db):
    _bind(_channel("C One"), "Bare", {"mode": "grid"})
    text = "\n".join(status.channel_status_lines())
    assert "Take Profits Pips (Anchor): not set" in text


def test_telegram_sourced_ladder_is_marked_not_reported_as_the_levels_used(fresh_db):
    """With tp_from_telegram on, the signal's own TPs replace this column for
    Telegram trades -- reporting the pips plainly would name levels the next
    signal will not use."""
    _bind(_channel("C One"), "FromTg", {
        "tp_from_telegram": True, "tp1_pips": 20, "tp1_pct": 50,
    })
    text = "\n".join(status.channel_status_lines())
    assert "(20) — from signal" in text


# ── Settings and triggers ─────────────────────────────────────────────────────

def test_template_settings_are_reported_from_the_bound_template(fresh_db):
    _bind(_channel("C One"), "Full", {
        "mode": "grid", "tpsl_mode": "on", "harvest_enabled": False,
        "be_mode": "entry", "trail_mode": "tp", "be_trigger": 2,
        "cancel_pending_level": 2, "sig_guard": True, "sig_guard_pips": 25.0,
        "lot_anchor": 0.03, "lot_pending": 0.0, "anchors": 1, "pendings": 0,
        "sl_pips": 60.0, "risk_pct": 0.0,
    })
    text = "\n".join(status.channel_status_lines())
    assert "Lots: Anchor = 0.03 | Pending = 0.00" in text
    assert "Anchor Count = 1 | Pendings = 0 | SL = 60.0 pips" in text
    assert "Harvest = OFF | Grid Mode = ON | TP = ON" in text
    assert "Active = ON | BE = ENTRY | Trail = TP" in text
    assert "BreakEven = TP2 | Delete Pending = TP2" in text
    assert "SIG GUARD = 25 pips" in text
    assert "Risk = OFF" in text


def test_delete_pending_off_is_not_reported_as_tp0(fresh_db):
    _bind(_channel("C One"), "NoCancel", {"cancel_pending_level": 0})
    assert "Delete Pending = OFF" in "\n".join(status.channel_status_lines())


def test_sig_guard_on_with_no_distance_is_not_reported_as_zero_pips(fresh_db):
    """sig_guard_pips 0 is the original all-or-nothing guard, not 'no guard'
    -- '0 pips' would read as the opposite of what it does."""
    _bind(_channel("C One"), "Guard", {"sig_guard": True, "sig_guard_pips": 0.0})
    text = "\n".join(status.channel_status_lines())
    assert "SIG GUARD = ON (any same-direction trade)" in text


def test_a_paused_channel_reports_active_off(fresh_db):
    name = _channel("C One")
    _bind(name, "Full", {"mode": "grid"})
    db.set_channel_paused(name, True)
    assert "Active = OFF" in "\n".join(status.channel_status_lines())


def test_builtin_strategy_channel_gets_a_block_without_invented_grid_fields(fresh_db):
    """A channel on a Python strategy has no anchors, lots or ladder -- and
    the copier-style grid would report DEFAULTS as if they were its settings."""
    name = _channel("C One")
    db.set_channel_strategy_override(name, "conservative")
    text = "\n".join(status.channel_status_lines())
    assert "No EA Template bound" in text
    assert "Take Profits Pips" not in text
    assert "Anchor Count" not in text


# ── Effective strategy (time of day) ─────────────────────────────────────────

def _window_today(**changes) -> None:
    """Enable one all-day window for today carrying `changes`."""
    from datetime import datetime
    sched.set_trading_schedule_enabled(True)
    full = sched.get_trading_schedule()
    day = sched.DAY_NAMES[datetime.now().weekday()]
    full[day][0].update({"enabled": True, "start": "00:00", "end": "23:59", **changes})
    sched.set_trading_schedule(full)


def test_channel_reports_its_own_strategy(fresh_db):
    name = _channel("C One")
    db.set_channel_strategy_override(name, "conservative")
    assert "🎛️ Strategy: Conservative" in "\n".join(status.channel_status_lines())


def test_channel_bound_to_a_template_names_the_template(fresh_db):
    name = _channel("C One")
    _bind(name, "Grid A", {"mode": "grid"})
    assert "Strategy: Template: Grid A" in "\n".join(status.channel_status_lines())


def test_an_active_schedule_window_override_replaces_the_reported_strategy(fresh_db):
    """The whole point of this screen is "what happens to the next signal",
    and inside an override window that is not the channel's own pick."""
    name = _channel("C One")
    _bind(name, "Day Grid", {"mode": "grid", "sl_pips": 60.0})
    et.save_ea_template("Night Grid", {"mode": "grid", "sl_pips": 12.0})
    _window_today(telegram_channels={
        name: {"enabled": True,
               "strategy_override": et.override_for_template("Night Grid")},
    })

    text = "\n".join(status.channel_status_lines())
    assert "Strategy: Template: Night Grid" in text
    assert "from the active Trading Schedule window" in text
    assert "channel setting is Template: Day Grid" in text
    # and the block below it must describe the template that will actually run
    assert "SL = 12.0 pips" in text
    assert "SL = 60.0 pips" not in text


def test_no_override_reports_the_channel_setting_without_a_provenance_note(fresh_db):
    name = _channel("C One")
    _bind(name, "Grid A", {"mode": "grid"})
    _window_today()
    text = "\n".join(status.channel_status_lines())
    assert "Strategy: Template: Grid A" in text
    assert "Trading Schedule window" not in text


def test_a_channel_blocked_by_the_current_window_says_so(fresh_db):
    """Settings that look live but cannot fire are the most misleading thing
    this screen could print."""
    name = _channel("C One")
    _bind(name, "Grid A", {"mode": "grid"})
    _window_today(telegram_channels={name: {"enabled": False, "strategy_override": ""}})
    assert "⛔ Schedule: blocked" in "\n".join(status.channel_status_lines())


def test_no_schedule_gate_line_when_the_schedule_is_off(fresh_db):
    name = _channel("C One")
    _bind(name, "Grid A", {"mode": "grid"})
    assert "Schedule: blocked" not in "\n".join(status.channel_status_lines())


def test_a_channel_with_no_override_falls_back_to_the_global_strategy(fresh_db):
    _channel("C One")
    db.update_risk_settings({"trade_strategy": "trail_stop"})
    text = "\n".join(status.channel_status_lines())
    assert "Strategy: Trailing Stop" in text
    assert "inherited from the global Active Strategy" in text


# ── Internal signal generators ───────────────────────────────────────────────

def test_a_running_generator_gets_a_full_block(fresh_db, monkeypatch):
    _bind("Reversal Engine", "RE Grid", {"mode": "grid", "sl_pips": 25.0})
    monkeypatch.setattr(status, "_ENGINES",
                        (("Reversal Engine", lambda: True),))
    text = "\n".join(status.internal_engine_lines())
    assert "🤖 *REVERSAL ENGINE* (internal generator)" in text
    assert "Engine: running" in text
    assert "Strategy: Template: RE Grid" in text
    assert "SL = 25.0 pips" in text


def test_a_stopped_generator_is_named_but_not_expanded(fresh_db, monkeypatch):
    """A stopped engine's TP ladder describes trades that cannot happen."""
    _bind("Reversal Engine", "RE Grid", {"mode": "grid", "sl_pips": 25.0})
    monkeypatch.setattr(status, "_ENGINES",
                        (("Reversal Engine", lambda: False),))
    text = "\n".join(status.internal_engine_lines())
    assert "Not generating:* Reversal Engine (stopped)" in text
    assert "SL = 25.0 pips" not in text


def test_an_unreachable_generator_is_not_reported_as_stopped(fresh_db, monkeypatch):
    """'stopped' is a claim about the engine; an import failure is a claim
    about us, and the two must not read the same."""
    def _boom():
        raise RuntimeError("module gone")

    monkeypatch.setattr(status, "_ENGINES", (("Bounce Engine", _boom),))
    assert "Bounce Engine (state unknown)" in "\n".join(status.internal_engine_lines())


def test_generators_are_the_canonical_channel_names(fresh_db):
    """They resolve strategies through the same channel_performance rows the
    Channel Strategy tab edits, so a typo here would silently report the
    global default for an engine that has its own template."""
    from forex_trader.core import core_db_channel as chan
    for name, _probe in status._ENGINES:
        assert name in chan._FIXED_ENGINE_CHANNELS


# ── Numbering and feed state ──────────────────────────────────────────────────

def test_channel_number_comes_from_the_readers_slot(fresh_db):
    """C1/C2 must match the slot the reader (and the copier, and the EA's own
    comments) uses, not this list's ordering."""
    first = _channel("Alpha", at=1.0)
    second = _channel("Beta", at=2.0)
    _bind(first, "A", {"mode": "grid"})
    _bind(second, "B", {"mode": "grid"})
    text = "\n".join(status.channel_status_lines(_reader(
        (2, "Alpha", True), (1, "Beta", False))))
    assert "*CHANNEL 2* (C2) (Name: Alpha)" in text
    assert "*CHANNEL 1* (C1) (Name: Beta)" in text


def test_feed_line_reports_listener_state(fresh_db):
    _bind(_channel("Alpha"), "A", {"mode": "grid"})
    assert "Feed: listening" in "\n".join(
        status.channel_status_lines(_reader((1, "Alpha", True))))
    assert "Feed: idle" in "\n".join(
        status.channel_status_lines(_reader((1, "Alpha", False))))


def test_no_reader_omits_the_feed_line_rather_than_claiming_idle(fresh_db):
    """/status can be answered before the reader starts; 'idle' there would
    read as a dead feed rather than an unknown one."""
    _bind(_channel("Alpha"), "A", {"mode": "grid"})
    text = "\n".join(status.channel_status_lines())
    assert "Feed:" not in text
    assert "(C1) (Name: Alpha)" in text


def test_channel_with_no_reader_slot_still_renders(fresh_db):
    """A group removed from the reader must not make its configured channel
    -- which still has settings, and may still be re-added -- disappear."""
    _bind(_channel("Alpha", at=1.0), "A", {"mode": "grid"})
    _bind(_channel("Beta", at=2.0), "B", {"mode": "grid"})
    text = "\n".join(status.channel_status_lines(_reader((1, "Alpha", True))))
    assert "(Name: Alpha)" in text
    assert "(Name: Beta)" in text


def test_no_channels_configured_says_so(fresh_db):
    assert status.channel_status_lines() == ["_No Telegram channels are configured._"]


def test_underscores_in_a_channel_name_are_escaped(fresh_db):
    """An unpaired '_' 400s the whole sendMessage, costing every block's
    formatting, not just this name's."""
    _bind(_channel("GOLD_DIGGERS"), "A", {"mode": "grid"})
    assert "GOLD\\_DIGGERS" in "\n".join(status.channel_status_lines())

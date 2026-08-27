"""How the Telegram Status screen decides what each channel will do.

`core_bot_channel_status.py` renders one block per channel to answer the
question actually asked from a phone -- *what will this channel do with the
next signal it gets* -- and the parts that decide that answer were the parts
without tests.

Two of them are about being honest rather than confident:

- `_engine_running` returns None, not False, when it cannot reach an engine,
  because "stopped" is a claim about the engine and an import failure is a
  claim about us.
- `_schedule_gate_line` prints a line only when the schedule is actively
  blocking, because settings that look live but cannot fire are the most
  misleading thing this screen could show.

Nothing here touches Telegram; the module builds strings.
"""
from __future__ import annotations

import sys
import types

import pytest

from backend.src.db import database as db
from backend.src.services.positions import core_bot_channel_status as st


# ── _num ──────────────────────────────────────────────────────────────────────

def test_a_stored_float_prints_the_way_it_was_typed():
    """20.0 was typed as 20; printing "20.0" makes a settings screen look like
    it changed something."""
    assert st._num(20.0) == "20"
    assert st._num(12.5) == "12.5"
    assert st._num("0.01") == "0.01"


def test_something_that_is_not_a_number_is_shown_as_is():
    assert st._num(None) == "None"
    assert st._num("auto") == "auto"


# ── _strategy_label ───────────────────────────────────────────────────────────

def test_no_strategy_reads_as_inheriting_the_global_one():
    assert st._strategy_label(None) == "inherit global"
    assert st._strategy_label("") == "inherit global"


def test_auto_is_labelled_as_ai_picked():
    assert st._strategy_label("auto") == "Auto (AI)"


def test_an_unknown_strategy_prints_its_own_name(monkeypatch):
    """Better a raw id than a blank where the strategy should be."""
    monkeypatch.setattr(st.ea_templates, "is_template_override", lambda s: False)
    assert st._strategy_label("something_new") == "something_new"


# ── _resolve_strategy ─────────────────────────────────────────────────────────
#
# Which strategy actually applies, and where it came from. Getting the ORIGIN
# wrong is as bad as getting the strategy wrong: the screen exists to say
# whether last night's setting is the one in force.

@pytest.fixture
def resolution(monkeypatch):
    """Control every source _resolve_strategy consults."""
    state = {"base": None, "schedule": None, "auto_pick": None, "global": None}

    monkeypatch.setattr(db, "get_channel_strategy_override",
                        lambda name: state["base"], raising=False)
    monkeypatch.setattr(db, "get_channel_strategy_rec",
                        lambda name: {"strategy": state["auto_pick"]}, raising=False)
    monkeypatch.setattr(db, "get_risk_settings",
                        lambda: {"trade_strategy": state["global"]}, raising=False)

    sched = types.ModuleType("backend.src.services.risk.schedule")
    sched.get_schedule_strategy_override = lambda name: state["schedule"]
    monkeypatch.setitem(sys.modules, "backend.src.services.risk.schedule", sched)
    return state


def test_a_channel_override_wins_over_the_global_setting(resolution):
    resolution["base"] = "scale_out"
    resolution["global"] = "be_runner"

    effective, base, origin = st._resolve_strategy("GD VIP")

    assert (effective, base, origin) == ("scale_out", "scale_out", "channel")


def test_a_schedule_override_wins_over_the_channel_one(resolution):
    """The schedule is the more specific statement: right now, do this."""
    resolution["base"] = "scale_out"
    resolution["schedule"] = "trail_stop"

    effective, base, origin = st._resolve_strategy("GD VIP")

    assert effective == "trail_stop"
    assert base == "scale_out", "the channel's own setting is still reported"
    assert origin == "schedule"


def test_auto_resolves_to_whatever_the_ai_actually_picked(resolution):
    resolution["base"] = "auto"
    resolution["auto_pick"] = "conservative_trial"

    effective, base, origin = st._resolve_strategy("GD VIP")

    assert (effective, origin) == ("conservative_trial", "auto")


def test_auto_with_nothing_picked_yet_falls_back_to_global(resolution):
    """Showing "auto" alone would not answer the question the screen exists for."""
    resolution["base"] = "auto"
    resolution["auto_pick"] = None
    resolution["global"] = "scale_out"

    effective, base, origin = st._resolve_strategy("GD VIP")

    assert (effective, origin) == ("scale_out", "global")


def test_nothing_set_anywhere_falls_back_to_global(resolution):
    resolution["global"] = "be_runner"
    effective, base, origin = st._resolve_strategy("GD VIP")
    assert (effective, base, origin) == ("be_runner", None, "global")


def test_a_broken_lookup_does_not_take_the_status_screen_down(resolution, monkeypatch):
    """One unreadable channel must not blank the whole screen."""
    monkeypatch.setattr(db, "get_channel_strategy_override",
                        lambda name: (_ for _ in ()).throw(RuntimeError("db gone")),
                        raising=False)
    resolution["global"] = "scale_out"

    effective, base, origin = st._resolve_strategy("GD VIP")

    assert (effective, base, origin) == ("scale_out", None, "global")


# ── _engine_running ───────────────────────────────────────────────────────────

def test_an_unreachable_engine_is_unknown_not_stopped():
    """"stopped" is a claim about the engine; an import failure is a claim
    about us. Printing the first when we mean the second is how someone
    concludes an engine died when it is running fine."""
    assert st._engine_running("backend.src.services.nope.not_a_module") is None


def test_an_engine_with_no_instance_is_stopped(monkeypatch):
    mod = types.ModuleType("fake_engine")
    mod.get_instance = lambda: None
    monkeypatch.setitem(sys.modules, "fake_engine", mod)

    assert st._engine_running("fake_engine") is False


def test_a_running_engine_reports_true(monkeypatch):
    mod = types.ModuleType("fake_engine2")
    mod.get_instance = lambda: types.SimpleNamespace(is_running=True)
    monkeypatch.setitem(sys.modules, "fake_engine2", mod)

    assert st._engine_running("fake_engine2") is True


def test_an_instance_that_cannot_answer_is_unknown(monkeypatch):
    class _Hostile:
        @property
        def is_running(self):
            raise RuntimeError("no idea")

    mod = types.ModuleType("fake_engine3")
    mod.get_instance = lambda: _Hostile()
    monkeypatch.setitem(sys.modules, "fake_engine3", mod)

    assert st._engine_running("fake_engine3") is None


# ── _schedule_gate_line ───────────────────────────────────────────────────────

def _sched_module(monkeypatch, *, enabled=True, allowed=True, reason="", raises=False):
    mod = types.ModuleType("backend.src.services.risk.schedule")

    def is_trading_schedule_enabled():
        if raises:
            raise RuntimeError("schedule unavailable")
        return enabled

    mod.is_trading_schedule_enabled = is_trading_schedule_enabled
    mod.check_trading_schedule = lambda source=None: (allowed, reason)
    mod.get_schedule_strategy_override = lambda name: None
    monkeypatch.setitem(sys.modules, "backend.src.services.risk.schedule", mod)


def test_no_line_when_the_schedule_is_switched_off(monkeypatch):
    _sched_module(monkeypatch, enabled=False)
    assert st._schedule_gate_line("GD VIP") is None


def test_no_line_when_the_channel_is_allowed_to_trade(monkeypatch):
    _sched_module(monkeypatch, allowed=True)
    assert st._schedule_gate_line("GD VIP") is None


def test_a_blocked_channel_says_so_and_says_why(monkeypatch):
    """Settings that look live but cannot fire are the most misleading thing
    this screen could print."""
    _sched_module(monkeypatch, allowed=False, reason="outside London session")

    line = st._schedule_gate_line("GD VIP")

    assert line is not None
    assert "blocked" in line and "outside London session" in line


def test_an_unreadable_schedule_prints_nothing_rather_than_guessing(monkeypatch):
    """Claiming "blocked" on a lookup failure would be the same lie in reverse."""
    _sched_module(monkeypatch, raises=True)
    assert st._schedule_gate_line("GD VIP") is None

"""Trend PA as a channel (a strategy can be chosen for it) and as a Trading
Schedule source (its own per-window toggle and override), like Reversal and
Breakout. Owner, 2026-09-29.

Nothing here reaches a broker.
"""
from __future__ import annotations

import json
from datetime import datetime

import pytest

from backend.src.db import database as db
from backend.src.services.channels import repo as channels
from backend.src.services.risk import schedule as sched
from backend.src.services.trend_pa import live_execute as lx

from tests.core.test_one_strategy_per_channel import WINDOW_PICK, _all_day_schedule

KEY = "trend_pa_engine"
NAME = "Trend PA Engine"
_WEDNESDAY = datetime(2026, 7, 22, 10, 30, 0)


# ── a channel on Trading > Strategy ──────────────────────────────────────────

def test_it_is_listed_on_the_strategy_page(fresh_db):
    sources = [c["source"] for c in channels.get_all_channel_strategy_settings()]
    assert NAME in sources


def test_it_is_not_listed_as_a_telegram_channel(fresh_db):
    assert NAME not in channels.get_telegram_channel_names()


def test_a_strategy_chosen_for_it_is_what_the_live_path_reads(fresh_db):
    assert lx._strategy() is None
    channels.set_channel_strategy_override(NAME, "be_runner")
    assert lx._strategy() == "be_runner"


def test_its_pick_travels_to_the_peer(fresh_db):
    channels.set_channel_strategy_override(NAME, "be_runner")
    assert channels.get_all_channel_strategy_overrides()[NAME]["strategy"] == "be_runner"


# ── a source in every Trading Schedule window ────────────────────────────────

def test_it_is_an_engine_source_with_its_own_label():
    assert KEY in sched.ENGINE_SOURCE_KEYS
    assert sched._SOURCE_LABELS[KEY] == NAME
    assert sched.schedule_source_key(NAME) == KEY


def test_a_new_window_allows_it_with_no_override():
    block = sched._default_block()
    assert block[KEY] is True and block[f"{KEY}_override"] == ""


def test_unticking_it_blocks_it_and_nothing_else(fresh_db):
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_all_day_schedule(**{KEY: False}))
    ok, why = sched.check_trading_schedule(now=_WEDNESDAY, source=KEY)
    assert ok is False and NAME in why
    assert sched.check_trading_schedule(now=_WEDNESDAY, source="breakout_engine")[0] is True


def test_it_does_not_follow_the_telegram_default(fresh_db):
    """Before its own toggle it was gated as if it were a Telegram channel."""
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_all_day_schedule(telegram_default_enabled=False))
    assert sched.check_trading_schedule(now=_WEDNESDAY, source=KEY)[0] is True


def test_its_window_override_wins_over_its_channel_pick(fresh_db):
    channels.set_channel_strategy_override(NAME, "be_runner")
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_all_day_schedule(**{f"{KEY}_override": WINDOW_PICK}))
    assert sched.effective_channel_strategy(NAME) == WINDOW_PICK


def test_a_window_saved_before_it_existed_allows_it_and_inherits_no_override(fresh_db):
    """The pre-2026-08-03 shared override was migrated into Reversal's and
    Breakout's own fields. Trend PA did not exist then; handing it that pick
    would choose its strategy for it."""
    old = _all_day_schedule(strategy_override="be_runner")
    for blocks in old.values():
        for b in blocks:
            b.pop(KEY, None)
            b.pop(f"{KEY}_override", None)
    db.set_app_config("trading_schedule", json.dumps(old))
    block = sched.get_trading_schedule()["wednesday"][0]
    assert block[KEY] is True
    assert block[f"{KEY}_override"] == ""
    assert block["breakout_engine_override"] == "be_runner"  # unchanged behaviour


def test_the_live_path_asks_the_schedule_under_its_own_key(monkeypatch):
    seen = {}

    def fake(source="telegram", now=None):
        seen["source"] = source
        return True, ""
    monkeypatch.setattr(sched, "check_trading_schedule", fake)
    lx._schedule()
    assert seen["source"] == KEY


# ── the other readers of a window's engine keys ──────────────────────────────

def test_a_template_rename_repoints_its_window_override():
    from backend.src.services.broker import template_rename
    assert f"{KEY}_override" in template_rename._WINDOW_OVERRIDE_KEYS


def test_an_auto_window_override_makes_it_an_auto_source(fresh_db, monkeypatch):
    from backend.src.services.positions import core_auto_template as auto
    sched.set_trading_schedule(_all_day_schedule(**{f"{KEY}_override": "auto"}))
    assert NAME in auto.auto_enabled_sources()


def test_the_per_channel_loss_cap_does_not_treat_its_key_as_a_channel():
    from backend.src.services.risk import channel_loss_cap
    assert KEY in channel_loss_cap._NOT_A_CHANNEL

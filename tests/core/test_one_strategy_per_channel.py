"""One channel, one strategy -- whichever route the signal takes to the broker.

Owner, 2026-09-21: "Telegram Auto (GOLD DIGGERS INSTITUTIONAL) and GOLD
DIGGERS INSTITUTIONAL use different strategies, they are the same channel".

They were, and it was not a display bug. Both spellings already canonicalise
(test_canonical_channel_name.py), and channel_performance held exactly one row
per channel. What differed was WHICH LAYER each execution route asked.

`resolution.py` resolves "Trading Schedule window override > channel override".
Six other routes to the broker asked `get_channel_strategy_override()` alone
and never looked at the schedule. So on 2026-09-21, with the schedule holding
`template:30 TP1 SL50 and Trail` for every day and the channel holding
`template:GD Instituational - single`, the same channel's signals ran:

    GOLD DIGGERS INSTITUTIONAL                 template:GD Instituational - single
    Telegram Auto (GOLD DIGGERS INSTITUTIONAL) template:30 TP1 SL50 and Trail

-- different geometry, different stop, different lot, depending only on which
code path happened to reach the order first.

Owner's decision (2026-09-21): **the schedule window wins everywhere.** That
is what resolution.py already did, so the fix is to give every other route the
same precedence rather than to remove it from the one that had it.

`effective_channel_strategy()` is that one answer. These tests hold both
halves: that it resolves the way the owner chose, and that no route resolves
the question any other way.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.db import database as db_module
from backend.src.services.risk import schedule as sched


CHANNEL = "GOLD DIGGERS INSTITUTIONAL"
WRAPPED = "Telegram Auto (GOLD DIGGERS INSTITUTIONAL)"

CHANNEL_PICK = "template:GD Instituational - single"
WINDOW_PICK = "template:30 TP1 SL50 and Trail"


def _all_day_schedule(**block_extras) -> dict:
    """The owner's real shape: one enabled 00:00-23:59 block on every day, so
    the active window is the same whichever day the suite runs."""
    schedule = sched._default_schedule()
    for day in sched.DAY_NAMES:
        block = dict(schedule[day][0])
        block.update({"enabled": True, "start": "00:00", "end": "23:59", "target": 0.0})
        block.update(block_extras)
        # Four blocks a day or get_trading_schedule discards the day as
        # malformed and silently hands back defaults -- which would make
        # every "the window wins" test here pass for the wrong reason.
        schedule[day][0] = block
    return schedule


def _window_assigns(strategy: str, channel: str = CHANNEL, enabled: bool = True) -> dict:
    return _all_day_schedule(telegram_channels={
        channel: {"enabled": enabled, "strategy_override": strategy}})


class TestBothSpellingsGetTheSameAnswer:
    """The bug as the owner saw it."""

    def test_the_wrapper_and_the_bare_name_resolve_identically(self, fresh_db):
        db_module.set_channel_strategy_override(CHANNEL, CHANNEL_PICK)
        sched.set_trading_schedule_enabled(True)
        sched.set_trading_schedule(_window_assigns(WINDOW_PICK))

        assert sched.effective_channel_strategy(WRAPPED) == \
            sched.effective_channel_strategy(CHANNEL)

    def test_and_the_answer_is_the_window(self, fresh_db):
        """Owner's call: the schedule window wins."""
        db_module.set_channel_strategy_override(CHANNEL, CHANNEL_PICK)
        sched.set_trading_schedule_enabled(True)
        sched.set_trading_schedule(_window_assigns(WINDOW_PICK))

        assert sched.effective_channel_strategy(WRAPPED) == WINDOW_PICK


class TestThePrecedence:
    def test_the_channel_decides_when_the_schedule_is_off(self, fresh_db):
        db_module.set_channel_strategy_override(CHANNEL, CHANNEL_PICK)
        sched.set_trading_schedule_enabled(False)
        sched.set_trading_schedule(_window_assigns(WINDOW_PICK))

        assert sched.effective_channel_strategy(CHANNEL) == CHANNEL_PICK

    def test_the_channel_decides_when_the_window_assigns_nothing(self, fresh_db):
        db_module.set_channel_strategy_override(CHANNEL, CHANNEL_PICK)
        sched.set_trading_schedule_enabled(True)
        sched.set_trading_schedule(_all_day_schedule())

        assert sched.effective_channel_strategy(CHANNEL) == CHANNEL_PICK

    def test_a_window_that_stands_the_channel_down_does_not_also_pick_for_it(
            self, fresh_db):
        """A disabled source has no window opinion about strategy -- the
        schedule GATE is what stops it trading (check_trading_schedule), not
        this. Unchanged from resolution.py's behaviour, and pinned so the
        consolidation cannot quietly turn a stand-down into a strategy."""
        db_module.set_channel_strategy_override(CHANNEL, CHANNEL_PICK)
        sched.set_trading_schedule_enabled(True)
        sched.set_trading_schedule(_window_assigns(WINDOW_PICK, enabled=False))

        assert sched.effective_channel_strategy(CHANNEL) == CHANNEL_PICK

    def test_nothing_set_anywhere_is_None(self, fresh_db):
        assert sched.effective_channel_strategy(CHANNEL) is None

    def test_auto_survives_the_consolidation(self, fresh_db):
        """"auto" is a real answer, not an absent one -- every caller branches
        on it. It must come back out of the helper unchanged."""
        db_module.set_channel_strategy_override(CHANNEL, None, auto=True)
        sched.set_trading_schedule_enabled(False)

        assert sched.effective_channel_strategy(CHANNEL) == "auto"

    def test_an_empty_source_asks_nothing_and_returns_None(self, fresh_db):
        assert sched.effective_channel_strategy("") is None


class TestTheInternalEnginesKeepTheirOwnWindowToggle:
    """Reversal/Breakout are keyed in a window by their engine key, not by a
    telegram_channels entry -- resolution.py mapped the two literal source
    names across before asking. The helper has to carry that mapping or the
    engines silently lose their per-window override."""

    @pytest.mark.parametrize("source,key", [
        ("Reversal Engine", "reversal_engine"),
        ("Breakout Engine", "breakout_engine"),
    ])
    def test_the_engine_window_override_is_found(self, fresh_db, source, key):
        sched.set_trading_schedule_enabled(True)
        sched.set_trading_schedule(_all_day_schedule(**{f"{key}_override": WINDOW_PICK}))

        assert sched.effective_channel_strategy(source) == WINDOW_PICK


class TestEveryRouteToTheBrokerAsksTheSameQuestion:
    """The actual defect was six routes each asking their own half of it.

    Source inspection rather than behaviour because the point is that no
    route has a second implementation -- a behavioural test per route would
    pass again the day someone adds a seventh.
    """

    ROUTES = [
        ("backend.src.services.signals.resolution", "resolve_open_trade_params"),
        ("backend.src.services.trading.instant_entry", "process_instant_entry"),
        ("backend.src.services.trading.instant_followup", "apply_followup_to_instant_trade"),
        ("backend.src.services.trading.limit_order_signal", "_resolve_management"),
        ("backend.src.services.trading.limit_order_signal", "_template_for_channel"),
        ("backend.src.services.signals.pending_activation", "_resolve_effective_strategy"),
        ("backend.src.services.positions.core_grid_template_dispatch",
         "grid_template_for_source"),
    ]

    @pytest.mark.parametrize("module_path,fn_name", ROUTES)
    def test_it_goes_through_effective_channel_strategy(self, module_path, fn_name):
        import importlib
        mod = importlib.import_module(module_path)
        src = inspect.getsource(getattr(mod, fn_name))
        assert "effective_channel_strategy" in src, (
            f"{module_path}.{fn_name} resolves a channel's strategy without "
            f"the shared helper -- that is how the two spellings diverged"
        )

    @pytest.mark.parametrize("module_path,fn_name", ROUTES)
    def test_it_does_not_ask_the_channel_layer_directly(self, module_path, fn_name):
        """The negative control for the test above: the helper being present
        proves nothing if the old lookup is still there beside it."""
        import importlib
        mod = importlib.import_module(module_path)
        src = inspect.getsource(getattr(mod, fn_name))
        assert "get_channel_strategy_override" not in src, (
            f"{module_path}.{fn_name} still reads the channel override "
            f"directly, bypassing the Trading Schedule window"
        )

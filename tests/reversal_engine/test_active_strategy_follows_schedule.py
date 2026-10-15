"""The Reversal Engine stamps each signal with the strategy it will trade
under, and that stamp must be the same answer the order itself gets.

2026-10-15: the Trading Schedule window held `template:Asian - Single` for the
reversal engine. The order resolved through `resolve_open_trade_params`, which
asks the window first, and ran Asian - Single (Telegram said so). The signal
row was stamped by `_active_strategy`, which asked only the channel override
and the global Active Strategy, and said `scale_out`. That stamp went into the
cross-node ledger at close and the Mac's Trades page showed Scale Out. It also
picked the ladder the engine's virtual management modelled for unexecuted
signals, so their outcomes were simulated on the wrong rules.

Precedence is the owner's 2026-09-21 decision, held in
tests/core/test_one_strategy_per_channel.py: window > channel > global.
"""
from __future__ import annotations

from backend.src.db import database as db_module
from backend.src.services.reversal_engine.reversal_engine_service import ReversalEngine
from backend.src.services.risk import schedule as sched


WINDOW_PICK = "template:Asian - Single"
CHANNEL_PICK = "template:30 TP1 SL50 and Trail"


def _all_day_schedule(**block_extras) -> dict:
    schedule = sched._default_schedule()
    for day in sched.DAY_NAMES:
        block = dict(schedule[day][0])
        block.update({"enabled": True, "start": "00:00", "end": "23:59", "target": 0.0})
        block.update(block_extras)
        schedule[day][0] = block
    return schedule


def _active() -> str:
    # _active_strategy reads only the databases, never the instance.
    return ReversalEngine._active_strategy(object.__new__(ReversalEngine))


def test_the_schedule_window_names_the_strategy(fresh_db):
    db_module.set_channel_strategy_override("Reversal Engine", CHANNEL_PICK)
    sched.set_trading_schedule_enabled(True)
    sched.set_trading_schedule(_all_day_schedule(reversal_engine_override=WINDOW_PICK))

    assert _active() == WINDOW_PICK


def test_the_channel_pick_applies_when_the_schedule_is_off(fresh_db):
    db_module.set_channel_strategy_override("Reversal Engine", CHANNEL_PICK)
    sched.set_trading_schedule_enabled(False)
    sched.set_trading_schedule(_all_day_schedule(reversal_engine_override=WINDOW_PICK))

    assert _active() == CHANNEL_PICK


def test_with_nothing_set_it_is_the_global_active_strategy(fresh_db):
    expected = db_module.get_risk_settings().get("trade_strategy")

    assert _active() == expected

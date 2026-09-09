"""Enable SL Parsing OFF must also stop a follow-up moving an open trade's SL.

Reported live 2026-09-09, with the toggle off:

    SL adjusted — GOLD DIGGERS INSTITUTIONAL
    Trade 062f91ad (ticket 1969210518): 4391.65 → 4395.0
    Source: learned rule

`apply_sl_parsing_override` handles the toggle when a NEW signal's stated stop
is parsed. It has nothing to do with this path: a later message saying "adjust
SL to X" is matched by a learned rule (or the AI classifier) and moves the stop
on a trade that is already open. That route never asked.

**One setting, two meanings of "parse an SL", only one honoured.** The owner's
reading is the plain one: *"sl parsing applies to all telegram channels so this
needs resolving"* — if the app is told not to take stops from Telegram, that
covers a stop arriving in a follow-up.

**The gate goes INSIDE `apply_sl_adjustment`, not at its call sites.** Two
routes reach it — `scan_messages`'s learned-rule fast path and
`ai_signal_fallback`'s classifier — and gating each one is how the next route
gets missed. Same lesson as bugs/024 and reversal-engine/080.

**OFF means do nothing, not substitute.** At entry the toggle substitutes a
template or fallback distance, because a trade must have a stop. Here the trade
already has one; the instruction is simply declined.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.trading import ai_signal_fallback as af


@pytest.fixture
def spy(monkeypatch):
    """Records how far the call gets before anything is claimed or changed."""
    seen = {"claimed": 0, "looked_for_trade": 0}

    async def _to_db_thread(fn, *a, **k):
        seen["claimed"] += 1
        return True

    def _find(_channel):
        seen["looked_for_trade"] += 1
        return None

    monkeypatch.setattr(af.db_module, "to_db_thread", _to_db_thread)
    monkeypatch.setattr(af.trade_repo, "find_channel_open_trade", _find)
    return seen


def _rs(on):
    return {"lk_enable_sl_parsing": on}


def _run(rs, via="learned_rule"):
    return asyncio.run(af.apply_sl_adjustment(
        4395.0, "GOLD DIGGERS INSTITUTIONAL", "tg-1", via, bridge=None, rs=rs))


class TestWithTheToggleOff:
    @pytest.mark.parametrize("via", ["learned_rule", "ai_fallback"])
    def test_the_adjustment_is_declined_whichever_route_asked(self, spy, via):
        """Both routes must be covered by the one check, or the next one added
        is missed again."""
        _run(_rs(0), via)

        assert spy["looked_for_trade"] == 0, "it went looking for a trade to modify"

    def test_it_does_not_even_claim_the_message(self, spy):
        """Claiming marks the message handled. Declining and claiming would
        mean the instruction could never be honoured if the toggle were turned
        back on while the message was still buffered."""
        _run(_rs(0))

        assert spy["claimed"] == 0


class TestWithTheToggleOn:
    def test_the_adjustment_proceeds_exactly_as_before(self, spy):
        """The default. Nothing about the existing behaviour changes."""
        _run(_rs(1))

        assert spy["claimed"] == 1
        assert spy["looked_for_trade"] == 1

    def test_an_absent_setting_is_treated_as_on(self, spy):
        """`lk_enable_sl_parsing` defaults to 1 everywhere else it is read; a
        row without the column must not silently disable SL adjustments."""
        _run({})

        assert spy["looked_for_trade"] == 1


class TestReadingTheSettingItself:
    def test_it_reads_the_live_settings_when_none_are_passed(self, monkeypatch, spy):
        """The two call sites should not have to fetch settings just to pass
        them in -- but a caller that already holds them may."""
        monkeypatch.setattr(af.db_module, "get_risk_settings",
                            lambda: {"lk_enable_sl_parsing": 0})

        asyncio.run(af.apply_sl_adjustment(
            4395.0, "CH", "tg-2", "learned_rule", bridge=None))

        assert spy["looked_for_trade"] == 0

    def test_unreadable_settings_do_not_block_the_adjustment(self, monkeypatch, spy):
        """Fail OPEN, like every other gate here. Refusing to move a stop
        because the settings could not be read would leave a trade sitting on
        a stop the channel has already told you to move."""
        def _boom():
            raise RuntimeError("db gone")
        monkeypatch.setattr(af.db_module, "get_risk_settings", _boom)

        asyncio.run(af.apply_sl_adjustment(
            4395.0, "CH", "tg-3", "learned_rule", bridge=None))

        assert spy["looked_for_trade"] == 1
